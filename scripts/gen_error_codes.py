"""Генерирует `backend/src/messunjerr/core/codes.py` из каталога ошибок (раздел 5.14 backend-v2-spec.md).

Запуск из корня репозитория: `py -3 scripts/gen_error_codes.py` (только стандартная библиотека).
Каталог в спецификации остаётся источником правды: при его изменении файл перегенерируется,
а тест `test_codes_match_spec` сверяет их, когда документация доступна.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "docs" / "backend-v2-spec.md"
OUT = ROOT / "backend" / "src" / "messunjerr" / "core" / "codes.py"

# Коды, которые встречаются только как адреса перенаправления OAuth и не имеют HTTP-статуса.
REDIRECT_ONLY = ("oauth_failed", "oauth_email_conflict", "oauth_email_required", "terms_required")


def parse_catalog(text: str) -> tuple[list[tuple[str, str, int]], list[tuple[str, list[str]]]]:
    start = text.index("### 5.14. Каталог ошибок")
    end = text.index("### 5.15. Каталог событий")
    block = text[start:end]

    problems: list[tuple[str, str, int]] = []  # (раздел, код, статус)
    items: list[tuple[str, list[str]]] = []  # (область, коды элементов)
    section = ""
    in_items = False
    for line in block.splitlines():
        if line.startswith("#### "):
            section = line[5:].strip()
            in_items = section.startswith("Коды элементов")
            continue
        if not line.startswith("|") or set(line.replace("|", "").strip()) <= {"-", ":", " "}:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if in_items:
            if cells[0] in ("Область",):
                continue
            items.append((cells[0], re.findall(r"`([a-z][a-z0-9_]*)`", cells[1])))
            continue
        if cells[0].startswith("`code`"):
            continue
        codes = re.findall(r"`([a-z][a-z0-9_]*)`", cells[0])
        match = re.fullmatch(r"\d{3}", cells[1])
        if not codes or not match:
            continue
        for code in codes:
            problems.append((section, code, int(cells[1])))
    return problems, items


def title(code: str) -> str:
    special = {"dm_forbidden": "DM forbidden", "csrf_failed": "CSRF check failed"}
    return special.get(code, code.replace("_", " ").capitalize())


def render(problems: list[tuple[str, str, int]], items: list[tuple[str, list[str]]]) -> str:
    out: list[str] = []
    out.append('"""Коды ошибок API: единый источник для problem+json (каталог 5.14 backend-v2-spec.md).')
    out.append("")
    out.append("Файл сгенерирован `scripts/gen_error_codes.py`; вручную не править.")
    out.append('"""')
    out.append("")
    out.append("# Имена вроде TOKEN_INVALID линтер принимает за пароли: это просто коды ошибок.")
    out.append("# ruff: noqa: S105")
    out.append("")
    out.append("from enum import StrEnum")
    out.append("")
    out.append("")
    out.append("class ErrorCode(StrEnum):")
    out.append('    """Код верхнего уровня (`code` в problem+json)."""')
    out.append("")
    current = None
    for section, code, _status in problems:
        if section != current:
            if current is not None:
                out.append("")
            out.append(f"    # {section}")
            current = section
        out.append(f'    {code.upper()} = "{code}"')
    out.append("")
    out.append("")
    out.append("class ItemCode(StrEnum):")
    out.append('    """Код элемента `errors[].code` у `validation_error`."""')
    out.append("")
    for area, codes in items:
        out.append(f"    # {area}")
        for code in codes:
            out.append(f'    {code.upper()} = "{code}"')
        out.append("")
    out.pop()
    out.append("")
    out.append("")
    out.append("# Адреса перенаправления OAuth (`/login?error=<код>`): HTTP-статуса у них нет.")
    out.append("OAUTH_REDIRECT_ERRORS: frozenset[str] = frozenset(")
    out.append("    {")
    for code in REDIRECT_ONLY:
        out.append(f'        "{code}",')
    out.append("    }")
    out.append(")")
    out.append("")
    out.append("")
    out.append("# (HTTP-статус, краткое стабильное название) для каждого кода.")
    out.append("PROBLEM_SPECS: dict[ErrorCode, tuple[int, str]] = {")
    for _section, code, status in problems:
        out.append(f"    ErrorCode.{code.upper()}: ({status}, {title(code)!r}),")
    out.append("}")
    out.append("")
    return "\n".join(out)


def main() -> int:
    text = SPEC.read_text(encoding="utf-8")
    problems, items = parse_catalog(text)
    seen: set[str] = set()
    for _s, code, _st in problems:
        if code in seen:
            print(f"дубликат кода: {code}", file=sys.stderr)
            return 1
        seen.add(code)
    OUT.write_text(render(problems, items), encoding="utf-8", newline="\n")
    print(f"{OUT.relative_to(ROOT)}: {len(problems)} кодов, {sum(len(c) for _, c in items)} кодов элементов")
    return 0


if __name__ == "__main__":
    sys.exit(main())
