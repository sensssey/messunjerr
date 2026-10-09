"""Чужие таблицы, которые читает social (`social/infra/directory.py`), описаны вручную.

import-linter не видит SQL, поэтому сверка с настоящими моделями identity и profiles лежит здесь:
переименование столбца или схемы в чужом контексте краснит этот тест, а не списки друзей в бою.
"""

from typing import cast

import pytest
from sqlalchemy import Table, TableClause

from messunjerr.identity.infra.models import USER_STATUSES, UserRow
from messunjerr.profiles.infra.models import ProfileRow
from messunjerr.social.infra import directory

PAIRS: list[tuple[str, TableClause, Table]] = [
    ("users", directory.users, cast(Table, UserRow.__table__)),
    ("profiles", directory.profiles, cast(Table, ProfileRow.__table__)),
]


@pytest.mark.parametrize(
    ("light", "table"), [(light, table) for _, light, table in PAIRS], ids=[p[0] for p in PAIRS]
)
def test_the_light_tables_describe_real_columns(light: TableClause, table: Table) -> None:
    assert (light.schema, light.name) == (table.schema, table.name)
    for column in light.columns:
        assert column.name in table.columns, f"в {table.fullname} нет столбца {column.name}"
        assert column.type.python_type is table.columns[column.name].type.python_type, column.name


def test_the_active_status_is_a_status_the_users_table_allows() -> None:
    assert directory.ACTIVE in USER_STATUSES
