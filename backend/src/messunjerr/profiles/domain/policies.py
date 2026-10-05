"""Политики профиля (4.6): кто что видит. Чистые функции; запросы и команды зовут только их.

Отношение зрителя V к владельцу O: `self`, `friend`, `follower` (подписка подтверждена), `stranger`,
`blocked` (любая из сторон заблокировала другую). Социальный граф появится в S7–S8, поэтому пока
настоящие отношения только `self` и `stranger`; матрица ниже рассчитана на все пять и покрыта
property-тестами, чтобы S7 подключил граф, а не переписывал правила.

Что видит зритель (4.6, 5.3):

- заблокированный не видит ничего: профиль для него `404`; аккаунт не `active` не видит никто;
- имя, ник, аватар, био и `is_private` видят все, кому профиль виден вообще;
- ссылки, город, язык, часовой пояс, дату рождения и счётчик постов (вместе с самими постами) видят
  владелец, друзья и подписчики, а у открытого профиля ещё и посторонние;
- дату рождения показывают в объёме `birth_date_visibility` (владельцу целиком);
- счётчики друзей, подписчиков и подписок подчиняются настройке владельца: `everyone`, `friends`
  (только друзья), `only_me`; закрытость профиля счётчики не скрывает, но сами списки у закрытого
  профиля чужим недоступны (`profile_private`); владелец всегда видит своё.
"""

from enum import StrEnum

from messunjerr.core.me import ListVisibility


class Relation(StrEnum):
    SELF = "self"
    FRIEND = "friend"
    FOLLOWER = "follower"
    STRANGER = "stranger"
    BLOCKED = "blocked"


class BirthDateView(StrEnum):
    """В каком объёме зритель видит дату рождения."""

    HIDDEN = "hidden"
    DAY_MONTH = "day_month"
    FULL = "full"


_INNER_CIRCLE = frozenset({Relation.SELF, Relation.FRIEND, Relation.FOLLOWER})


def can_view_profile(relation: Relation, *, owner_active: bool) -> bool:
    """Виден ли профиль вообще. Если нет, ответ `404`: существование скрыто (4.6)."""
    return owner_active and relation is not Relation.BLOCKED


def sees_details(relation: Relation, *, is_private: bool) -> bool:
    """Видит ли зритель то, что выходит за рамки карточки: ссылки, город, дату рождения, посты.

    У открытого профиля это все, кроме заблокированных; у закрытого владелец, друзья и подписчики.
    """
    if relation is Relation.BLOCKED:
        return False
    return not is_private or relation in _INNER_CIRCLE


def birth_date_view(relation: Relation, *, is_private: bool, visibility: str) -> BirthDateView:
    """Дата рождения: владельцу целиком, остальным по `birth_date_visibility`, если они видят детали."""
    if relation is Relation.SELF:
        return BirthDateView.FULL
    if not sees_details(relation, is_private=is_private):
        return BirthDateView.HIDDEN
    match visibility:
        case "full":
            return BirthDateView.FULL
        case "day_month":
            return BirthDateView.DAY_MONTH
        case _:
            return BirthDateView.HIDDEN


def can_see_counter(relation: Relation, *, visibility: ListVisibility) -> bool:
    """Счётчик друзей, подписчиков или подписок по настройке владельца (закрытость не влияет)."""
    if relation is Relation.BLOCKED:
        return False
    if relation is Relation.SELF:
        return True
    match visibility:
        case "everyone":
            return True
        case "friends":
            return relation is Relation.FRIEND
        case "only_me":
            return False


def can_see_list(relation: Relation, *, is_private: bool, visibility: ListVisibility) -> bool:
    """Сам список друзей, подписчиков или подписок: настройка владельца и видимость деталей профиля."""
    return can_see_counter(relation, visibility=visibility) and (
        relation is Relation.SELF or sees_details(relation, is_private=is_private)
    )


def can_see_posts_counter(relation: Relation, *, is_private: bool) -> bool:
    """Счётчик постов: у закрытого профиля скрыт от посторонних вместе с самими постами (5.3)."""
    return sees_details(relation, is_private=is_private)
