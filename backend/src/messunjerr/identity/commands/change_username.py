"""Команда `PATCH /me/username` (5.3, S3-03): смена ника не чаще раза в 30 дней.

- Первая смена бесплатна (`username_changed_at` пуст); дальше нужно подождать `USERNAME_CHANGE_COOLDOWN_DAYS`
  дней, иначе `409 username_change_cooldown` с `retry_after_days`.
- Прежний ник остаётся занятым на тот же срок (резерв в `identity.username_reservations`): иначе чужой
  человек мог бы тут же занять освободившееся имя и выдать себя за прежнего владельца.
- Тот же ник, что и сейчас, не считается сменой: ответ `200`, пауза не запускается.
- Занятый ник отвечает `409 username_taken`, зарезервированный системой (`admin`, `support`…)
  `422 username_reserved`.

В аудит попадает факт смены без самих ников: журнал только растёт, а ник личный идентификатор.
"""

import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.exc import IntegrityError

from messunjerr.core.audit import record_audit
from messunjerr.core.clock import utcnow
from messunjerr.core.codes import ItemCode
from messunjerr.core.uow import UnitOfWork
from messunjerr.identity.commands.common import ClientInfo
from messunjerr.identity.domain import audit
from messunjerr.identity.domain.errors import (
    field_error,
    token_user_gone,
    username_change_cooldown,
    username_taken,
)
from messunjerr.identity.domain.usernames import UsernameProblem, check_username
from messunjerr.identity.infra.repositories import (
    UsernameReservationRepository,
    UserRepository,
    violated_constraint,
)
from messunjerr.identity.queries.me import username_is_held
from messunjerr.settings import Settings

SECONDS_PER_DAY = 24 * 3600


@dataclass(frozen=True, slots=True)
class ChangeUsername:
    user_id: uuid.UUID
    username: str
    """Новый ник, уже приведённый к нижнему регистру."""
    client: ClientInfo


def _check_format(username: str) -> None:
    match check_username(username):
        case UsernameProblem.RESERVED:
            raise field_error(
                "/body/username", ItemCode.USERNAME_RESERVED, "This username is reserved."
            )
        case UsernameProblem.INVALID:
            raise field_error(
                "/body/username", ItemCode.INVALID_FORMAT, "Use 3-30 letters, digits or _."
            )
        case None:
            pass


def _days_left(until: datetime, now: datetime) -> int:
    """Сколько дней ждать до `until`, округляя вверх: «осталось 0 дней» клиенту не поможет."""
    return max(1, math.ceil((until - now).total_seconds() / SECONDS_PER_DAY))


async def change_username(
    command: ChangeUsername,
    *,
    uow: UnitOfWork,
    settings: Settings,
    now: datetime | None = None,
) -> str:
    """Меняет ник и возвращает новый. Строка пользователя блокируется: параллельные смены ждут."""
    moment = now or utcnow()
    _check_format(command.username)
    user = await UserRepository(uow.session).get_by_id(command.user_id, for_update=True)
    if user is None:
        raise token_user_gone()
    if user.username == command.username:
        return user.username

    cooldown = timedelta(days=settings.username_change_cooldown_days)
    if user.username_changed_at is not None and moment < user.username_changed_at + cooldown:
        raise username_change_cooldown(_days_left(user.username_changed_at + cooldown, moment))
    if await username_is_held(uow.session, command.username, now=moment, except_user_id=user.id):
        raise username_taken()

    previous = user.username
    try:
        # Точка сохранения: гонка за один и тот же ник кончается `409`, а не сломанной транзакцией.
        async with uow.session.begin_nested():
            user.username = command.username
            user.username_changed_at = moment
            await uow.session.flush()
    except IntegrityError as error:
        if violated_constraint(error) == "uq_users_username":
            raise username_taken() from error
        raise
    if cooldown > timedelta(0):
        await UsernameReservationRepository(uow.session).reserve(
            previous, user_id=user.id, until=moment + cooldown
        )
    record_audit(
        uow.session,
        action=audit.USERNAME_CHANGED,
        actor_id=user.id,
        target_type=audit.TARGET_USER,
        target_id=user.id,
        ip=command.client.ip,
        user_agent=command.client.user_agent,
    )
    await uow.commit()
    return user.username
