"""Политики подписок (S8, 4.6, 5.4): решение о подписке, ответ на запрос, доступ к спискам подписчиков.

Как и в S7, пространства решений малы, и тесты перебирают их целиком. Эталон записан не повтором
цепочки `if` из кода, а условиями «тогда и только тогда» по тексту спецификации: подписка требует,
чтобы цели не скрывали от вас (нет блокировки, аккаунт `active`); открытый профиль даёт подписку
сразу, закрытый запрос; повтор идемпотентен. Тесты написаны раньше команд и ручек.
"""

import itertools
import uuid
from dataclasses import fields
from typing import Any, cast, get_args

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from messunjerr.core.me import ListVisibility
from messunjerr.core.outbox import Outbox
from messunjerr.core.uow import UnitOfWork
from messunjerr.profiles.api_public import ProfileVisibilityListener, Relation
from messunjerr.profiles.infra.stubs import NoFollowsYet
from messunjerr.profiles.services import create_profile_services
from messunjerr.social.commands.follows import OpenedProfileApprovals
from messunjerr.social.domain import events
from messunjerr.social.domain.policies import (
    FollowDecision,
    FollowState,
    ListAccess,
    ResponseDecision,
    decide_follow,
    decide_follow_request_response,
    decide_list_access,
)
from messunjerr.social.domain.rules import FollowRequestStatus, graph_aggregate
from messunjerr.social.services import create_social_services

ALL_FOLLOW_STATES = [
    FollowState(
        is_self=is_self,
        target_active=active,
        blocked=blocked,
        following=following,
        requested=requested,
        target_private=private,
    )
    for is_self, active, blocked, following, requested, private in itertools.product(
        (False, True), repeat=6
    )
]
VISIBILITIES: tuple[ListVisibility, ...] = ("everyone", "friends", "only_me")


# ----------------------------------------------------------------------------- подписка (5.4, 4.6)
def test_the_whole_space_of_follow_decisions_is_covered() -> None:
    assert len(ALL_FOLLOW_STATES) == 64
    assert len(set(ALL_FOLLOW_STATES)) == 64


def test_friendship_is_not_a_fact_of_the_follow_decision() -> None:
    """Дружба на решение не влияет (4.6): закрытый профиль даже друзьям отвечает запросом."""
    assert [field.name for field in fields(FollowState)] == [
        "is_self",
        "target_active",
        "blocked",
        "following",
        "requested",
        "target_private",
    ]


@pytest.mark.parametrize("state", ALL_FOLLOW_STATES, ids=lambda s: repr(s)[12:-1])
def test_a_follow_decision_matches_the_specification_exactly(state: FollowState) -> None:
    """Каждый исход тогда и только тогда, когда выполнено его условие из 5.4 и 4.6."""
    # Цель видна зрителю: это не он сам, аккаунт `active`, блокировки нет ни в одну сторону.
    visible = not state.is_self and state.target_active and not state.blocked
    # Нового эффекта нет, если подписка уже есть или запрос уже ждёт ответа (идемпотентность).
    nothing_to_do = state.following or state.requested
    expected = {
        # `400 self_action`: сам на себя, что бы ещё ни было
        FollowDecision.SELF: state.is_self,
        # `404`: цели нет, она не `active` или блокировка в любую сторону (даже если подписка уже есть)
        FollowDecision.HIDDEN: not state.is_self and (not state.target_active or state.blocked),
        # `200 following`: подписка уже есть
        FollowDecision.ALREADY_FOLLOWING: visible and state.following,
        # `200 requested`: подписки нет, запрос уже ждёт
        FollowDecision.ALREADY_REQUESTED: visible and not state.following and state.requested,
        # открытый профиль: подписка сразу
        FollowDecision.FOLLOW: visible and not nothing_to_do and not state.target_private,
        # закрытый профиль: запрос, ответ владельца превратит его в подписку
        FollowDecision.REQUEST: visible and not nothing_to_do and state.target_private,
    }

    decision = decide_follow(state)

    assert [outcome for outcome, holds in expected.items() if holds] == [decision]


@pytest.mark.parametrize(
    ("state", "decision"),
    [
        (FollowState(False, True, False, False, False, False), FollowDecision.FOLLOW),
        (FollowState(False, True, False, False, False, True), FollowDecision.REQUEST),
        (FollowState(True, True, False, False, False, False), FollowDecision.SELF),
        (FollowState(False, False, False, False, False, False), FollowDecision.HIDDEN),
        (FollowState(False, True, True, False, False, False), FollowDecision.HIDDEN),
        (FollowState(False, True, False, True, False, False), FollowDecision.ALREADY_FOLLOWING),
        (FollowState(False, True, False, True, False, True), FollowDecision.ALREADY_FOLLOWING),
        (FollowState(False, True, False, False, True, True), FollowDecision.ALREADY_REQUESTED),
        # блокировка и неактивная цель сильнее всего остального: ничего не раскрывают
        (FollowState(False, True, True, True, True, True), FollowDecision.HIDDEN),
        (FollowState(False, False, False, True, True, True), FollowDecision.HIDDEN),
        # запрос на открытом профиле (так быть не должно) остаётся запросом, а не второй подпиской
        (FollowState(False, True, False, False, True, False), FollowDecision.ALREADY_REQUESTED),
    ],
)
def test_follow_decisions_for_the_scenarios_of_the_specification(
    state: FollowState, decision: FollowDecision
) -> None:
    assert decide_follow(state) is decision


# ----------------------------------------------------------------------------- ответ на запрос
@pytest.mark.parametrize(
    ("is_owner", "status", "follower_active"),
    list(itertools.product((False, True), list(FollowRequestStatus), (False, True))),
)
def test_a_follow_request_response_matches_the_specification_exactly(
    is_owner: bool, status: FollowRequestStatus, follower_active: bool
) -> None:
    visible = is_owner and follower_active
    expected = {
        # чужой, несуществующий запрос и запрос от скрытого аккаунта не отличить от отсутствующих
        ResponseDecision.NOT_FOUND: not visible,
        # `409 follow_request_not_pending`: запрос уже получил исход
        ResponseDecision.NOT_PENDING: visible and status is not FollowRequestStatus.PENDING,
        ResponseDecision.ALLOWED: visible and status is FollowRequestStatus.PENDING,
    }

    decision = decide_follow_request_response(
        is_owner=is_owner, status=status, follower_active=follower_active
    )

    assert [outcome for outcome, holds in expected.items() if holds] == [decision]


# ----------------------------------------------------------------------------- списки подписчиков
@pytest.mark.parametrize(
    ("relation", "is_private", "visibility", "access"),
    [
        # открытый профиль: настройка владельца решает всё
        (Relation.STRANGER, False, "everyone", ListAccess.ALLOWED),
        (Relation.STRANGER, False, "friends", ListAccess.LIST_HIDDEN),
        (Relation.FOLLOWER, False, "friends", ListAccess.LIST_HIDDEN),
        (Relation.FRIEND, False, "friends", ListAccess.ALLOWED),
        (Relation.FRIEND, False, "only_me", ListAccess.LIST_HIDDEN),
        # закрытый профиль: чужой не видит ничего, подписчик видит детали, но не список по настройке
        (Relation.STRANGER, True, "everyone", ListAccess.PROFILE_PRIVATE),
        (Relation.FOLLOWER, True, "everyone", ListAccess.ALLOWED),
        (Relation.FOLLOWER, True, "friends", ListAccess.LIST_HIDDEN),
        (Relation.FOLLOWER, True, "only_me", ListAccess.LIST_HIDDEN),
        (Relation.FRIEND, True, "friends", ListAccess.ALLOWED),
        # владелец видит свои списки всегда, заблокированный ничего
        (Relation.SELF, True, "only_me", ListAccess.ALLOWED),
        (Relation.BLOCKED, False, "everyone", ListAccess.NOT_FOUND),
    ],
)
def test_the_followers_lists_follow_the_setting_of_the_owner_and_the_privacy_of_the_profile(
    relation: Relation, is_private: bool, visibility: ListVisibility, access: ListAccess
) -> None:
    """Подписчики и подписки живут под настройкой `followers_list_visibility` и той же политикой, что и друзья."""
    decision = decide_list_access(
        relation, owner_active=True, is_private=is_private, visibility=visibility
    )

    assert decision is access


def test_an_owner_who_is_not_active_hides_every_list_whatever_the_relation() -> None:
    for relation, visibility, private in itertools.product(
        list(Relation), VISIBILITIES, (False, True)
    ):
        assert (
            decide_list_access(
                relation, owner_active=False, is_private=private, visibility=visibility
            )
            is ListAccess.NOT_FOUND
        )


# ----------------------------------------------------------------------------- события подписок
class _CapturingSession:
    """Сеанс, который только запоминает добавленные строки: хватает, чтобы увидеть, что пишет `record`."""

    def __init__(self) -> None:
        self.added: list[Any] = []

    def add(self, row: Any) -> None:
        self.added.append(row)


def _written(event: events.GraphEvent, actor: uuid.UUID) -> Any:
    session = _CapturingSession()
    events.record(Outbox(cast(AsyncSession, session)), event, actor_id=actor)
    (row,) = session.added
    return row


FOLLOWER, FOLLOWEE, REQUEST = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
FOLLOW_EVENTS: list[events.GraphEvent] = [
    events.FollowCreated(follower_id=FOLLOWER, followee_id=FOLLOWEE),
    events.FollowRequested(request_id=REQUEST, follower_id=FOLLOWER, followee_id=FOLLOWEE),
    events.FollowRequestResponded(
        request_id=REQUEST,
        follower_id=FOLLOWER,
        followee_id=FOLLOWEE,
        decision=events.FollowRequestDecision.APPROVED,
    ),
    events.FollowRequestResponded(
        request_id=REQUEST,
        follower_id=FOLLOWER,
        followee_id=FOLLOWEE,
        decision=events.FollowRequestDecision.DECLINED,
    ),
    events.FollowRemoved(follower_id=FOLLOWER, followee_id=FOLLOWEE),
]


@pytest.mark.parametrize("event", FOLLOW_EVENTS, ids=lambda e: type(e).__name__)
def test_a_follow_event_goes_to_the_graph_topic_under_the_key_of_the_pair(
    event: events.GraphEvent,
) -> None:
    row = _written(event, FOLLOWEE)

    low, high = sorted([FOLLOWER, FOLLOWEE], key=lambda value: value.int)
    assert row.topic == events.TOPIC_GRAPH == "mj.social.graph.v1"
    assert row.key == f"{low}:{high}" == graph_aggregate(FOLLOWEE, FOLLOWER)
    assert row.event_type == type(event).__name__
    # Совершивший действие лежит в заголовках (поле конверта 6.4), а не в теле события.
    assert row.headers["actor_id"] == str(FOLLOWEE)
    assert "actor_id" not in row.payload


@pytest.mark.parametrize("event", FOLLOW_EVENTS, ids=lambda e: type(e).__name__)
def test_a_follow_event_carries_identifiers_and_the_decision_only(
    event: events.GraphEvent,
) -> None:
    """⚖️ В теле только идентификаторы и перечисление: ни имён, ни почты, всё строками."""
    payload = _written(event, FOLLOWER).payload

    assert all(isinstance(value, str) for value in payload.values())
    allowed = {"request_id", "follower_id", "followee_id", "decision"}
    assert set(payload) <= allowed
    assert payload["follower_id"] == str(FOLLOWER)
    assert payload["followee_id"] == str(FOLLOWEE)
    if isinstance(event, events.FollowRequestResponded):
        assert payload["decision"] == event.decision.value
        assert payload["decision"] in {"approved", "declined"}
    if isinstance(event, events.FollowCreated | events.FollowRemoved):
        assert set(payload) == {"follower_id", "followee_id"}
    if isinstance(event, events.FollowRequested):
        assert set(payload) == {"request_id", "follower_id", "followee_id"}


def test_the_key_of_the_pair_does_not_depend_on_who_follows_whom() -> None:
    forward = _written(events.FollowRemoved(follower_id=FOLLOWER, followee_id=FOLLOWEE), FOLLOWER)
    backward = _written(events.FollowRemoved(follower_id=FOLLOWEE, followee_id=FOLLOWER), FOLLOWER)

    assert forward.key == backward.key


def test_every_graph_event_can_be_recorded() -> None:
    """Событие, добавленное в `GraphEvent`, обязано уметь назвать пару: иначе `record` падает в бою."""
    first, second, request = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    samples: list[events.GraphEvent] = [
        events.FriendRequestSent(request_id=request, sender_id=first, receiver_id=second),
        events.FriendRequestResponded(
            request_id=request,
            sender_id=first,
            receiver_id=second,
            decision=events.Decision.ACCEPTED,
        ),
        events.FriendshipRemoved(user_low_id=first, user_high_id=second),
        events.UserBlocked(blocker_id=first, blocked_id=second),
        events.UserUnblocked(blocker_id=first, blocked_id=second),
        events.FollowCreated(follower_id=first, followee_id=second),
        events.FollowRequested(request_id=request, follower_id=first, followee_id=second),
        events.FollowRequestResponded(
            request_id=request,
            follower_id=first,
            followee_id=second,
            decision=events.FollowRequestDecision.DECLINED,
        ),
        events.FollowRemoved(follower_id=first, followee_id=second),
    ]
    union: Any = events.GraphEvent
    assert {type(sample) for sample in samples} == set(get_args(union.__value__))

    for sample in samples:
        row = _written(sample, first)
        assert row.event_type == type(sample).__name__
        assert row.key == graph_aggregate(first, second)


# ----------------------------------------------------------------------------- порт открытия профиля
class _RecordingListener:
    def __init__(self) -> None:
        self.opened: list[uuid.UUID] = []

    async def profile_opened(self, uow: UnitOfWork, *, owner_id: uuid.UUID) -> None:
        self.opened.append(owner_id)


async def test_without_a_social_graph_opening_a_profile_does_nothing() -> None:
    stub: ProfileVisibilityListener = NoFollowsYet()
    uow: Any = None  # заглушка к БД не обращается

    assert await stub.profile_opened(uow, owner_id=uuid.uuid4()) is None


def test_profile_services_get_the_stub_until_the_graph_is_plugged_in() -> None:
    assert isinstance(create_profile_services().visibility, NoFollowsYet)
    listener = _RecordingListener()

    assert create_profile_services(visibility=listener).visibility is listener


def test_the_social_services_bring_the_listener_that_the_application_plugs_in() -> None:
    """Корень приложения передаёт профилям `social.visibility`: без него открытие профиля ничего не одобряет."""
    social = create_social_services()
    plugged = create_profile_services(visibility=social.visibility)

    assert isinstance(social.visibility, OpenedProfileApprovals)
    assert plugged.visibility is social.visibility
