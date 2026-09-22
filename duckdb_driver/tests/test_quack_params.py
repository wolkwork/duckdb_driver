import datetime
import decimal
import uuid
from typing import Any

import duckdb
import pytest
from sqlalchemy.engine.url import make_url

from duckdb_driver.quack import QuackDialect, render_parameters, to_sql_literal


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        False,
        0,
        -42,
        2**70,
        1.5,
        -0.1,
        decimal.Decimal("123.4500"),
        "",
        "plain",
        "it's got 'quotes'",
        'double "quotes" and \\ backslash',
        "?, $1, -- not a comment",
        "unicode ✓ 🦆",
        b"\x00\x01binary\xff",
        datetime.date(2024, 2, 29),
        datetime.datetime(2024, 2, 29, 13, 45, 1, 123456),
        datetime.time(13, 45, 1, 5),
        datetime.timedelta(days=3, seconds=5, microseconds=7),
        uuid.UUID("12345678-1234-5678-1234-567812345678"),
        [1, 2, 3],
        ["a", "b'c"],
        {"a": 1, "b": "x"},
    ],
)
def test_literal_roundtrip(value: Any) -> None:
    (result,) = duckdb.execute(f"SELECT {to_sql_literal(value)}").fetchone()  # type: ignore[misc]
    assert result == value


def test_literal_tz_datetime() -> None:
    value = datetime.datetime(2024, 1, 1, 12, 0, tzinfo=datetime.timezone.utc)
    conn = duckdb.connect()
    conn.execute("SET TimeZone = 'UTC'")
    (result,) = conn.execute(f"SELECT {to_sql_literal(value)}").fetchone()  # type: ignore[misc]
    assert result == value


def test_literal_special_floats() -> None:
    nan, inf, ninf = duckdb.execute(
        f"SELECT {to_sql_literal(float('nan'))}, {to_sql_literal(float('inf'))}, "
        f"{to_sql_literal(float('-inf'))}"
    ).fetchone()  # type: ignore[misc]
    assert nan != nan
    assert inf == float("inf")
    assert ninf == float("-inf")


def test_literal_unsupported_type() -> None:
    with pytest.raises(TypeError):
        to_sql_literal(object())


def test_render_qmark() -> None:
    assert (
        render_parameters("SELECT ?, '?', \"?\" -- ?\n, ?", (1, "a"))
        == "SELECT 1, '?', \"?\" -- ?\n, 'a'"
    )


def test_render_numeric_dollar() -> None:
    assert (
        render_parameters("SELECT $2, $1, '$1', /* $1 */ $10", list(range(1, 11)))
        == "SELECT 2, 1, '$1', /* $1 */ 10"
    )


def test_render_named() -> None:
    assert (
        render_parameters("SELECT $a, $b, '$a'", {"a": 1, "b": "it's"})
        == "SELECT 1, 'it''s', '$a'"
    )


def test_render_dollar_quoted_string() -> None:
    assert (
        render_parameters("SELECT $$ ? $1 $$, $tag$ ? $tag$, ?", ("x",))
        == "SELECT $$ ? $1 $$, $tag$ ? $tag$, 'x'"
    )


def test_render_no_parameters() -> None:
    assert render_parameters("SELECT '?'", None) == "SELECT '?'"
    assert render_parameters("SELECT ?", ()) == "SELECT ?"


def test_render_missing_parameters() -> None:
    with pytest.raises(IndexError):
        render_parameters("SELECT ?, ?", (1,))
    with pytest.raises(IndexError):
        render_parameters("SELECT $2", (1,))
    with pytest.raises(KeyError):
        render_parameters("SELECT $b", {"a": 1})


def test_rendered_statement_executes() -> None:
    sql = render_parameters(
        "SELECT $1 || $2 AS s, $3 AS d", ("it's ", "fine", datetime.date(2020, 1, 1))
    )
    assert duckdb.execute(sql).fetchone() == ("it's fine", datetime.date(2020, 1, 1))


@pytest.mark.parametrize(
    "url, expected",
    [
        (
            "quack://localhost",
            {"host": "localhost", "port": None, "token": None, "execution": "remote"},
        ),
        (
            "quack://:s3cret@db.example.com:9000",
            {"host": "db.example.com", "port": 9000, "token": "s3cret"},
        ),
        (
            "duckdb+quack://user:s3cret@host/analytics?execution=local&alias=srv",
            {
                "host": "host",
                "token": "s3cret",
                "remote_database": "analytics",
                "execution": "local",
                "alias": "srv",
            },
        ),
        (
            "quack://host/?token=abcd&disable_ssl=true&threads=4",
            {"token": "abcd", "disable_ssl": "true", "url_config": {"threads": "4"}},
        ),
    ],
)
def test_create_connect_args(url: str, expected: dict) -> None:
    _, opts = QuackDialect().create_connect_args(make_url(url))
    for key, value in expected.items():
        assert opts[key] == value, key


def test_create_connect_args_token_twice() -> None:
    with pytest.raises(ValueError):
        QuackDialect().create_connect_args(make_url("quack://:a@host/?token=b"))
