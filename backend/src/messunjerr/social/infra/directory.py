"""Чужие таблицы, которые социальный граф только читает: кто этот человек и жив ли его аккаунт.

Запросы списков (друзья, подписчики, заявки, блокировки) собирают карточки людей одним соединением, а
не вызовом на каждого (4.6, 6.5: запросы выражают условия в SQL). Подписка читает ещё закрытость
профиля цели (`profiles.is_private`) под `FOR SHARE`. Пишет в эти таблицы только владелец (identity и
profiles), а здесь они описаны лёгкими табличными выражениями: модели чужих контекстов импортировать
нельзя (контракт `.importlinter`), и схема остаётся их собственностью.
"""

from sqlalchemy import Boolean, Text, Uuid, column, table

users = table(
    "users",
    column("id", Uuid()),
    column("username", Text()),
    column("status", Text()),
    schema="identity",
)
profiles = table(
    "profiles",
    column("user_id", Uuid()),
    column("display_name", Text()),
    column("avatar_asset_id", Uuid()),
    column("is_private", Boolean()),
    schema="profile",
)

ACTIVE = "active"
"""Статус аккаунта, которого видят другие (`identity.users.status`); остальные скрыты (4.6)."""
