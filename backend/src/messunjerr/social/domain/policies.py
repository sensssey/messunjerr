"""Политики социального графа (4.6): кто кому может написать заявку, подписаться, ответить на запрос,
заблокировать и посмотреть список друзей или подписчиков. Чистые функции; команды и запросы зовут
только их, а ручки своих проверок не пишут.

Порядок проверок повторяет спецификацию (5.4) и ничего не раскрывает лишнего: ресурс, о котором
зритель знать не должен (нет цели, блокировка, аккаунт не `active`), всегда `404`; запрещённое
действие над видимым ресурсом `409` или `403` с конкретным кодом.
"""

from dataclasses import dataclass
from enum import StrEnum

from messunjerr.core.me import ListVisibility
from messunjerr.profiles.api_public import (
    Relation,
    can_see_counter,
    can_view_profile,
    sees_details,
)
from messunjerr.social.domain.rules import Direction, FollowRequestStatus, FriendRequestStatus


# --------------------------------------------------------------------------- заявка в друзья
class SendDecision(StrEnum):
    CREATE = "create"
    """Заявки нет: создать (`201`)."""
    ACCEPT_INCOMING = "accept_incoming"
    """От цели уже лежит встречная заявка: принять её (`200`, статус `accepted`)."""
    SELF = "self"
    """`400 self_action`."""
    HIDDEN = "hidden"
    """`404 not_found`: цели нет, она не `active` или есть блокировка в любую сторону."""
    ALREADY_FRIENDS = "already_friends"
    """`409 already_friends`."""
    ALREADY_SENT = "already_sent"
    """`409 friend_request_exists`: ваша заявка этому человеку уже ждёт ответа."""


@dataclass(frozen=True, slots=True)
class PairState:
    """Факты о паре «действующий и цель» на момент решения; их читают под замком пары."""

    is_self: bool
    target_active: bool
    blocked: bool
    """Блокировка в любую сторону."""
    friends: bool
    pending: Direction | None = None
    """Активная заявка между ними: `outgoing` отправил действующий, `incoming` пришла ему."""


def decide_friend_request(state: PairState) -> SendDecision:
    """Можно ли отправить заявку (4.6): нет блокировки, не друзья, нет активной заявки, цель `active`;
    встречная заявка принимается автоматически."""
    if state.is_self:
        return SendDecision.SELF
    if not state.target_active or state.blocked:
        return SendDecision.HIDDEN
    if state.friends:
        return SendDecision.ALREADY_FRIENDS
    if state.pending is Direction.OUTGOING:
        return SendDecision.ALREADY_SENT
    if state.pending is Direction.INCOMING:
        return SendDecision.ACCEPT_INCOMING
    return SendDecision.CREATE


class ResponseDecision(StrEnum):
    ALLOWED = "allowed"
    NOT_FOUND = "not_found"
    """`404`: заявка чужая или её нет (так чужие идентификаторы не проверить перебором)."""
    NOT_PENDING = "not_pending"
    """`409 friend_request_not_pending`: заявка уже получила исход."""


def decide_response(
    *, is_addressee: bool, status: FriendRequestStatus, other_active: bool
) -> ResponseDecision:
    """Принять или отклонить может только получатель (отменить только отправитель: тот же вопрос с
    `is_addressee` отправителя). Если другая сторона не `active`, заявка для зрителя исчезла."""
    if not is_addressee or not other_active:
        return ResponseDecision.NOT_FOUND
    if status is not FriendRequestStatus.PENDING:
        return ResponseDecision.NOT_PENDING
    return ResponseDecision.ALLOWED


# --------------------------------------------------------------------------- блокировка
class BlockDecision(StrEnum):
    ALLOWED = "allowed"
    SELF = "self"
    """`400 self_action`."""
    HIDDEN = "hidden"
    """`404 not_found`: такого аккаунта нет, он не `active` либо сам заблокировал действующего."""


def decide_block(*, is_self: bool, target_active: bool, blocked_by_target: bool) -> BlockDecision:
    """Заблокировать можно любого, кого для вас не скрывают.

    Скрыты несуществующий и неактивный аккаунты и тот, кто заблокировал вас: его профиль для вас `404`
    (4.6), и блокировка отвечает так же, ничего нового не раскрывая. Поэтому взаимной блокировки не
    бывает: блокирует тот, кто успел первым.
    """
    if is_self:
        return BlockDecision.SELF
    if not target_active or blocked_by_target:
        return BlockDecision.HIDDEN
    return BlockDecision.ALLOWED


# --------------------------------------------------------------------------- подписка
class FollowDecision(StrEnum):
    FOLLOW = "follow"
    """Профиль открыт: подписка появляется сразу (`200`, `following`, событие `FollowCreated`)."""
    REQUEST = "request"
    """Профиль закрыт: создаётся запрос (`200`, `requested`, событие `FollowRequested`)."""
    ALREADY_FOLLOWING = "already_following"
    """Подписка уже есть: `200`, `following`, событий нет."""
    ALREADY_REQUESTED = "already_requested"
    """Запрос уже ждёт ответа: `200`, `requested`, событий нет."""
    SELF = "self"
    """`400 self_action`."""
    HIDDEN = "hidden"
    """`404 not_found`: цели нет, она не `active` или есть блокировка в любую сторону."""


@dataclass(frozen=True, slots=True)
class FollowState:
    """Факты о паре «подписывающийся и цель» на момент решения.

    Пару читают под замком, а закрытость профиля цели под `FOR SHARE` на строке профиля, взятым
    раньше замка пары (порядок блокировок в `social.commands.follows`).
    """

    is_self: bool
    target_active: bool
    blocked: bool
    """Блокировка в любую сторону."""
    following: bool
    """Подписка уже подтверждена."""
    requested: bool
    """Запрос на подписку ждёт ответа."""
    target_private: bool
    """Профиль цели закрыт."""


def decide_follow(state: FollowState) -> FollowDecision:
    """Можно ли подписаться (4.6, 5.4): нет блокировки, цель `active`; открытый профиль даёт подписку
    сразу, закрытый запрос, на который отвечает владелец.

    Дружба на решение не влияет: у закрытого профиля друзья тоже идут через запрос. Подписка и
    ждущий запрос одной пары вместе не бывают (команды их не создают); если они всё же встретились,
    подписка главнее запроса, а оба главнее закрытости профиля: ответ остаётся честным и второй
    подписки или второго запроса не появляется.
    """
    if state.is_self:
        return FollowDecision.SELF
    if not state.target_active or state.blocked:
        return FollowDecision.HIDDEN
    if state.following:
        return FollowDecision.ALREADY_FOLLOWING
    if state.requested:
        return FollowDecision.ALREADY_REQUESTED
    return FollowDecision.REQUEST if state.target_private else FollowDecision.FOLLOW


def decide_follow_request_response(
    *, is_owner: bool, status: FollowRequestStatus, follower_active: bool
) -> ResponseDecision:
    """Одобрить или отклонить запрос на подписку может только владелец профиля, и только ждущий.

    Чужой, несуществующий запрос и запрос от аккаунта не `active` для владельца неотличимы от
    отсутствующих (`404`): чужие идентификаторы не проверить перебором, а ушедший человек для
    владельца исчез. Запрос, который уже получил исход, это `409 follow_request_not_pending`.
    """
    if not is_owner or not follower_active:
        return ResponseDecision.NOT_FOUND
    if status is not FollowRequestStatus.PENDING:
        return ResponseDecision.NOT_PENDING
    return ResponseDecision.ALLOWED


# --------------------------------------------------------------------------- списки друзей и подписчиков
class ListAccess(StrEnum):
    ALLOWED = "allowed"
    NOT_FOUND = "not_found"
    """`404`: нет такого человека, он не `active` или блокировка в любую сторону."""
    PROFILE_PRIVATE = "profile_private"
    """`403 profile_private`: профиль закрыт, зритель не друг и не подписчик."""
    LIST_HIDDEN = "list_hidden"
    """`403 list_hidden`: настройка владельца не пускает зрителя к списку."""


def decide_list_access(
    relation: Relation, *, owner_active: bool, is_private: bool, visibility: ListVisibility
) -> ListAccess:
    """Список друзей, подписчиков или подписок чужого профиля (4.6, 5.3).

    Владелец видит свои списки всегда. Если закрыты и профиль, и список, называется первая причина:
    закрытость профиля (она объясняет, почему не видно ничего). Подписчики и подписки живут под
    одной настройкой владельца `followers_list_visibility`: отдельной для подписок нет.
    """
    if not can_view_profile(relation, owner_active=owner_active):
        return ListAccess.NOT_FOUND
    if relation is Relation.SELF:
        return ListAccess.ALLOWED
    if not sees_details(relation, is_private=is_private):
        return ListAccess.PROFILE_PRIVATE
    if not can_see_counter(relation, visibility=visibility):
        return ListAccess.LIST_HIDDEN
    return ListAccess.ALLOWED
