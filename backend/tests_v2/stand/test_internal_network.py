"""SeaweedFS изнутри сети стенда: наружу контейнера выставлены только порты S3 (проверка кода S4).

Права S3 (IAM) защищают лишь HTTP-порт S3. Master, volume и filer SeaweedFS отдают объекты без
проверки прав, а gRPC-порт S3-шлюза позволяет любому соседу добавить себе администратора. Поэтому
всё, кроме S3, должно слушать только 127.0.0.1, а сосед по сети (скомпрометированный API, воркер,
Redis) не должен достучаться ни до чего другого.
"""

import asyncio
from urllib.parse import urlsplit

import pytest

from .conftest import Stand

S3_PORTS = (8333, 8334)
CLOSED_PORTS = (
    8888,  # filer: чтение и удаление объектов в обход IAM
    9333,  # master
    8080,  # volume
    18888,  # filer gRPC
    19333,  # master gRPC
    18080,  # volume gRPC
    18333,  # S3 gRPC: PutIdentity создаёт администратора без проверки
    8181,  # каталог Iceberg
    9101,  # Lance
    9000,  # локальный HTTP-порт S3 за пробросом
    9001,  # локальный HTTPS-порт S3 за пробросом
    19000,  # локальный gRPC S3
)


async def is_open(host: str, port: int) -> bool:
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=3)
    except (OSError, TimeoutError):
        return False
    writer.close()
    return True


def storage_host(stand: Stand) -> str:
    host = urlsplit(stand.internal_s3_url).hostname
    assert host
    return host


@pytest.mark.parametrize("port", S3_PORTS)
async def test_the_s3_ports_are_reachable_from_the_internal_network(
    stand: Stand, port: int
) -> None:
    assert await is_open(storage_host(stand), port)


@pytest.mark.parametrize("port", CLOSED_PORTS)
async def test_everything_else_in_seaweedfs_is_closed_to_the_neighbours(
    stand: Stand, port: int
) -> None:
    assert not await is_open(storage_host(stand), port), f"порт {port} SeaweedFS открыт соседям"
