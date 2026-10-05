"""Как показать сессию человеку: подпись устройства по User-Agent и маска IP-адреса.

Подпись нужна, когда клиент не передал `device_label` при входе. Разбор намеренно простой: он
показывает «Firefox на Windows», чтобы человек узнал свою сессию, а не определяет устройство.
"""

import ipaddress
import re

_BROWSERS: tuple[tuple[str, str], ...] = (
    (r"Edg(e|A|iOS)?/", "Edge"),
    (r"OPR/|Opera", "Opera"),
    (r"YaBrowser/", "Яндекс Браузер"),
    (r"Firefox/|FxiOS/", "Firefox"),
    (r"Chrome/|CriOS/", "Chrome"),
    (r"Safari/", "Safari"),
    (r"curl/", "curl"),
    (r"python-httpx|python-requests|aiohttp", "Python"),
)
_SYSTEMS: tuple[tuple[str, str], ...] = (
    (r"Windows", "Windows"),
    (r"Android", "Android"),
    (r"iPhone|iPad|iOS", "iOS"),
    (r"Mac OS X|Macintosh", "macOS"),
    (r"CrOS", "ChromeOS"),
    (r"Linux|X11", "Linux"),
)


def describe_user_agent(user_agent: str | None) -> str | None:
    """«Firefox на Windows», «Chrome» или `None`, если ничего узнать не удалось."""
    if not user_agent:
        return None
    browser = next((name for pattern, name in _BROWSERS if re.search(pattern, user_agent)), None)
    system = next((name for pattern, name in _SYSTEMS if re.search(pattern, user_agent)), None)
    if browser and system:
        return f"{browser} на {system}"
    return browser or system


def mask_ip(value: str | None) -> str | None:
    """`203.0.113.45` -> `203.0.113.x`; IPv6 показывается по префиксу /48: `2001:db8:85a3::x`."""
    if not value:
        return None
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv4Address):
        return ".".join([*str(address).split(".")[:3], "x"])
    network = ipaddress.ip_network(f"{address}/48", strict=False)
    return f"{network.network_address}x"
