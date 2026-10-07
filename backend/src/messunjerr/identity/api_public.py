"""Публичный интерфейс identity для контекстов выше (4.2, правило 1).

Остальные контексты берут отсюда зависимости аутентификации, карточку аккаунта и порты, которые они
реализуют; модели, репозитории и команды identity они не импортируют (контракт в `.importlinter`).
"""

from messunjerr.identity.api.deps import (
    ClientDep,
    Principal,
    PrincipalAllowingDeletionDep,
    PrincipalDep,
    SensitivePrincipalDep,
    limit_user,
    no_store,
    principal_user_id,
)
from messunjerr.identity.commands.common import ClientInfo
from messunjerr.identity.infra.ports import MeExtrasProvider, ProfileProvisioner, ProfileSeed
from messunjerr.identity.queries.accounts import AccountCard, find_account

__all__ = [
    "AccountCard",
    "ClientDep",
    "ClientInfo",
    "MeExtrasProvider",
    "Principal",
    "PrincipalAllowingDeletionDep",
    "PrincipalDep",
    "ProfileProvisioner",
    "ProfileSeed",
    "SensitivePrincipalDep",
    "find_account",
    "limit_user",
    "no_store",
    "principal_user_id",
]
