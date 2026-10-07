// Проверка потоков и presigned URL из браузера (S4-02, S4-05). Скрипт внешний: CSP стенда запрещает
// встроенные. Кнопки на странице нужны для ручной проверки в Chrome и Яндекс.Браузере; с `#auto=1&put=…&get=…&pub=…`
// страница сама проходит весь сценарий и ставит заголовок `done` (так её запускает scripts/stand_browser_check.py).
const logEl = document.getElementById("log");
const log = (text) => {
  logEl.textContent += `\n${new Date().toISOString().slice(11, 23)}  ${text}`;
};
const $ = (id) => document.getElementById(id);
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const params = new URLSearchParams(location.hash.slice(1));

function runSse(count) {
  return new Promise((resolve) => {
    const source = new EventSource(`/api/v1/_spike/sse?count=${count}&interval=0.3`);
    let ticks = 0;
    source.addEventListener("tick", (event) => {
      const { n, sent_at: sentAt } = JSON.parse(event.data);
      ticks += 1;
      // В автоматическом прогоне время виртуальное (--virtual-time-budget): задержку не показываем.
      log(params.has("auto") ? `SSE tick ${n}` : `SSE tick ${n}, задержка ${Math.round(Date.now() - sentAt * 1000)} мс`);
    });
    source.addEventListener("end", () => {
      log(`SSE: конец потока, событий ${ticks}`);
      source.close();
      resolve(ticks);
    });
    source.addEventListener("bye", () => log("SSE: сервер закрывается, переподключение"));
    source.onerror = () => log("SSE: ошибка или переподключение");
  });
}

let socket = null;
function openSocket(tick) {
  return new Promise((resolve, reject) => {
    socket = new WebSocket(`wss://${location.host}/api/v1/_spike/ws?tick=${tick}`);
    socket.onopen = () => {
      log("WS: открыт");
      $("ws-send").disabled = false;
      $("ws-close").disabled = false;
      resolve(socket);
    };
    socket.onmessage = (event) => log(`WS: получено ${event.data}`);
    socket.onclose = (event) => {
      log(`WS: закрыт, код ${event.code}`);
      $("ws-send").disabled = true;
      $("ws-close").disabled = true;
    };
    socket.onerror = () => reject(new Error("WebSocket не открылся"));
  });
}

async function presigned(putUrl, getUrl) {
  const body = new Blob([new Uint8Array(100000).fill(65)], { type: "image/webp" });
  const put = await fetch(putUrl, { method: "PUT", body, headers: { "Content-Type": "image/webp" } });
  log(`PUT ${put.status}`);
  const get = await fetch(getUrl);
  const bytes = await get.arrayBuffer();
  log(`GET ${get.status}, ${bytes.byteLength} байт, ${get.headers.get("content-type")}`);
}

async function publicAvatar(url) {
  const response = await fetch(url);
  log(`PUBLIC ${response.status}, cache-control=${response.headers.get("cache-control")}`);
}

$("sse").addEventListener("click", () => runSse(10));
$("ws").addEventListener("click", () => openSocket(5).catch((error) => log(String(error))));
$("ws-send").addEventListener("click", () => socket?.send("привет"));
$("ws-close").addEventListener("click", () => socket?.close(1000));
$("presign").addEventListener("click", () =>
  presigned($("put").value, $("get").value).catch((error) => log(`presigned: ${error}`)),
);

async function auto() {
  try {
    await runSse(3);
    const ws = await openSocket(0);
    const echo = new Promise((resolve) => {
      ws.addEventListener("message", (event) => resolve(event.data), { once: true });
    });
    ws.send("привет");
    log(`WS: эхо «${await echo}»`);
    ws.close(1000);
    await sleep(300);
    await presigned(params.get("put"), params.get("get"));
    if (params.get("pub")) await publicAvatar(params.get("pub"));
    const protocols = new Set(performance.getEntriesByType("resource").map((entry) => entry.nextHopProtocol));
    log(`протоколы: ${[...protocols].join(", ")}`);
    log("AUTO: ВСЁ ПРОШЛО");
  } catch (error) {
    log(`AUTO: ОШИБКА ${error}`);
  }
  document.title = "done";
}

if (params.has("auto")) auto();
