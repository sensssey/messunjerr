"""Политики социального графа (4.6, 5.4): решения о заявках, ответах, блокировках и списках друзей.

Пространства решений малы, поэтому тесты перебирают их целиком, а не выборочно. Эталон записан не
повтором цепочки `if` из кода, а как условия «тогда и только тогда» по тексту спецификации: каждый
исход имеет полное описание, исходы не пересекаются, и вместе они покрывают все сочетания. Тесты
пишутся раньше кода ручек (риск S7: матрица доступа самое дорогое место для ошибок).
"""

import itertools
import uuid

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from messunjerr.core.me import ListVisibility
from messunjerr.profiles.api_public import Relation
from messunjerr.social.domain.policies import (
    BlockDecision,
    ListAccess,
    PairState,
    ResponseDecision,
    SendDecision,
    decide_block,
    decide_friend_request,
    decide_list_access,
    decide_response,
)
from messunjerr.social.domain.rules import (
    Direction,
    FriendRequestStatus,
    graph_aggregate,
    ordered_pair,
)
from messunjerr.social.infra.repositories import pair_lock_key

PENDING_STATES = (None, Direction.OUTGOING, Direction.INCOMING)
ALL_PAIR_STATES = [
    PairState(
        is_self=is_self, target_active=active, blocked=blocked, friends=friends, pending=pending
    )
    for is_self, active, blocked, friends, pending in itertools.product(
        (False, True), (False, True), (False, True), (False, True), PENDING_STATES
    )
]
VISIBILITIES: tuple[ListVisibility, ...] = ("everyone", "friends", "only_me")


# ----------------------------------------------------------------------------- заявка в друзья (5.4)
def test_the_whole_space_of_friend_request_decisions_is_covered() -> None:
    assert len(ALL_PAIR_STATES) == 48


@pytest.mark.parametrize("state", ALL_PAIR_STATES, ids=lambda s: repr(s)[10:-1])
def test_a_friend_request_decision_matches_the_specification_exactly(state: PairState) -> None:
    """Каждый исход тогда и только тогда, когда выполнено его условие из 5.4 и 4.6."""
    others_fine = not state.is_self and state.target_active and not state.blocked
    expected = {
        # `400 self_action`: сам себе, что бы ещё ни было
        SendDecision.SELF: state.is_self,
        # `404`: цели нет, она не `active` или блокировка в любую сторону (даже если уже друзья)
        SendDecision.HIDDEN: not state.is_self and (not state.target_active or state.blocked),
        # `409 already_friends`
        SendDecision.ALREADY_FRIENDS: others_fine and state.friends,
        # `409 friend_request_exists`: ваша заявка уже ждёт ответа
        SendDecision.ALREADY_SENT: others_fine
        and not state.friends
        and state.pending is Direction.OUTGOING,
        # встречная заявка принимается автоматически
        SendDecision.ACCEPT_INCOMING: others_fine
        and not state.friends
        and state.pending is Direction.INCOMING,
        # новая заявка: ничего не мешает и заявок нет
        SendDecision.CREATE: others_fine and not state.friends and state.pending is None,
    }

    decision = decide_friend_request(state)

    assert [outcome for outcome, holds in expected.items() if holds] == [decision]


@pytest.mark.parametrize(
    ("state", "decision"),
    [
        (PairState(False, True, False, False), SendDecision.CREATE),
        (PairState(True, True, False, False), SendDecision.SELF),
        (PairState(False, False, False, False), SendDecision.HIDDEN),
        (PairState(False, True, True, False), SendDecision.HIDDEN),
        (PairState(False, True, False, True), SendDecision.ALREADY_FRIENDS),
        (PairState(False, True, False, False, Direction.OUTGOING), SendDecision.ALREADY_SENT),
        (PairState(False, True, False, False, Direction.INCOMING), SendDecision.ACCEPT_INCOMING),
        # блокировка сильнее дружбы и заявок: ничего не раскрывает
        (PairState(False, True, True, True, Direction.INCOMING), SendDecision.HIDDEN),
    ],
)
def test_friend_request_decisions_for_the_scenarios_of_the_specification(
    state: PairState, decision: SendDecision
) -> None:
    assert decide_friend_request(state) is decision


# ----------------------------------------------------------------------------- ответ на заявку
@pytest.mark.parametrize(
    ("is_addressee", "status", "other_active"),
    list(itertools.product((False, True), list(FriendRequestStatus), (False, True))),
)
def test_a_response_decision_matches_the_specification_exactly(
    is_addressee: bool, status: FriendRequestStatus, other_active: bool
) -> None:
    visible = is_addressee and other_active
    expected = {
        # чужую, несуществующую или от скрытого аккаунта заявку не отличить от отсутствующей
        ResponseDecision.NOT_FOUND: not visible,
        # `409 friend_request_not_pending`: заявка уже получила исход
        ResponseDecision.NOT_PENDING: visible and status is not FriendRequestStatus.PENDING,
        ResponseDecision.ALLOWED: visible and status is FriendRequestStatus.PENDING,
    }

    decision = decide_response(is_addressee=is_addressee, status=status, other_active=other_active)

    assert [outcome for outcome, holds in expected.items() if holds] == [decision]


# ----------------------------------------------------------------------------- блокировка
@pytest.mark.parametrize(
    ("is_self", "target_active", "blocked_by_target"),
    list(itertools.product((False, True), repeat=3)),
)
def test_a_block_decision_matches_the_specification_exactly(
    is_self: bool, target_active: bool, blocked_by_target: bool
) -> None:
    """Скрытого человека (нет аккаунта, он не `active`, он сам заблокировал вас) заблокировать нельзя."""
    expected = {
        BlockDecision.SELF: is_self,
        BlockDecision.HIDDEN: not is_self and (not target_active or blocked_by_target),
        BlockDecision.ALLOWED: not is_self and target_active and not blocked_by_target,
    }

    decision = decide_block(
        is_self=is_self, target_active=target_active, blocked_by_target=blocked_by_target
    )

    assert [outcome for outcome, holds in expected.items() if holds] == [decision]


# ----------------------------------------------------------------------------- списки друзей (5.3, 4.6)
@pytest.mark.parametrize(
    ("relation", "owner_active", "is_private", "visibility"),
    list(itertools.product(list(Relation), (False, True), (False, True), VISIBILITIES)),
)
def test_a_list_access_decision_matches_the_specification_exactly(
    relation: Relation, owner_active: bool, is_private: bool, visibility: ListVisibility
) -> None:
    """Список друзей чужого профиля: 404, 403 `profile_private`, 403 `list_hidden` либо доступ."""
    visible = owner_active and relation is not Relation.BLOCKED
    # Закрытый профиль открывает детали владельцу, друзьям и подписчикам, но не чужим.
    details = relation in {Relation.SELF, Relation.FRIEND, Relation.FOLLOWER} or not is_private
    # Настройка владельца: `everyone` всем, `friends` только друзьям, `only_me` никому (кроме него).
    allowed_by_setting = (
        relation is Relation.SELF
        or visibility == "everyone"
        or (visibility == "friends" and relation is Relation.FRIEND)
    )
    expected = {
        ListAccess.NOT_FOUND: not visible,
        ListAccess.ALLOWED: visible
        and (relation is Relation.SELF or (details and allowed_by_setting)),
        # закрытость профиля называется первой: она объясняет, почему не видно ничего
        ListAccess.PROFILE_PRIVATE: visible and relation is not Relation.SELF and not details,
        ListAccess.LIST_HIDDEN: visible
        and relation is not Relation.SELF
        and details
        and not allowed_by_setting,
    }

    decision = decide_list_access(
        relation, owner_active=owner_active, is_private=is_private, visibility=visibility
    )

    assert [outcome for outcome, holds in expected.items() if holds] == [decision]


# ----------------------------------------------------------------------------- пара людей
uuids = st.uuids()
pair_settings = settings(max_examples=300, deadline=None, database=None)


@pair_settings
@given(first=uuids, second=uuids)
def test_a_pair_is_stored_in_one_order_whoever_comes_first(
    first: uuid.UUID, second: uuid.UUID
) -> None:
    low, high = ordered_pair(first, second)

    assert (low, high) == ordered_pair(second, first)
    assert {low, high} == {first, second}
    assert low.int <= high.int
    assert graph_aggregate(first, second) == graph_aggregate(second, first) == f"{low}:{high}"


@pair_settings
@given(first=uuids, second=uuids)
def test_the_pair_lock_is_the_same_for_both_directions_and_fits_a_bigint(
    first: uuid.UUID, second: uuid.UUID
) -> None:
    key = pair_lock_key(first, second)

    assert key == pair_lock_key(second, first)
    assert -(2**63) <= key < 2**63


def test_the_pair_order_matches_the_byte_order_postgresql_uses_for_uuid() -> None:
    """`CHECK (user_low_id < user_high_id)` в БД и `ordered_pair` в коде не должны расходиться."""
    smaller = uuid.UUID("00000000-0000-7000-8000-000000000001")
    larger = uuid.UUID("ffffffff-ffff-7fff-bfff-ffffffffffff")

    assert ordered_pair(larger, smaller) == (smaller, larger)
    assert smaller.bytes < larger.bytes
