"""
Integration tests for the Quack client/server dialect.

A Quack server is started in a separate process on a free localhost port. The
tests are skipped when the quack extension cannot be installed/loaded (e.g.
DuckDB < 1.5 or no access to the extension repository).

Environment variables to run against locally built binaries:

* ``QUACK_EXTENSION_PATH``: an (unsigned) quack extension to install first.
* ``QUACK_SERVER_BINARY``: a DuckDB CLI to run the server with, instead of
  the Python DuckDB package.
"""

import datetime
import os
import socket
import subprocess
import sys
import time
from typing import Any, Dict, Generator, List

import duckdb
import pytest
import sqlalchemy
from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    inspect,
    select,
    text,
)
from sqlalchemy.dialects import registry  # type: ignore
from sqlalchemy.engine import Engine

from duckdb_driver.quack import QuackConnectionWrapper

# entry points aren't picked up by every SQLAlchemy version in editable installs
registry.register("quack", "duckdb_driver.quack", "QuackDialect")
registry.register("duckdb.quack", "duckdb_driver.quack", "QuackDialect")

TOKEN = "super_secret_token"
EXTENSION_PATH = os.environ.get("QUACK_EXTENSION_PATH")
SERVER_BINARY = os.environ.get("QUACK_SERVER_BINARY")

# executes one statement per stdin line, keeps running until stdin is closed
PYTHON_SERVER = """
import sys, duckdb
conn = duckdb.connect(config={"allow_unsigned_extensions": True})
for line in sys.stdin:
    if line.strip():
        conn.execute(line)
"""


def _config() -> Dict[str, Any]:
    return {"allow_unsigned_extensions": True} if EXTENSION_PATH else {}


def _connect_args(**kwargs: Any) -> Dict[str, Any]:
    return {"config": _config(), **kwargs}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _client() -> duckdb.DuckDBPyConnection:
    """A plain DuckDB client, independent of the dialect under test."""
    conn = duckdb.connect(config=_config())
    if EXTENSION_PATH:
        conn.execute(f"INSTALL '{EXTENSION_PATH}'")
    conn.execute("INSTALL quack")
    conn.execute("LOAD quack")
    return conn


def server_query(server: Dict[str, Any], sql: str) -> List[Any]:
    """Run ``sql`` on the server through the stateless ``quack_query``."""
    return (
        server["client"]
        .execute("FROM quack_query(?, ?, token = ?)", [server["uri"], sql, TOKEN])
        .fetchall()
    )


SETUP_SQL = [
    "CREATE TABLE sales AS SELECT i AS id, 'region_' || (i % 3) AS region, "
    "i * 1.5 AS amount FROM range(100) t(i)",
    "CREATE TABLE regions AS SELECT * FROM (VALUES ('region_0', 'North'), "
    "('region_1', 'South'), ('region_2', 'East')) v(region, name)",
    # a macro that exists only on the server: proves where a query runs
    "CREATE MACRO server_only() AS 'executed on server'",
    "CREATE SCHEMA analytics",
    "CREATE TABLE analytics.metrics (k VARCHAR, v INTEGER)",
    "INSERT INTO analytics.metrics VALUES ('a', 1), ('b', 2)",
    "ATTACH ':memory:' AS other_db",
    "CREATE TABLE other_db.only_here AS SELECT 7 AS x",
    "CREATE TABLE ready AS SELECT 1 AS ok",
]


@pytest.fixture(scope="module")
def quack_server() -> Generator[Dict[str, Any], None, None]:
    try:
        client = _client()
    except duckdb.Error as e:
        pytest.skip(f"quack extension not available: {e}")
    # older DuckDB versions ship a quack extension without these functions
    functions = {"quack_serve", "quack_query", "quack_query_by_name"}
    available = client.execute(
        "SELECT DISTINCT function_name FROM duckdb_functions() "
        "WHERE function_name IN ('quack_serve', 'quack_query', 'quack_query_by_name')"
    ).fetchall()
    if {name for (name,) in available} != functions:
        client.close()
        pytest.skip(f"quack extension of DuckDB {duckdb.__version__} is not supported")

    port = _free_port()
    uri = f"quack:localhost:{port}"
    statements = []
    if EXTENSION_PATH:
        statements.append(f"INSTALL '{EXTENSION_PATH}';")
    statements += [
        "LOAD quack;",
        f"CALL quack_serve('{uri}', token = '{TOKEN}');",
        *(f"{s};" for s in SETUP_SQL),
    ]
    cmd = (
        [SERVER_BINARY, "-unsigned"]
        if SERVER_BINARY
        else [sys.executable, "-c", PYTHON_SERVER]
    )
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdin is not None
    proc.stdin.write("\n".join(statements) + "\n")
    proc.stdin.flush()

    server = {"port": port, "uri": uri, "client": client}
    deadline = time.monotonic() + 60
    while True:
        try:
            if server_query(server, "SELECT ok FROM ready") == [(1,)]:
                break
        except duckdb.Error as e:
            last_error = e
        if proc.poll() is not None or time.monotonic() > deadline:
            proc.kill()
            stderr = proc.stderr.read() if proc.stderr else ""
            pytest.fail(f"quack server did not start: {last_error} {stderr}")
        time.sleep(0.2)

    try:
        yield server
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        client.close()


def _url(server: Dict[str, Any], token: str = TOKEN, **query: str) -> str:
    qs = "&".join(f"{k}={v}" for k, v in query.items())
    return f"quack://:{token}@localhost:{server['port']}/" + (f"?{qs}" if qs else "")


@pytest.fixture
def engine(quack_server: Dict[str, Any]) -> Generator[Engine, None, None]:
    eng = create_engine(_url(quack_server), connect_args=_connect_args())
    yield eng
    eng.dispose()


@pytest.fixture
def local_engine(quack_server: Dict[str, Any]) -> Generator[Engine, None, None]:
    eng = create_engine(
        _url(quack_server, execution="local"), connect_args=_connect_args()
    )
    yield eng
    eng.dispose()


def test_select(engine: Engine) -> None:
    with engine.connect() as conn:
        assert conn.execute(text("SELECT 42")).scalar() == 42
        assert conn.execute(text("SELECT count(*) FROM sales")).scalar() == 100


def test_query_runs_on_server(engine: Engine) -> None:
    with engine.connect() as conn:
        assert conn.execute(text("SELECT server_only()")).scalar() == (
            "executed on server"
        )


def test_local_execution_mode(local_engine: Engine) -> None:
    with local_engine.connect() as conn:
        # tables of the server are visible through the attached catalog...
        assert conn.execute(text("SELECT count(*) FROM sales")).scalar() == 100
        assert conn.execute(text("SELECT current_database()")).scalar() == "quack"
        # ... but the query is planned locally, so server-only macros are unknown
        with pytest.raises(sqlalchemy.exc.DBAPIError):
            conn.execute(text("SELECT server_only()"))


def test_duckdb_quack_scheme(quack_server: Dict[str, Any]) -> None:
    eng = create_engine(
        _url(quack_server).replace("quack://", "duckdb+quack://"),
        connect_args=_connect_args(),
    )
    with eng.connect() as conn:
        assert conn.execute(text("SELECT server_only()")).scalar() is not None
    assert eng.dialect.name == "duckdb"
    assert eng.url.get_backend_name() == "duckdb"
    eng.dispose()


def test_token_via_connect_args(quack_server: Dict[str, Any]) -> None:
    eng = create_engine(
        f"quack://localhost:{quack_server['port']}",
        connect_args=_connect_args(token=TOKEN),
    )
    with eng.connect() as conn:
        assert conn.execute(text("SELECT 1")).scalar() == 1
    eng.dispose()


def test_wrong_token_is_rejected(quack_server: Dict[str, Any]) -> None:
    eng = create_engine(
        _url(quack_server, token="wrong_token"), connect_args=_connect_args()
    )
    with pytest.raises(sqlalchemy.exc.DBAPIError):
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
    eng.dispose()


def test_missing_token_is_rejected(quack_server: Dict[str, Any]) -> None:
    eng = create_engine(
        f"quack://localhost:{quack_server['port']}", connect_args=_connect_args()
    )
    with pytest.raises(sqlalchemy.exc.DBAPIError):
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
    eng.dispose()


def test_unreachable_server(quack_server: Dict[str, Any]) -> None:
    eng = create_engine(
        f"quack://:{TOKEN}@localhost:{_free_port()}", connect_args=_connect_args()
    )
    with pytest.raises(sqlalchemy.exc.DBAPIError):
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
    eng.dispose()


def test_disable_ssl(quack_server: Dict[str, Any]) -> None:
    # quack_serve listens on plain HTTP: forcing TLS must fail, disabling it works
    for value, ok in (("true", True), ("false", False)):
        eng = create_engine(
            _url(quack_server, disable_ssl=value), connect_args=_connect_args()
        )
        try:
            if ok:
                with eng.connect() as conn:
                    assert conn.execute(text("SELECT 1")).scalar() == 1
            else:
                with pytest.raises(sqlalchemy.exc.DBAPIError):
                    eng.connect()
        finally:
            eng.dispose()


def test_invalid_execution_mode(quack_server: Dict[str, Any]) -> None:
    eng = create_engine(_url(quack_server, execution="bogus"))
    with pytest.raises(ValueError, match="execution mode"):
        eng.connect()


def test_bound_parameters(engine: Engine) -> None:
    with engine.connect() as conn:
        result = conn.execute(
            text(
                "SELECT count(*), max(amount) FROM sales "
                "WHERE region = :region AND id < :max_id"
            ),
            {"region": "region_1", "max_id": 10},
        ).fetchone()
        assert result is not None
        assert result[0] == 3
        assert float(result[1]) == 10.5

        tricky = "it's -- not ? a $1 comment"
        assert conn.execute(text("SELECT :v"), {"v": tricky}).scalar() == tricky
        day = datetime.date(2024, 2, 29)
        assert conn.execute(text("SELECT :d"), {"d": day}).scalar() == day


def test_sql_expression_language(engine: Engine) -> None:
    pytest.importorskip("sqlalchemy", "1.4.0")
    sales = Table(
        "sales",
        MetaData(),
        Column("id", Integer),
        Column("region", String),
    )
    stmt = (
        select(sales.c.region, sqlalchemy.func.count().label("n"))
        .where(sales.c.id >= 10)
        .group_by(sales.c.region)
        .order_by(sales.c.region)
    )
    with engine.connect() as conn:
        rows = conn.execute(stmt).fetchall()
    assert [tuple(r) for r in rows] == [
        ("region_0", 30),
        ("region_1", 30),
        ("region_2", 30),
    ]


def test_join_of_remote_tables(engine: Engine) -> None:
    # joins run on the server in remote mode (Quack's attach mode cannot run
    # multiple remote scans in a single query)
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT r.name, count(*) FROM sales s JOIN regions r USING (region) "
                "GROUP BY ALL ORDER BY 1"
            )
        ).fetchall()
    assert [tuple(r) for r in rows] == [("East", 33), ("North", 34), ("South", 33)]


def test_description(engine: Engine) -> None:
    raw = engine.raw_connection()
    try:
        cursor = raw.cursor()
        cursor.execute("SELECT id, region, amount FROM sales ORDER BY id LIMIT 3", ())
        assert [d[0] for d in cursor.description] == ["id", "region", "amount"]
        assert cursor.fetchmany(2) == [(0, "region_0", 0.0), (1, "region_1", 1.5)]
        assert cursor.fetchall() == [(2, "region_2", 3.0)]
    finally:
        raw.close()


def test_reflection(engine: Engine) -> None:
    pytest.importorskip("sqlalchemy", "1.4.0")
    insp = inspect(engine)
    assert "sales" in insp.get_table_names()
    assert "metrics" in insp.get_table_names(schema="analytics")
    assert "memory.analytics" in insp.get_schema_names()
    assert insp.has_table("sales")
    assert not insp.has_table("does_not_exist")
    columns = {c["name"]: c for c in insp.get_columns("sales")}
    assert set(columns) == {"id", "region", "amount"}
    assert isinstance(columns["region"]["type"], sqlalchemy.String)


def test_ddl_and_dml(engine: Engine, quack_server: Dict[str, Any]) -> None:
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE written_by_client (i INTEGER, s VARCHAR)"))
        conn.execute(
            text("INSERT INTO written_by_client VALUES (:i, :s)"),
            [{"i": 1, "s": "a"}, {"i": 2, "s": "b"}],
        )
    rows = server_query(quack_server, "SELECT * FROM written_by_client ORDER BY i")
    assert rows == [(1, "a"), (2, "b")]
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE written_by_client"))


def test_rollback(engine: Engine, quack_server: Dict[str, Any]) -> None:
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE rolled_back (i INTEGER)"))
    conn = engine.connect()
    trans = conn.begin()
    conn.execute(text("INSERT INTO rolled_back VALUES (1)"))
    assert conn.execute(text("SELECT count(*) FROM rolled_back")).scalar() == 1
    trans.rollback()
    conn.close()
    assert server_query(quack_server, "SELECT count(*) FROM rolled_back") == [(0,)]
    with engine.begin() as c:
        c.execute(text("DROP TABLE rolled_back"))


def test_orm_style_table_roundtrip(engine: Engine) -> None:
    pytest.importorskip("sqlalchemy", "1.4.0")
    metadata = MetaData()
    table = Table(
        "orm_roundtrip",
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=False),
        Column("name", String),
    )
    metadata.create_all(engine)
    try:
        with engine.begin() as conn:
            conn.execute(
                table.insert(), [{"id": 1, "name": "x"}, {"id": 2, "name": "y"}]
            )
        with engine.connect() as conn:
            rows = conn.execute(select(table).order_by(table.c.id)).fetchall()
        assert [tuple(r) for r in rows] == [(1, "x"), (2, "y")]
    finally:
        metadata.drop_all(engine)


def test_remote_database(quack_server: Dict[str, Any]) -> None:
    eng = create_engine(
        f"quack://:{TOKEN}@localhost:{quack_server['port']}/other_db",
        connect_args=_connect_args(),
    )
    try:
        with eng.connect() as conn:
            assert conn.execute(text("SELECT current_database()")).scalar() == (
                "other_db"
            )
            assert conn.execute(text("SELECT x FROM only_here")).scalar() == 7
    finally:
        eng.dispose()


def test_pool_pre_ping(quack_server: Dict[str, Any]) -> None:
    eng = create_engine(
        _url(quack_server), connect_args=_connect_args(), pool_pre_ping=True
    )
    for _ in range(3):
        with eng.connect() as conn:
            assert conn.execute(text("SELECT 1")).scalar() == 1
    eng.dispose()


def test_connection_wrapper_type(engine: Engine) -> None:
    raw = engine.raw_connection()
    try:
        assert isinstance(raw.dbapi_connection, QuackConnectionWrapper)  # type: ignore[attr-defined]
    except AttributeError:  # SQLAlchemy < 2 / 1.4 naming
        assert isinstance(raw.connection, QuackConnectionWrapper)  # type: ignore[attr-defined]
    finally:
        raw.close()
