"""Службы контекста media, создаваемые один раз при старте процесса: хранилище, привязки и выдача ссылок."""

from dataclasses import dataclass

from messunjerr.media.domain.ports import AssetAudience, AssetUsage, ObjectStorage
from messunjerr.media.infra.memory import UnconfiguredStorage
from messunjerr.media.infra.s3 import S3ObjectStorage
from messunjerr.media.infra.usage import CompositeAssetAudience, CompositeAssetUsage
from messunjerr.media.queries.presenter import AssetPresenter
from messunjerr.settings import Settings


def build_storage(settings: Settings) -> ObjectStorage:
    """Клиент S3 по настройкам; без них заглушка, которая отвечает «хранилище недоступно»."""
    if (
        settings.s3_endpoint_internal is None
        or settings.s3_access_key is None
        or settings.s3_secret_key is None
    ):
        return UnconfiguredStorage()
    return S3ObjectStorage(
        internal_endpoint=settings.s3_endpoint_internal,
        public_endpoint=settings.storage_public_url,
        bucket=settings.s3_bucket,
        access_key=settings.s3_access_key.get_secret_value(),
        secret_key=settings.s3_secret_key.get_secret_value(),
        region=settings.s3_region,
    )


@dataclass(slots=True)
class MediaServices:
    storage: ObjectStorage
    usage: AssetUsage
    audience: AssetAudience
    presenter: AssetPresenter

    async def close(self) -> None:
        await self.storage.close()


def create_media_services(
    settings: Settings,
    *,
    storage: ObjectStorage | None = None,
    usage: AssetUsage | None = None,
    audience: AssetAudience | None = None,
) -> MediaServices:
    """`storage` подставляют тесты; `usage` и `audience` собирает корень приложения из привязок контекстов."""
    chosen = storage if storage is not None else build_storage(settings)
    return MediaServices(
        storage=chosen,
        usage=usage if usage is not None else CompositeAssetUsage(),
        audience=audience if audience is not None else CompositeAssetAudience(),
        presenter=AssetPresenter(chosen, settings),
    )
