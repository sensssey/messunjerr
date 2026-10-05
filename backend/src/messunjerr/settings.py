"""Настройки приложения: переменные окружения и секреты из файлов (`<ИМЯ>_FILE`).

Список и значения по умолчанию описаны в приложении 6.1 backend-v2-spec.md. Значения по умолчанию
безопасны для продакшена: например, документация OpenAPI включается только вне `prod`.
"""

import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    NoDecode,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

AppEnv = Literal["dev", "test", "stage", "prod"]
LogFormat = Literal["json", "console"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]

DEFAULT_REACTION_PALETTE: tuple[str, ...] = ("👍", "❤️", "😂", "😮", "😢", "😡", "🔥", "🎉")
GIB = 1024**3


class FileSecretsSource(PydanticBaseSettingsSource):
    """Берёт значение поля из файла, путь к которому лежит в переменной `<ИМЯ_ПОЛЯ>_FILE`.

    Так в контейнеры передаются секреты (Docker secrets, файлы с правами 0600), чтобы они не
    попадали в переменные окружения и в `docker inspect`.
    """

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        path = os.environ.get(f"{field_name.upper()}_FILE")
        if not path:
            return None, field_name, False
        return Path(path).read_text(encoding="utf-8").strip(), field_name, False

    def __call__(self) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for name, field in self.settings_cls.model_fields.items():
            value, key, _ = self.get_field_value(field, name)
            if value is not None:
                values[key] = value
        return values


def _split_csv(value: Any) -> Any:
    """Разбирает «a,b,c» из переменной окружения в список (JSON-вариант тоже допустим)."""
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            return value
        return [part.strip() for part in text.split(",") if part.strip()]
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False, frozen=True)

    # --- окружение
    app_env: AppEnv = "prod"
    app_build: str = Field(default="dev", description="git sha сборки, попадает в /api/v1/meta")
    public_base_url: str | None = None

    # --- логи
    log_level: LogLevel = "INFO"
    log_format: LogFormat = "json"

    # --- PostgreSQL: роли app (DML), migrator (DDL) и admin (только для CLI и тестов)
    database_url: SecretStr
    migrator_database_url: SecretStr | None = None
    admin_database_url: SecretStr | None = None
    db_readonly_password: SecretStr | None = None
    db_pool_size: int = Field(default=10, ge=1, le=100)
    db_max_overflow: int = Field(default=10, ge=0, le=100)
    db_pool_timeout_seconds: float = Field(default=5.0, gt=0)
    db_connect_timeout_seconds: float = Field(default=5.0, gt=0)

    # --- Redis
    redis_url: SecretStr
    redis_socket_timeout_seconds: float = Field(default=2.0, gt=0)
    redis_max_connections: int = Field(default=50, ge=1, le=1000)
    redis_pool_timeout_seconds: float = Field(
        default=2.0,
        gt=0,
        description="сколько ждать свободное соединение, когда все заняты (потом: Redis недоступен)",
    )

    # --- HTTP
    request_body_limit_bytes: int = Field(default=1_048_576, ge=1024)

    # --- токены и сессии (4.7): ключ Ed25519 в PEM (PKCS8) или 32 байта seed в base64url
    jwt_private_key: SecretStr | None = None
    jwt_key_id: str | None = Field(
        default=None, description="по умолчанию: отпечаток ключа (RFC 7638)"
    )
    jwt_issuer: str = "messunjerr"
    access_token_ttl_seconds: int = Field(default=600, ge=60, le=3600)
    refresh_ttl_days: int = Field(default=30, ge=1, le=365)
    refresh_absolute_ttl_days: int = Field(default=90, ge=1, le=730)
    refresh_race_window_seconds: int = Field(
        default=10, ge=0, le=60, description="окно гонки двух вкладок при ротации refresh (4.7)"
    )
    email_verification_ttl_hours: int = Field(default=24, ge=1, le=168)
    password_reset_ttl_minutes: int = Field(default=60, ge=5, le=1440)
    email_change_ttl_minutes: int = Field(default=60, ge=5, le=1440)
    unverified_account_ttl_days: int = Field(
        default=7,
        ge=1,
        le=365,
        description="срок, после которого неподтверждённый аккаунт удаляется",
    )
    # Откуда принимаются запросы к cookie-ручкам (CSRF, 4.7): по умолчанию адрес клиента.
    allowed_origins: Annotated[list[str], NoDecode] = []

    # --- лимиты запросов (4.14): бакеты и значения в core/ratelimits.toml, файл их переопределяет
    rate_limits_enabled: bool = True
    rate_limits_file: str | None = None

    # --- идемпотентность (5.1): сколько хранится сохранённый ответ
    idempotency_ttl_hours: int = Field(default=24, ge=1, le=168)

    # --- пароли: Argon2id (по умолчанию профиль RFC 9106 с малой памятью); тесты уменьшают стоимость
    argon2_time_cost: int = Field(default=3, ge=1, le=20)
    argon2_memory_cost_kib: int = Field(default=65_536, ge=1024, le=2_097_152)
    argon2_parallelism: int = Field(default=4, ge=1, le=32)
    password_hash_concurrency: int = Field(default=4, ge=1, le=64)

    # --- ⚖️ юридический минимум: галочка согласия и возраст (план спринтов 1.2)
    min_age: int = Field(default=18, ge=0, le=100)
    legal_terms_version: str = Field(default="2026-10-01", min_length=1, max_length=32)
    legal_operator_name: str | None = None
    legal_operator_address: str | None = None
    legal_contact_email: str | None = None

    # --- аккаунт: срок восстановления после запроса удаления (⚖️) и пауза между сменами ника
    account_deletion_grace_days: int = Field(default=14, ge=1, le=90)
    username_change_cooldown_days: int = Field(
        default=30,
        ge=0,
        le=365,
        description="пауза между сменами ника; столько же прежний ник остаётся занятым (5.3)",
    )

    # --- почта: письма уходят фоновой задачей `send_email` (воркер), API её только ставит
    mail_transport: Literal["smtp"] = "smtp"
    smtp_url: SecretStr | None = None
    mail_from: str = "messunjerr <no-reply@localhost>"

    # --- параметры, которые отдаёт /api/v1/meta и использует клиент
    auth_methods: Annotated[list[str], NoDecode] = ["password"]
    reaction_palette: Annotated[list[str], NoDecode] = list(DEFAULT_REACTION_PALETTE)
    group_max_members: int = Field(default=100, ge=2)
    message_edit_window_hours: int = Field(default=48, ge=0)
    media_quota_bytes: int = Field(default=GIB, ge=0)

    @field_validator("auth_methods", "reaction_palette", "allowed_origins", mode="before")
    @classmethod
    def _csv_lists(cls, value: Any) -> Any:
        return _split_csv(value)

    @field_validator("database_url", "migrator_database_url", "admin_database_url")
    @classmethod
    def _asyncpg_url(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not value.get_secret_value().startswith("postgresql+asyncpg://"):
            raise ValueError(
                "ожидается адрес вида postgresql+asyncpg://пользователь:пароль@хост/база"
            )
        return value

    @field_validator("redis_url")
    @classmethod
    def _redis_url(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().startswith(("redis://", "rediss://")):
            raise ValueError("ожидается адрес вида redis://[:пароль@]хост:порт/номер_базы")
        return value

    @property
    def docs_enabled(self) -> bool:
        """Документация OpenAPI отключена в проде (A19)."""
        return self.app_env != "prod"

    @property
    def base_url(self) -> str:
        """Публичный адрес для ссылок в письмах; вне prod и stage по умолчанию локальный клиент."""
        return (self.public_base_url or "http://localhost:3000").rstrip("/")

    @property
    def origins(self) -> frozenset[str]:
        """Допустимые источники запросов к cookie-ручкам: адрес клиента и `ALLOWED_ORIGINS`."""
        parts = urlsplit(self.base_url)
        client_origin = f"{parts.scheme}://{parts.netloc}"
        return frozenset({client_origin, *(origin.rstrip("/") for origin in self.allowed_origins)})

    @property
    def strict_runtime(self) -> bool:
        """В prod и stage обязательные секреты и адреса проверяются при старте (`check_runtime`)."""
        return self.app_env in ("prod", "stage")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Порядок важен: явные аргументы, затем переменные окружения, затем файлы `*_FILE`.
        return (init_settings, env_settings, FileSecretsSource(settings_cls))


def check_runtime(settings: Settings, *, needs_mail: bool = False) -> None:
    """Останавливает процесс при старте, если в prod или stage не хватает обязательных значений.

    Проверка не входит в валидацию модели: утилиты и тесты создают `Settings` частично.
    `needs_mail` включает проверку SMTP, она нужна воркеру, но не API.
    """
    if not settings.strict_runtime:
        return
    missing: list[str] = []
    if not settings.public_base_url:
        missing.append("PUBLIC_BASE_URL")
    if settings.jwt_private_key is None:
        missing.append("JWT_PRIVATE_KEY (или JWT_PRIVATE_KEY_FILE)")
    if needs_mail and settings.smtp_url is None:
        missing.append("SMTP_URL (или SMTP_URL_FILE)")
    if missing:
        raise RuntimeError(f"APP_ENV={settings.app_env}: не заданы {', '.join(missing)}")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    # Обязательные поля приходят из окружения; без них приложение должно упасть при старте.
    return Settings()  # pyright: ignore[reportCallIssue]
