"""Политики профиля (4.6): таблицы решений. Свойства матрицы проверяет соседний файл (hypothesis)."""

import pytest

from messunjerr.core.me import ListVisibility
from messunjerr.profiles.domain.policies import (
    BirthDateView,
    Relation,
    birth_date_view,
    can_see_counter,
    can_see_list,
    can_see_posts_counter,
    can_view_profile,
    sees_details,
)

S, F, W, X, B = (
    Relation.SELF,
    Relation.FRIEND,
    Relation.FOLLOWER,
    Relation.STRANGER,
    Relation.BLOCKED,
)


@pytest.mark.parametrize(
    ("relation", "active", "expected"),
    [
        (S, True, True),
        (F, True, True),
        (W, True, True),
        (X, True, True),
        (B, True, False),  # заблокирован в любую сторону: 404
        (S, False, False),  # аккаунт не `active` не видит никто, даже сам владелец (ручка закрыта)
        (X, False, False),
        (F, False, False),
    ],
)
def test_who_can_view_a_profile_at_all(relation: Relation, active: bool, expected: bool) -> None:
    assert can_view_profile(relation, owner_active=active) is expected


@pytest.mark.parametrize(
    ("relation", "is_private", "expected"),
    [
        (S, False, True),
        (F, False, True),
        (W, False, True),
        (X, False, True),
        (B, False, False),
        (S, True, True),
        (F, True, True),
        (W, True, True),
        (X, True, False),  # закрытый профиль: посторонний видит только карточку
        (B, True, False),
    ],
)
def test_details_beyond_the_card(relation: Relation, is_private: bool, expected: bool) -> None:
    assert sees_details(relation, is_private=is_private) is expected
    assert can_see_posts_counter(relation, is_private=is_private) is expected


@pytest.mark.parametrize("visibility", ["hidden", "day_month", "full"])
@pytest.mark.parametrize("is_private", [False, True])
def test_the_owner_always_sees_the_full_birth_date(visibility: str, is_private: bool) -> None:
    view = birth_date_view(S, is_private=is_private, visibility=visibility)
    assert view is BirthDateView.FULL


@pytest.mark.parametrize(
    ("relation", "is_private", "visibility", "expected"),
    [
        (X, False, "hidden", BirthDateView.HIDDEN),
        (X, False, "day_month", BirthDateView.DAY_MONTH),
        (X, False, "full", BirthDateView.FULL),
        (F, False, "day_month", BirthDateView.DAY_MONTH),
        (W, False, "full", BirthDateView.FULL),
        (X, True, "full", BirthDateView.HIDDEN),  # закрытый профиль: чужим дата скрыта целиком
        (X, True, "day_month", BirthDateView.HIDDEN),
        (F, True, "full", BirthDateView.FULL),
        (W, True, "day_month", BirthDateView.DAY_MONTH),
        (F, True, "hidden", BirthDateView.HIDDEN),
        (B, False, "full", BirthDateView.HIDDEN),
        (B, True, "full", BirthDateView.HIDDEN),
    ],
)
def test_birth_date_follows_the_owners_choice_within_what_the_viewer_may_see(
    relation: Relation, is_private: bool, visibility: str, expected: BirthDateView
) -> None:
    assert birth_date_view(relation, is_private=is_private, visibility=visibility) is expected


def test_an_unknown_birth_date_visibility_hides_the_date() -> None:
    assert birth_date_view(X, is_private=False, visibility="whatever") is BirthDateView.HIDDEN


@pytest.mark.parametrize(
    ("relation", "visibility", "expected"),
    [
        (S, "only_me", True),
        (S, "friends", True),
        (S, "everyone", True),
        (F, "everyone", True),
        (F, "friends", True),
        (F, "only_me", False),
        (W, "everyone", True),
        (W, "friends", False),
        (W, "only_me", False),
        (X, "everyone", True),
        (X, "friends", False),
        (X, "only_me", False),
        (B, "everyone", False),
        (B, "friends", False),
        (B, "only_me", False),
    ],
)
def test_counters_follow_the_owners_list_setting(
    relation: Relation, visibility: ListVisibility, expected: bool
) -> None:
    assert can_see_counter(relation, visibility=visibility) is expected


def test_a_private_profile_does_not_hide_counters_but_hides_the_lists_themselves() -> None:
    """Счётчик закрытого профиля виден по настройке владельца, а список только своим кругом (5.3)."""
    assert can_see_counter(X, visibility="everyone") is True
    assert can_see_list(X, is_private=True, visibility="everyone") is False
    assert can_see_list(X, is_private=False, visibility="everyone") is True
    assert can_see_list(W, is_private=True, visibility="everyone") is True
    assert can_see_list(F, is_private=True, visibility="friends") is True
    assert can_see_list(W, is_private=True, visibility="friends") is False
    assert can_see_list(S, is_private=True, visibility="only_me") is True
