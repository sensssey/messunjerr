"""Ошибки контекста profiles: нарушение инварианта и элементы `errors[]` по каталогу 5.14."""

import uuid

from messunjerr.core.codes import ItemCode
from messunjerr.core.errors import ErrorItem
from messunjerr.profiles.domain.ports import AvatarCheck
from messunjerr.profiles.domain.rules import BirthDateProblem


class ProfileMissingError(RuntimeError):
    """У аккаунта нет профиля или настроек приватности. Так быть не должно: строки создаёт регистрация
    в той же транзакции, а миграция 0003 создала их прежним аккаунтам. Это 500, а не 404: молча подставить
    значения по умолчанию значило бы скрыть повреждение данных."""

    def __init__(self, user_id: uuid.UUID) -> None:
        super().__init__(f"profile rows are missing for user {user_id}")
        self.user_id = user_id


_BIRTH_DATE_ITEMS: dict[BirthDateProblem, tuple[ItemCode, str]] = {
    BirthDateProblem.UNDERAGE: (
        ItemCode.UNDERAGE,
        "The minimum age for the service is not reached.",
    ),
    BirthDateProblem.FUTURE: (ItemCode.OUT_OF_RANGE, "The birth date cannot be in the future."),
    BirthDateProblem.IMPLAUSIBLE: (ItemCode.OUT_OF_RANGE, "The birth date is not plausible."),
}

_AVATAR_DETAILS = {
    AvatarCheck.NOT_FOUND: "The asset does not exist or does not belong to you.",
    AvatarCheck.NOT_READY: "The asset is not processed yet.",
    AvatarCheck.WRONG_PURPOSE: "The asset was not uploaded as an avatar.",
}


def birth_date_error(problem: BirthDateProblem, *, min_age: int) -> ErrorItem:
    code, detail = _BIRTH_DATE_ITEMS[problem]
    meta = {"min_age": min_age} if problem is BirthDateProblem.UNDERAGE else {}
    return ErrorItem("/body/birth_date", code, detail, meta)


def avatar_error(check: AvatarCheck) -> ErrorItem:
    return ErrorItem("/body/avatar_asset_id", check.value, _AVATAR_DETAILS[check])
