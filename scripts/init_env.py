"""Создаёт или дополняет deploy/.env по deploy/.env.example (только стандартная библиотека).

Запуск: `py -3 scripts/init_env.py [--force]`.

- Нет `.env`: создаётся из примера, заглушки заменяются случайными значениями.
- `.env` уже есть: существующие значения не трогаются, дописываются только новые переменные из
  примера (после обновления проекта). С `--force` файл создаётся заново.

Заглушки: `CHANGE_ME` (пароль, 32 hex-символа) и `CHANGE_ME_JWT_SEED` (seed ключа Ed25519,
32 случайных байта в base64url; из него получается постоянный ключ подписи токенов в разработке).
"""

import base64
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "deploy" / ".env.example"
TARGET = ROOT / "deploy" / ".env"


def fill(line: str) -> str:
    """Заменяет заглушки в строке-присваивании случайными значениями (комментарии не трогает)."""
    if line.startswith("#"):
        return line
    seed = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
    line = line.replace("CHANGE_ME_JWT_SEED", seed)
    while "CHANGE_ME" in line:
        line = line.replace("CHANGE_ME", secrets.token_hex(16), 1)
    return line


def key_of(line: str) -> str | None:
    """Имя переменной из строки `ИМЯ=значение`; для комментариев и пустых строк `None`."""
    if line.startswith("#") or "=" not in line:
        return None
    return line.split("=", 1)[0].strip()


def main(argv: list[str]) -> int:
    example_lines = EXAMPLE.read_text(encoding="utf-8").splitlines()

    if not TARGET.exists() or "--force" in argv:
        TARGET.write_text(
            "\n".join(fill(line) for line in example_lines) + "\n", encoding="utf-8", newline="\n"
        )
        print(f"создан {TARGET.relative_to(ROOT)} со случайными паролями")
        return 0

    existing = TARGET.read_text(encoding="utf-8")
    known = {key_of(line) for line in existing.splitlines()}
    added = [fill(line) for line in example_lines if (key := key_of(line)) and key not in known]
    if not added:
        print(f"{TARGET.relative_to(ROOT)} актуален (чтобы пересоздать: --force)")
        return 0
    block = "\n# Добавлено scripts/init_env.py после обновления проекта\n" + "\n".join(added) + "\n"
    TARGET.write_text(existing.rstrip("\n") + "\n" + block, encoding="utf-8", newline="\n")
    names = ", ".join(key_of(line) or "" for line in added)
    print(f"{TARGET.relative_to(ROOT)}: добавлены {names}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
