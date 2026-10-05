"""Свойства матрицы доступа к профилю (4.6): hypothesis проверяет инварианты на всех сочетаниях.

Это каркас property-тестов спринта S3: S7 добавит к нему правила дружбы и блокировок, S11 видимость
постов. Пространство мало (пять отношений, два вида профиля, три настройки списков и даты), поэтому
hypothesis перебирает его почти целиком, а инварианты читаются как формулировки правил.
"""

from hypothesis import given, settings
from hypothesis import strategies as st

from messunjerr.core.me import BIRTH_DATE_VISIBILITIES, LIST_VISIBILITIES, ListVisibility
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

relations = st.sampled_from(list(Relation))
other_than_self = st.sampled_from([r for r in Relation if r is not Relation.SELF])
list_visibilities: st.SearchStrategy[ListVisibility] = st.sampled_from(
    ["everyone", "friends", "only_me"]
)
birth_visibilities = st.sampled_from(BIRTH_DATE_VISIBILITIES)
private = st.booleans()

# От дальнего круга к ближнему: всё, что видит дальний, видит и ближний.
CLOSENESS = [Relation.STRANGER, Relation.FOLLOWER, Relation.FRIEND]
BIRTH_RANK = {BirthDateView.HIDDEN: 0, BirthDateView.DAY_MONTH: 1, BirthDateView.FULL: 2}
LIST_RANK = {"only_me": 0, "friends": 1, "everyone": 2}

property_settings = settings(max_examples=300, deadline=None, database=None)


def test_the_strategies_cover_the_enumerations_of_the_schema() -> None:
    assert set(LIST_RANK) == set(LIST_VISIBILITIES)
    assert {view.value for view in BirthDateView} == set(BIRTH_DATE_VISIBILITIES)


@property_settings
@given(is_private=private, visibility=list_visibilities, birth=birth_visibilities)
def test_a_blocked_viewer_sees_nothing(
    is_private: bool, visibility: ListVisibility, birth: str
) -> None:
    relation = Relation.BLOCKED
    assert not can_view_profile(relation, owner_active=True)
    assert not sees_details(relation, is_private=is_private)
    assert not can_see_posts_counter(relation, is_private=is_private)
    assert not can_see_counter(relation, visibility=visibility)
    assert not can_see_list(relation, is_private=is_private, visibility=visibility)
    assert (
        birth_date_view(relation, is_private=is_private, visibility=birth) is BirthDateView.HIDDEN
    )


@property_settings
@given(is_private=private, visibility=list_visibilities, birth=birth_visibilities)
def test_the_owner_sees_everything_of_their_own_profile(
    is_private: bool, visibility: ListVisibility, birth: str
) -> None:
    relation = Relation.SELF
    assert can_view_profile(relation, owner_active=True)
    assert sees_details(relation, is_private=is_private)
    assert can_see_posts_counter(relation, is_private=is_private)
    assert can_see_counter(relation, visibility=visibility)
    assert can_see_list(relation, is_private=is_private, visibility=visibility)
    assert birth_date_view(relation, is_private=is_private, visibility=birth) is BirthDateView.FULL


@property_settings
@given(relation=relations)
def test_nobody_sees_the_profile_of_an_inactive_account(relation: Relation) -> None:
    assert not can_view_profile(relation, owner_active=False)


@property_settings
@given(
    near=st.integers(min_value=1, max_value=len(CLOSENESS) - 1),
    is_private=private,
    visibility=list_visibilities,
    birth=birth_visibilities,
)
def test_a_closer_relation_never_sees_less(
    near: int, is_private: bool, visibility: ListVisibility, birth: str
) -> None:
    """Друг видит не меньше подписчика, подписчик не меньше постороннего."""
    far_relation, near_relation = CLOSENESS[near - 1], CLOSENESS[near]
    assert sees_details(far_relation, is_private=is_private) <= sees_details(
        near_relation, is_private=is_private
    )
    assert can_see_posts_counter(far_relation, is_private=is_private) <= can_see_posts_counter(
        near_relation, is_private=is_private
    )
    assert can_see_counter(far_relation, visibility=visibility) <= can_see_counter(
        near_relation, visibility=visibility
    )
    assert can_see_list(far_relation, is_private=is_private, visibility=visibility) <= can_see_list(
        near_relation, is_private=is_private, visibility=visibility
    )
    assert (
        BIRTH_RANK[birth_date_view(far_relation, is_private=is_private, visibility=birth)]
        <= BIRTH_RANK[birth_date_view(near_relation, is_private=is_private, visibility=birth)]
    )


@property_settings
@given(relation=other_than_self, visibility=list_visibilities, birth=birth_visibilities)
def test_opening_a_profile_never_hides_anything(
    relation: Relation, visibility: ListVisibility, birth: str
) -> None:
    assert sees_details(relation, is_private=True) <= sees_details(relation, is_private=False)
    assert can_see_list(relation, is_private=True, visibility=visibility) <= can_see_list(
        relation, is_private=False, visibility=visibility
    )
    assert (
        BIRTH_RANK[birth_date_view(relation, is_private=True, visibility=birth)]
        <= BIRTH_RANK[birth_date_view(relation, is_private=False, visibility=birth)]
    )


@property_settings
@given(
    relation=relations,
    is_private=private,
    tight=list_visibilities,
    loose=list_visibilities,
)
def test_a_looser_list_setting_never_hides_anything(
    relation: Relation, is_private: bool, tight: ListVisibility, loose: ListVisibility
) -> None:
    if LIST_RANK[tight] > LIST_RANK[loose]:
        tight, loose = loose, tight
    assert can_see_counter(relation, visibility=tight) <= can_see_counter(
        relation, visibility=loose
    )
    assert can_see_list(relation, is_private=is_private, visibility=tight) <= can_see_list(
        relation, is_private=is_private, visibility=loose
    )


@property_settings
@given(relation=other_than_self, is_private=private)
def test_only_me_hides_the_lists_from_everyone_but_the_owner(
    relation: Relation, is_private: bool
) -> None:
    assert not can_see_counter(relation, visibility="only_me")
    assert not can_see_list(relation, is_private=is_private, visibility="only_me")


@property_settings
@given(relation=other_than_self, is_private=private, birth=birth_visibilities)
def test_others_never_see_more_of_the_birth_date_than_the_owner_allowed(
    relation: Relation, is_private: bool, birth: str
) -> None:
    allowed = {"hidden": 0, "day_month": 1, "full": 2}[birth]
    shown = BIRTH_RANK[birth_date_view(relation, is_private=is_private, visibility=birth)]
    assert shown <= allowed


@property_settings
@given(relation=other_than_self, visibility=list_visibilities)
def test_a_list_is_never_visible_without_its_counter_being_visible(
    relation: Relation, visibility: ListVisibility
) -> None:
    for is_private in (False, True):
        if can_see_list(relation, is_private=is_private, visibility=visibility):
            assert can_see_counter(relation, visibility=visibility)
            assert sees_details(relation, is_private=is_private)
