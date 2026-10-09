"""Поиск людей `GET /search/users` (5.7, 4.10): ник и отображаемое имя на `pg_trgm`.

Порядок выдачи (4.10): 0 точное совпадение ника, 1 префикс ника, 2 сходство по нику и имени. Внутри
группы по убыванию сходства, затем по нику и идентификатору: порядок полный, поэтому страницы со
смещением ничего не теряют и не повторяют, пока данные не меняются.

Как сравниваются имена (замеры на русских данных большого набора, раздел 3 заметок S8):

- «ё» и «е» одна буква: имя и запрос проходят `translate(…, 'ёЁ', 'еЕ')`, регистр снимают триграммы;
- слова имени можно называть в любом порядке: триграммы строятся по словам, `Петров Иван` и
  `иван петров` дают один и тот же набор (сходство 1,0);
- кандидаты те, у кого `similarity ≥ 0,2` либо `word_similarity ≥ 0,6` (запрос похож на слово или
  начало слова имени: «ив» находит «Иван Петров», «ивонов» находит «Иванов»);
- мера сходства: `similarity + word_similarity`, лучшая из ника и имени. Слово целиком выше
  начала слова, короткое имя выше длинного.

Фильтры в запросе, а не после него (страницы не пустеют): аккаунт `active`, не сам зритель, нет
блокировки в любую сторону. Наружу идёт только категория `basic` (⚖️): `UserSummary`.
"""

# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
# (SQLAlchemy не выводит тип строки у `select(*колонки)`: границы функций описаны точно)

import uuid
from typing import Any

from sqlalchemy import (
    Select,
    Text,
    case,
    cast,
    exists,
    func,
    literal,
    literal_column,
    or_,
    select,
    true,
    union_all,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from messunjerr.social.infra.directory import ACTIVE, profiles, users
from messunjerr.social.infra.models import BlockRow
from messunjerr.social.queries.cards import CARD_COLUMNS, summary_of
from messunjerr.social.queries.friends import escape_like
from messunjerr.social.queries.relationships import relationships_for
from messunjerr.social.queries.search_models import UserSearchItem, UserSearchPage

MIN_QUERY_LENGTH = 2
MAX_QUERY_LENGTH = 50
DEFAULT_LIMIT = 20
MAX_LIMIT = 50
MAX_DEPTH = 200
"""Глубже 200 результатов не листают: `offset + limit ≤ 200` (5.7)."""

SIMILARITY_THRESHOLD = 0.2
"""Порог `similarity` для кандидатов. По умолчанию у `pg_trgm` 0,3; замер на 5 000 русских имён с одной
опечаткой внутри слова: слово-оригинал есть в первых двадцати результатах у 84% запросов при 0,3, у 92% при
0,25 и у 98% при 0,2 (ценой вдвое большей выдачи). Порог ставится в начале запроса (`set_config(…, true)`:
только на время транзакции), потому что оператор `%` читает его из настройки."""

WORD_SIMILARITY_THRESHOLD = 0.6
"""Порог `word_similarity` (умолчание `pg_trgm`, но записан явно): ниже он начал бы пропускать куски
середины слов, выше терял бы двухбуквенное начало слова («ив» даёт 0,667): при 0,7 полнота таких запросов
падала с 1,00 до 0,01."""

SEARCH_SETTINGS = {
    "pg_trgm.similarity_threshold": str(SIMILARITY_THRESHOLD),
    "pg_trgm.word_similarity_threshold": str(WORD_SIMILARITY_THRESHOLD),
    "cpu_operator_cost": "0.05",
    "plan_cache_mode": "force_custom_plan",
}
"""Настройки на время транзакции поиска (`set_config(…, true)`), каждая по причине:

- порог триграмм (выше);
- `cpu_operator_cost` (по умолчанию 0,0025): функции `pg_trgm` тяжелее обычных операторов (около 14 мкс
  на имя, замер), а планировщик считает их дешёвыми и для запроса из нескольких слов выбирает
  последовательное чтение всей таблицы с этим фильтром (66 мс на 5 000 людей вместо 5 мс по GIN).
  Двадцатикратная цена оператора возвращает индекс, на остальные запросы поиска не влияет;
- `plan_cache_mode`: подготовленный запрос asyncpg после пяти выполнений мог бы перейти на общий план,
  а он не знает значений параметров и не видит ни префикса, ни селективности триграмм."""

type SearchRows = Select[Any, Any, Any, Any, Any, Any]
"""Строки страницы: `card_id`, `username`, `display_name`, `avatar_asset_id`, `rank_class`, `score`."""

_YO = literal_column("'ёЁ'")
_YE = literal_column("'еЕ'")
"""Литералы, а не параметры: индекс по имени построен по выражению `translate(display_name, 'ёЁ',
'еЕ')`, и общий план подготовленного запроса сопоставляет выражения буква в букву."""


def fold_query(text: str) -> str:
    """Строка поиска в виде, в котором её сравнивают: строчные буквы, «ё» как «е»."""
    return text.lower().replace("ё", "е")


_LOGIN_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_")


def is_searchable(folded: str) -> bool:
    """Есть ли в строке хоть что-то, по чему ищут: буква, цифра или `_`. Из одной пунктуации
    (`--`, `!!`) нельзя составить ни триграмму, ни начало ника: результат пуст, базу не трогаем."""
    return any(char.isalnum() or char == "_" for char in folded)


def may_match_login(folded: str) -> bool:
    """Ник состоит из `[a-z0-9_]`: у строки без единого такого символа (русский запрос) с ним нет ни
    общей триграммы, ни общего начала, и поиск по нику только зря читал бы таблицу `users`."""
    return any(char in _LOGIN_CHARS for char in folded)


def _folded_name() -> ColumnElement[str]:
    return func.translate(profiles.c.display_name, _YO, _YE)


def _login() -> ColumnElement[str]:
    """Ник как `text`: он хранится в нижнем регистре (`CHECK username_format`), поэтому индексы
    `ix_users_username_*` построены по `username::text` без `lower()`."""
    return cast(users.c.username, Text)


def search_statement(viewer_id: uuid.UUID, q: str, *, limit: int, offset: int) -> SearchRows:
    """Запрос страницы: карточки `CARD_COLUMNS`, группа `rank_class` и мера `score`.

    Кандидатов ищут два запроса, каждый по своему индексу: по нику и по имени (условие «ИЛИ» через две
    таблицы индекс бы не использовало). Каждый сразу применяет фильтры, считает группу и меру только по
    своему полю и оставляет первые `offset + limit` строк; затем `UNION ALL` и `GROUP BY` берут у
    человека, найденного по обоим полям, лучшую меру. Так дорогие функции `pg_trgm` вызываются один
    раз на кандидата, а в соединение с карточками попадает не больше `2 × (offset + limit)` строк.
    Скрытых блокировкой исключает маленький набор `hidden` (блокировки зрителя в обе стороны):
    соединение по нему хэшем, а не проверка «ИЛИ» на каждого кандидата.
    """
    folded = fold_query(q)
    needle = literal(folded, type_=Text)
    prefix = literal(f"{escape_like(folded)}%", type_=Text)
    name, login = _folded_name(), _login()
    # При равной мере ники идут побайтово (`COLLATE "C"`), а не по правилам локали базы: в `en_US` знак
    # подчёркивания при сравнении не считается, и порядок зависел бы от того, как создана база.
    login_order = login.collate("C")

    hidden = union_all(
        select(BlockRow.blocked_id.label("id")).where(BlockRow.blocker_id == viewer_id),
        select(BlockRow.blocker_id.label("id")).where(BlockRow.blocked_id == viewer_id),
    ).cte("hidden")
    is_prefix = login.like(prefix, escape="\\")  # точное совпадение тоже начинается с запроса
    rank_class = case((login == needle, 0), (is_prefix, 1), else_=2)
    # У ников-префиксов мера одна и дешёвая: запрос вроде `bi` даёт пять тысяч кандидатов по префиксу
    # ника (замер: полная мера на каждого стоила 60 мс). Полная идёт только у остальных.
    by_login_score = case(
        (is_prefix, func.similarity(login, needle)),
        else_=func.similarity(login, needle) + func.word_similarity(needle, login),
    )
    by_name_score = case(
        (is_prefix, func.similarity(login, needle)),
        else_=func.similarity(name, needle) + func.word_similarity(needle, name),
    )
    visible = (
        users.c.status == ACTIVE,
        users.c.id != viewer_id,
        ~exists().where(hidden.c.id == users.c.id),
    )
    keep = offset + limit

    def source(score: ColumnElement[float], *conditions: ColumnElement[bool], named: bool):
        statement = select(
            users.c.id.label("id"), rank_class.label("rank_class"), score.label("score")
        )
        if named:
            statement = statement.select_from(profiles).join(
                users, users.c.id == profiles.c.user_id
            )
        return (
            statement.where(*visible, or_(*conditions))
            .order_by(rank_class, score.desc(), login_order, users.c.id)
            .limit(keep)
        )

    sources = [source(by_name_score, name.op("%")(needle), needle.op("<%")(name), named=True)]
    if may_match_login(folded):
        sources.append(
            source(
                by_login_score,
                login == needle,
                is_prefix,
                login.op("%")(needle),
                needle.op("<%")(login),
                named=False,
            )
        )
    matches = (sources[0] if len(sources) == 1 else union_all(*sources)).subquery("matches")
    best = (
        select(
            matches.c.id,
            func.min(matches.c.rank_class).label("rank_class"),
            func.max(matches.c.score).label("score"),
        )
        .group_by(matches.c.id)
        .subquery("best")
    )
    return (
        select(*CARD_COLUMNS, best.c.rank_class, best.c.score)
        .select_from(best)
        .join(users, users.c.id == best.c.id)
        .join(profiles, profiles.c.user_id == users.c.id)
        .order_by(best.c.rank_class, best.c.score.desc(), login_order, users.c.id)
        .limit(limit)
        .offset(offset)
    )


async def search_users(
    session: AsyncSession, *, viewer_id: uuid.UUID, q: str, limit: int, offset: int
) -> UserSearchPage:
    """Страница поиска. `q` уже проверен и приведён к NFC (`api.search`); глубину проверил вызывающий."""
    if not is_searchable(fold_query(q)):
        return UserSearchPage(items=[], next_offset=None)
    await session.execute(
        select(
            *(
                func.set_config(literal(name), literal(value), true())
                for name, value in SEARCH_SETTINGS.items()
            )
        )
    )
    rows = (
        await session.execute(search_statement(viewer_id, q, limit=limit + 1, offset=offset))
    ).all()
    page = rows[:limit]
    relationships = await relationships_for(session, viewer_id, [row.card_id for row in page])
    return UserSearchPage(
        items=[
            UserSearchItem(user=summary_of(row), relationship=relationships[row.card_id])
            for row in page
        ],
        next_offset=(offset + limit if len(rows) > limit and offset + limit < MAX_DEPTH else None),
    )
