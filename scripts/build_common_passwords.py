"""Собирает список частых паролей для политики паролей (4.7): `identity/domain/data/common_passwords.txt`.

Источник: SecLists, файлы `xato-net-10-million-passwords-100000.txt` и `Pwdb_top-100000.txt`
(лицензия MIT, https://github.com/danielmiessler/SecLists). В список попадают пароли длиной не
меньше минимальной (короче всё равно не пройдёт проверку длины), приведённые к нижнему регистру,
плюс русские пароли, набранные в латинской раскладке.

    docker compose -f deploy/compose.dev.yml run --rm --no-deps tools \
        python /scripts/build_common_passwords.py /app/src/messunjerr/identity/domain/data/common_passwords.txt

Скрипт нужен, чтобы список можно было воспроизвести и обновить; сам список лежит в репозитории.
"""

import sys
import unicodedata
import urllib.request
from datetime import date
from pathlib import Path

_BASE = "https://raw.githubusercontent.com/danielmiessler/SecLists/master/Passwords/Common-Credentials/"
SOURCE_URLS = (
    _BASE + "xato-net-10-million-passwords-100000.txt",
    _BASE + "Pwdb_top-100000.txt",
)
MIN_LENGTH = 10

# Популярные пароли русскоязычных пользователей: слова в латинской раскладке и клавиатурные ряды.
EXTRA = [
    "ghbdtn1234", "ghbdtnghbdtn", "ghbdtn123456", "vfrcbv1234", "yfpdfybt12", "gfhjkm1234",
    "lbvf1234567", "cjkywe1234", "ctrhtnysq1", "1q2w3e4r5t6y", "1q2w3e4r5t", "1qaz2wsx3edc",
    "zaq12wsxcde3", "zaq1xsw2cde3", "qazwsxedcrfv", "qwertyuiop", "qwertyuiopasdfghjkl",
    "asdfghjkl;", "йцукенгшщзх", "йцукенгшщзхъ", "фывапролджэ", "qwerty123456", "qwerty1234567",
    "password123", "password1234", "1234567890", "12345678910", "0987654321", "9876543210",
    "1111111111", "0000000000", "123123123123", "11223344556", "123456789a", "a123456789",
    "1234567890q", "qwe123qwe123", "q1w2e3r4t5", "q1w2e3r4t5y6", "1q2w3e4r5t6y7u",
]  # fmt: skip


def main() -> None:
    target = Path(sys.argv[1])
    lines: list[str] = []
    for url in SOURCE_URLS:
        with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310 (адреса заданы выше)
            lines += response.read().decode("utf-8", errors="ignore").splitlines()
    found: dict[str, None] = {}
    for line in lines + EXTRA:
        password = unicodedata.normalize("NFC", line.strip()).lower()
        if len(password) >= MIN_LENGTH:
            found[password] = None
    header = (
        "# Частые пароли (в нижнем регистре, длиной не меньше 10 символов), по одному в строке.\n"
        "# Сгенерировано scripts/build_common_passwords.py "
        f"{date.today().isoformat()}.\n"
        "# Источник: SecLists xato-net-10-million-passwords-100000 и Pwdb_top-100000 (MIT), плюс\n"
        "# русские пароли в латинской раскладке. Строки с # и пустые строки игнорируются.\n"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(header + "\n".join(found) + "\n", encoding="utf-8")
    print(f"{target}: {len(found)} паролей")  # noqa: T201


if __name__ == "__main__":
    main()
