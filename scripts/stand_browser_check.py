"""Проверка стенда настоящим браузером (S4-02, S4-05): только стандартная библиотека.

Запуск: `py -3 scripts/stand_browser_check.py [--browser ПУТЬ]`. Скрипт выпускает presigned-ссылки
(ключи из deploy/secrets), открывает headless-браузер на `https://messunjerr.localhost/spike.html`
и по журналу страницы проверяет через Caddy:

- SSE (EventSource): три события и конец потока;
- WebSocket: эхо и закрытие кодом 1000;
- presigned PUT и GET из `fetch` (подписанные Content-Type и Content-Length);
- чтение публичного аватара без подписи;
- загрузка файла по ссылке, которую выдал настоящий API (S5): заявка, `PUT` из `fetch` с заголовками
  `upload.headers` (в том числе `If-None-Match: *`), повторный `PUT` получает 412, `complete`, `ready`.

Как устроено. Современные Chromium и Edge на Windows запускаются «пускачом»: `msedge.exe` порождает
отдельный процесс браузера и сразу завершается, так что `--dump-dom` ничего не печатает. Поэтому
браузером управляют по протоколу DevTools (WebSocket на 127.0.0.1): скрипт открывает страницу, ждёт в
её журнале строку `AUTO:` и читает журнал целиком.

Браузер запускается с одноразовым профилем и флагом `--ignore-certificate-errors`: корневому
сертификату Caddy он не доверяет, а менять доверие в системе скрипт не должен. Для проверки в своём
Chrome или Яндекс.Браузере откройте https://messunjerr.localhost/spike.html после `make stand-ca`.
Проверено на Microsoft Edge 154 (Windows 11).
"""

import argparse
import base64
import contextlib
import http.client
import importlib.util
import json
import os
import re
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SECRETS = ROOT / "deploy" / "secrets"
SITE = "https://messunjerr.localhost"
COMPOSE = ["docker", "compose", "-p", "messunjerr-stand", "-f", "deploy/compose.yml"]
PAGE_TIMEOUT_SECONDS = 90

BROWSERS = (
    "msedge",
    "chrome",
    "google-chrome",
    "chromium",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)

EXPECTED = (
    "SSE: конец потока, событий 3",
    "WS: эхо «привет»",
    "PUT 200",
    "GET 200, 100000 байт",
    "PUBLIC 200",
    "AUTO: ВСЁ ПРОШЛО",
)


def find_browser(explicit: str | None) -> str:
    for candidate in [explicit] if explicit else BROWSERS:
        if candidate and (Path(candidate).is_file() or shutil.which(candidate)):
            return candidate
    sys.exit("браузер не найден: укажите --browser ПУТЬ (подойдёт любой на Chromium)")


def load_signer():  # noqa: ANN201 (модуль подгружается из тестов стенда)
    spec = importlib.util.spec_from_file_location(
        "sigv4", ROOT / "backend" / "tests_v2" / "stand" / "sigv4.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def create_public_avatar(key: str) -> None:
    """Кладёт объект в `public/` по внутреннему адресу filer'а: снаружи писать в этот префикс нельзя."""
    command = (
        f'printf avatar | curl -sf -X PUT -H "Content-Type: image/webp" --data-binary @- '
        f"http://localhost:8888/buckets/media/{key}"
    )
    subprocess.run(
        [*COMPOSE, "exec", "-T", "seaweedfs", "sh", "-c", command],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )


# ----------------------------------------------------------------------------- DevTools
class WebSocket:
    """Минимальный клиент WebSocket (RFC 6455) для DevTools: текстовые кадры, без сжатия."""

    def __init__(self, url: str, timeout: float = 15.0) -> None:
        parts = urllib.parse.urlsplit(url)
        self._socket = socket.create_connection((parts.hostname or "127.0.0.1", parts.port or 80), timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {parts.path} HTTP/1.1\r\nHost: {parts.netloc}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self._socket.sendall(request.encode("ascii"))
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self._socket.recv(4096)
            if not chunk:
                raise ConnectionError("DevTools закрыл соединение при рукопожатии")
            head += chunk
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise ConnectionError(f"DevTools отказал в WebSocket: {head.split(b'\r\n', 1)[0]!r}")

    def _read(self, size: int) -> bytes:
        data = b""
        while len(data) < size:
            chunk = self._socket.recv(size - len(data))
            if not chunk:
                raise ConnectionError("соединение с DevTools закрыто")
            data += chunk
        return data

    def send(self, text: str) -> None:
        payload = text.encode("utf-8")
        header = bytearray([0x81])
        if len(payload) < 126:
            header.append(0x80 | len(payload))
        elif len(payload) < 65536:
            header += bytes([0x80 | 126]) + struct.pack(">H", len(payload))
        else:
            header += bytes([0x80 | 127]) + struct.pack(">Q", len(payload))
        mask = os.urandom(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self._socket.sendall(bytes(header) + mask + masked)

    def receive(self) -> str:
        message = b""
        while True:
            first, second = self._read(2)
            opcode, final = first & 0x0F, bool(first & 0x80)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read(8))[0]
            payload = self._read(length)  # кадры сервера не маскируются
            if opcode == 0x8:
                raise ConnectionError("DevTools закрыл WebSocket")
            if opcode == 0x9:  # ping: отвечаем тем же содержимым
                self._socket.sendall(bytes([0x8A, 0x80 | len(payload)]) + b"\0\0\0\0" + payload)
                continue
            if opcode in (0x0, 0x1, 0x2):
                message += payload
                if final:
                    return message.decode("utf-8")

    def close(self) -> None:
        try:
            self._socket.close()
        except OSError:
            pass


class DevTools:
    def __init__(self, url: str) -> None:
        self._ws = WebSocket(url)
        self._next_id = 0

    def call(self, method: str, **params: Any) -> dict[str, Any]:
        self._next_id += 1
        self._ws.send(json.dumps({"id": self._next_id, "method": method, "params": params}))
        while True:
            message: dict[str, Any] = json.loads(self._ws.receive())
            if message.get("id") == self._next_id:  # остальное это события, они не нужны
                if "error" in message:
                    raise RuntimeError(f"{method}: {message['error']}")
                result: dict[str, Any] = message.get("result", {})
                return result

    def close(self) -> None:
        self._ws.close()


def _http_json(url: str, method: str = "GET") -> Any:
    request = urllib.request.Request(url, method=method)  # noqa: S310 (адрес локальный)
    with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310
        return json.load(response)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _stop_browser(profile: str, process: subprocess.Popen[bytes]) -> None:
    """Добивает процессы браузера, оставшиеся от одноразового профиля (после `Browser.close` их нет)."""
    if process.poll() is None:
        process.terminate()
    if sys.platform == "win32":
        pattern = Path(profile).name
        script = (
            "Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -like "
            f"'*{pattern}*' }} | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force "
            "-ErrorAction SilentlyContinue }"
        )
        subprocess.run(["powershell", "-NoProfile", "-Command", script], check=False, capture_output=True)
    time.sleep(1)
    shutil.rmtree(profile, ignore_errors=True)


@contextlib.contextmanager
def open_page(browser: str, url: str) -> Iterator[DevTools]:
    """Открывает `url` в headless-браузере с одноразовым профилем и отдаёт страницу для управления."""
    profile = tempfile.mkdtemp(prefix="stand-browser-")
    port = _free_port()
    process = subprocess.Popen(  # noqa: S603
        [
            browser,
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--ignore-certificate-errors",
            f"--user-data-dir={profile}",
            f"--remote-debugging-port={port}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    page: DevTools | None = None
    try:
        deadline = time.monotonic() + 30
        version: dict[str, Any] = {}
        while time.monotonic() < deadline:
            try:
                version = _http_json(f"{base}/json/version")
                break
            except OSError:
                time.sleep(0.3)
        if not version:
            raise RuntimeError("браузер не открыл порт DevTools за 30 секунд")
        target = _http_json(f"{base}/json/new?about:blank", method="PUT")
        page = DevTools(target["webSocketDebuggerUrl"])
        page.call("Page.enable")
        page.call("Page.navigate", url=url)
        yield page
        try:
            browser_ws = DevTools(version["webSocketDebuggerUrl"])
            browser_ws.call("Browser.close")
            browser_ws.close()
        except (ConnectionError, OSError, RuntimeError):
            pass
    finally:
        if page is not None:
            page.close()
        _stop_browser(profile, process)


def evaluate(page: DevTools, expression: str, *, wait_for_promise: bool = False) -> Any:
    """Выполняет JavaScript на странице и возвращает значение (для `async`-кода нужен `wait_for_promise`)."""
    result = page.call(
        "Runtime.evaluate",
        expression=expression,
        returnByValue=True,
        awaitPromise=wait_for_promise,
    )
    if "exceptionDetails" in result:
        raise RuntimeError(f"ошибка JavaScript: {result['exceptionDetails'].get('text')}")
    return result.get("result", {}).get("value")


def read_page_log(browser: str, url: str) -> str:
    """Открывает `url` в headless-браузере и возвращает журнал страницы (`#log`) после строки `AUTO:`."""
    with open_page(browser, url) as page:
        log = ""
        deadline = time.monotonic() + PAGE_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            log = str(evaluate(page, "(document.getElementById('log') || {}).textContent || ''"))
            if re.search(r"AUTO: ", log):  # строки журнала начинаются со времени
                break
            time.sleep(0.5)
        return log


# ----------------------------------------------------------------------------- загрузка через API (S5)
_INSECURE = ssl.create_default_context()
_INSECURE.check_hostname = False
_INSECURE.verify_mode = ssl.CERT_NONE  # корневому сертификату Caddy система не доверяет, это стенд


class _StandConnection(http.client.HTTPSConnection):
    """Соединение на 127.0.0.1 с именем сайта в SNI и `Host`: Python на Windows не знает `*.localhost`."""

    def connect(self) -> None:
        sock = socket.create_connection(("127.0.0.1", 443), self.timeout)
        self.sock = _INSECURE.wrap_socket(sock, server_hostname=urllib.parse.urlsplit(SITE).hostname)


class _StandHandler(urllib.request.HTTPSHandler):
    def https_open(self, req: urllib.request.Request) -> http.client.HTTPResponse:
        return self.do_open(_StandConnection, req)  # type: ignore[arg-type]


OPENER = urllib.request.build_opener(_StandHandler)
MAILPIT = "http://127.0.0.1:8026"
UPLOAD_SIZE = 4096


def api(method: str, path: str, *, token: str | None = None, body: Any = None) -> tuple[int, Any]:
    """Запрос к API стенда через Caddy: (код, разобранный JSON или `None`)."""
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(  # noqa: S310 (адрес стенда)
        f"{SITE}/api/v1{path}", data=data, method=method, headers=headers
    )
    try:
        with OPENER.open(request, timeout=30) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as error:
        raw = error.read()
        return error.code, json.loads(raw) if raw else None


def sign_in_new_account() -> str:
    """Регистрация, письмо из Mailpit стенда, подтверждение: возвращает access-токен."""
    unique = uuid.uuid4().hex[:12]
    email = f"browser-{unique}@example.com"
    status, body = api(
        "POST",
        "/auth/register",
        body={
            "email": email,
            "username": f"browser_{unique}",
            "password": "correct horse battery staple",
            "accept_terms": True,
        },
    )
    if status != 201:
        raise RuntimeError(f"регистрация: {status} {body}")
    for _ in range(60):
        with urllib.request.urlopen(  # noqa: S310
            f"{MAILPIT}/api/v1/search?query=to:{urllib.parse.quote(email)}", timeout=10
        ) as response:
            found = json.load(response).get("messages") or []
        if found:
            with urllib.request.urlopen(  # noqa: S310
                f"{MAILPIT}/api/v1/message/{found[0]['ID']}", timeout=10
            ) as response:
                text = json.load(response)["Text"]
            token = re.search(r"#token=(\S+)", text)
            if token is None:
                raise RuntimeError("в письме нет ссылки с токеном")
            status, body = api("POST", "/auth/verify-email", body={"token": token.group(1)})
            if status != 200:
                raise RuntimeError(f"подтверждение почты: {status} {body}")
            return str(body["access_token"])
        time.sleep(0.5)
    raise RuntimeError("письмо подтверждения не пришло в Mailpit за 30 секунд")


PUT_FROM_BROWSER = """(async () => {{
  const size = {size};
  const bytes = new Uint8Array(size);
  bytes.set([0xff, 0xd8, 0xff, 0xe0, 0x00, 0x10, 0x4a, 0x46, 0x49, 0x46, 0x00]);
  for (let i = 11; i < size; i++) bytes[i] = i % 251;
  const url = {url};
  const headers = {headers};
  const first = await fetch(url, {{method: 'PUT', headers, body: new Blob([bytes])}});
  const other = bytes.slice();
  other[size - 1] ^= 0xff;
  const second = await fetch(url, {{method: 'PUT', headers, body: new Blob([other])}});
  return JSON.stringify([first.status, second.status]);
}})()"""


def check_upload_through_api(browser: str) -> list[str]:
    """Загрузка файла так, как её сделает фронтенд: заявка в API, `PUT` из `fetch` по выданной ссылке.

    Возвращает строки отчёта; в начале строки `НЕТ`, если что-то не прошло.
    """
    report: list[str] = []
    try:
        token = sign_in_new_account()
    except (RuntimeError, OSError) as error:  # например, исчерпан лимит регистраций с одного адреса
        return [f"НЕТ: нет аккаунта для проверки: {error}"]
    status, created = api(
        "POST",
        "/media/uploads",
        token=token,
        body={
            "purpose": "post",
            "filename": "browser.jpg",
            "content_type": "image/jpeg",
            "size_bytes": UPLOAD_SIZE,
        },
    )
    if status != 201:
        return [f"НЕТ: заявка на загрузку: {status} {created}"]
    upload, asset_id = created["upload"], created["asset"]["id"]
    report.append(f"заявка: 201, заголовки для PUT {upload['headers']}")

    # Страница того же происхождения, что и /media/*: запрос без CORS, как у настоящего фронтенда.
    with open_page(browser, f"{SITE}/") as page:
        for _ in range(60):
            if evaluate(page, "document.readyState") == "complete":
                break
            time.sleep(0.25)
        expression = PUT_FROM_BROWSER.format(
            size=UPLOAD_SIZE, url=json.dumps(upload["url"]), headers=json.dumps(upload["headers"])
        )
        statuses = json.loads(str(evaluate(page, expression, wait_for_promise=True)))
    report.append(f"PUT из браузера: {statuses[0]}, повторный PUT: {statuses[1]}")
    if statuses != [200, 412]:
        report.append("НЕТ: ожидалось [200, 412] (запись один раз)")
        return report

    status, done = api("POST", f"/media/uploads/{asset_id}/complete", token=token)
    report.append(f"complete: {status}, статус {(done or {}).get('asset', {}).get('status')}")
    final = ""
    for _ in range(60):
        status, asset = api("GET", f"/media/{asset_id}", token=token)
        final = str((asset or {}).get("status"))
        if final in ("ready", "rejected"):
            break
        time.sleep(0.5)
    report.append(f"воркер media: {final}, тип {(asset or {}).get('content_type')}")
    if status != 200 or final != "ready":
        report.append("НЕТ: файл не дошёл до ready")
    api("DELETE", f"/media/{asset_id}", token=token)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--browser", help="путь к браузеру на Chromium (по умолчанию Edge или Chrome)")
    args = parser.parse_args()
    browser = find_browser(args.browser)

    sigv4 = load_signer()
    access_key = (SECRETS / "s3_app_access_key").read_text(encoding="utf-8").strip()
    secret_key = (SECRETS / "s3_app_secret_key").read_text(encoding="utf-8").strip()
    key = f"uploads/browser-check/{uuid.uuid4().hex}.webp"
    avatar_key = "public/avatars/browser-check/64.webp"
    create_public_avatar(avatar_key)

    def sign(method: str, headers: dict[str, str] | None = None) -> str:
        return sigv4.presign_url(
            method,
            f"{SITE}/media/{key}",
            access_key=access_key,
            secret_key=secret_key,
            expires=900,
            signed_headers=headers,
        )

    fragment = urllib.parse.urlencode(
        {
            "auto": "1",
            "put": sign("PUT", {"Content-Type": "image/webp", "Content-Length": "100000"}),
            "get": sign("GET"),
            "pub": f"{SITE}/media/{avatar_key}",
        }
    )
    log = read_page_log(browser, f"{SITE}/spike.html#{fragment}")

    print(f"браузер: {browser}")
    print(log.strip() or "журнал страницы пуст (страница не открылась?)")
    missing = [marker for marker in EXPECTED if marker not in log]
    if missing:
        print("\nНЕ НАЙДЕНО в журнале:", *missing, sep="\n  ")
        return 1

    print("\nзагрузка файла через API (S5):")
    upload_report = check_upload_through_api(browser)
    for line in upload_report:
        print(" ", line)
    if any(line.startswith("НЕТ") for line in upload_report):
        return 1
    print("\nПРОВЕРКА В БРАУЗЕРЕ ПРОЙДЕНА")
    return 0


if __name__ == "__main__":
    sys.exit(main())
