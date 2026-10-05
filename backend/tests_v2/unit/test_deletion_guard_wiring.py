"""Каждое место, где identity выдаёт access-токен, обязано подтверждать признак «ждёт удаления».

Ограничение `account_deletion_pending` держит признак в Redis; его ставит `DELETE /me`, а выдача нового
токена (вход, подтверждение почты, `refresh`) подтверждает и продлевает (4.7). Если новый путь выдачи
токена (например, вход через OAuth в S21) забудет это сделать, потерянный Redis снимет ограничение
до следующего обновления токена. Тест читает исходники команд и красный, пока такой путь не вызовет
`confirm_deletion_flag_after_commit`.
"""

import re
from pathlib import Path

import pytest

COMMANDS = Path(__file__).resolve().parents[2] / "src" / "messunjerr" / "identity" / "commands"
ISSUES_A_TOKEN = re.compile(r"\btokens\.issue\(|\bstart_session\(")
CONFIRMS_THE_FLAG = "confirm_deletion_flag_after_commit("
DEFINES_THE_HELPERS = "common.py"


def command_modules() -> list[Path]:
    return sorted(path for path in COMMANDS.glob("*.py") if path.name != DEFINES_THE_HELPERS)


def test_the_commands_directory_is_where_the_tests_expect_it() -> None:
    assert COMMANDS.is_dir()
    assert len(command_modules()) >= 8


def test_there_are_token_issuing_commands_to_check() -> None:
    issuing = {
        path.name for path in command_modules() if ISSUES_A_TOKEN.search(path.read_text("utf-8"))
    }
    assert {"login.py", "verify_email.py", "refresh.py"} <= issuing


@pytest.mark.parametrize(
    "path",
    [p for p in command_modules() if ISSUES_A_TOKEN.search(p.read_text("utf-8"))],
    ids=lambda p: p.name,
)
def test_every_command_that_issues_a_token_confirms_the_deletion_flag(path: Path) -> None:
    source = path.read_text("utf-8")
    issued = len(ISSUES_A_TOKEN.findall(source))
    confirmed = source.count(CONFIRMS_THE_FLAG)
    assert confirmed >= issued, (
        f"{path.name}: токен выдаётся {issued} раз, признак удаления подтверждается {confirmed}"
    )
