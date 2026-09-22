"""
SQLAlchemy dialect for talking to a remote DuckDB over the Quack protocol.

See https://duckdb.org/quack/ for the protocol itself. A URL looks like::

    quack://:<token>@<host>[:<port>][?execution=remote|local&alias=...]

The client is a local in-memory DuckDB that ``ATTACH``es the server. Two
execution modes are supported:

``remote`` (default)
    Every statement is shipped verbatim to the server through
    ``quack_query_by_name`` and fully executed there; only the result travels
    back. Bind parameters are rendered as SQL literals on the client, as the
    Quack protocol has no notion of bind parameters.

``local``
    The server is attached and made the default catalog (``USE``). Queries are
    planned and executed by the local DuckDB; the Quack scan pushes projections
    and simple filters down to the server, but joins/aggregations run locally.
"""

import datetime
import decimal
import math
import re
import uuid
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Type,
    Union,
)

import duckdb
from sqlalchemy import pool
from sqlalchemy.engine.url import URL

from . import ConnectionWrapper, CursorWrapper, Dialect

if TYPE_CHECKING:
    from sqlalchemy.base import Connection

__all__ = ["QuackDialect", "render_parameters", "to_sql_literal"]

EXECUTION_MODES = ("remote", "local")
DEFAULT_ALIAS = "quack"

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _quote_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _to_bool(name: str, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    lowered = str(value).strip().lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise ValueError(f"Invalid boolean value for {name!r}: {value!r}")


def to_sql_literal(value: Any) -> str:
    """Render a Python value as a DuckDB SQL literal."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "'nan'::DOUBLE"
        if math.isinf(value):
            return "'inf'::DOUBLE" if value > 0 else "'-inf'::DOUBLE"
        return repr(value) + "::DOUBLE"
    if isinstance(value, decimal.Decimal):
        if not value.is_finite():
            return f"'{value}'::DOUBLE"
        return str(value)
    if isinstance(value, str):
        return _quote_string(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "'" + "".join(f"\\x{b:02X}" for b in bytes(value)) + "'::BLOB"
    if isinstance(value, datetime.datetime):
        if value.tzinfo is not None:
            return f"TIMESTAMPTZ '{value.isoformat(sep=' ')}'"
        return f"TIMESTAMP '{value.isoformat(sep=' ')}'"
    if isinstance(value, datetime.date):
        return f"DATE '{value.isoformat()}'"
    if isinstance(value, datetime.time):
        if value.tzinfo is not None:
            return f"TIMETZ '{value.isoformat()}'"
        return f"TIME '{value.isoformat()}'"
    if isinstance(value, datetime.timedelta):
        micros = (
            value.days * 86_400_000_000 + value.seconds * 1_000_000 + value.microseconds
        )
        return f"to_microseconds({micros})"
    if isinstance(value, uuid.UUID):
        return f"'{value}'::UUID"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(to_sql_literal(v) for v in value) + "]"
    if isinstance(value, dict):
        return (
            "{"
            + ", ".join(
                f"{_quote_string(str(k))}: {to_sql_literal(v)}"
                for k, v in value.items()
            )
            + "}"
        )
    raise TypeError(
        f"Cannot send parameter of type {type(value).__name__} over Quack; "
        "only standard Python scalar/date/list/dict values are supported"
    )


def _skip_quoted(sql: str, i: int, quote: str) -> int:
    """Return the index just past the literal/identifier opened at sql[i]."""
    n = len(sql)
    i += 1
    while i < n:
        if sql[i] == quote:
            if i + 1 < n and sql[i + 1] == quote:
                i += 2
                continue
            return i + 1
        i += 1
    return n


_DOLLAR_TAG_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")
_NUMERIC_PARAM_RE = re.compile(r"\$(\d+)")
_NAMED_PARAM_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")


def render_parameters(
    statement: str,
    parameters: Optional[Union[Sequence[Any], Mapping[str, Any]]],
) -> str:
    """
    Inline bind parameters into ``statement``.

    Supports ``?`` (qmark), ``$1`` (numeric_dollar) positional and ``$name``
    named placeholders, skipping string literals, quoted identifiers,
    comments and dollar-quoted strings.
    """
    if parameters is None:
        return statement
    named = isinstance(parameters, Mapping)
    if not named and len(parameters) == 0:
        return statement

    out: List[str] = []
    n = len(statement)
    i = 0
    qmark_index = 0
    while i < n:
        c = statement[i]
        if c in ("'", '"'):
            j = _skip_quoted(statement, i, c)
        elif statement.startswith("--", i):
            j = statement.find("\n", i)
            j = n if j == -1 else j
        elif statement.startswith("/*", i):
            j = statement.find("*/", i + 2)
            j = n if j == -1 else j + 2
        elif c == "?" and not named:
            if qmark_index >= len(parameters):
                raise IndexError("Not enough parameters for statement")
            out.append(to_sql_literal(parameters[qmark_index]))  # type: ignore[index]
            qmark_index += 1
            i += 1
            continue
        elif c == "$":
            m = _NUMERIC_PARAM_RE.match(statement, i)
            if m and not named:
                idx = int(m.group(1)) - 1
                if not 0 <= idx < len(parameters):
                    raise IndexError(f"No parameter for placeholder ${idx + 1}")
                out.append(to_sql_literal(parameters[idx]))  # type: ignore[index]
                i = m.end()
                continue
            tag = _DOLLAR_TAG_RE.match(statement, i)
            if tag:
                end = statement.find(tag.group(0), tag.end())
                j = n if end == -1 else end + len(tag.group(0))
            else:
                m = _NAMED_PARAM_RE.match(statement, i)
                if m and named:
                    key = m.group(1)
                    if key not in parameters:
                        raise KeyError(f"No parameter for placeholder ${key}")
                    out.append(to_sql_literal(parameters[key]))  # type: ignore[index]
                    i = m.end()
                    continue
                j = i + 1
        else:
            j = i + 1
        out.append(statement[i:j])
        i = j
    return "".join(out)


class QuackCursorWrapper(CursorWrapper):
    """Cursor that forwards every statement to the attached Quack server."""

    def __init__(
        self, c: duckdb.DuckDBPyConnection, connection_wrapper: "QuackConnectionWrapper"
    ) -> None:
        super().__init__(c, connection_wrapper)
        self._quack_connection = connection_wrapper

    def execute(
        self,
        statement: str,
        parameters: Optional[Tuple] = None,
        context: Optional[Any] = None,
    ) -> None:
        super().execute(
            self._quack_connection.to_remote(render_parameters(statement, parameters))
        )

    def executemany(
        self,
        statement: str,
        parameters: Optional[List[Dict]] = None,
        context: Optional[Any] = None,
    ) -> None:
        for params in parameters or []:
            self.execute(statement, params)  # type: ignore[arg-type]


class QuackConnectionWrapper(ConnectionWrapper):
    """
    Connection whose statements (and transactions) run on the Quack server.
    """

    def __init__(self, c: duckdb.DuckDBPyConnection, alias: str) -> None:
        super().__init__(c)
        self._raw = c
        self.alias = alias

    def to_remote(self, statement: str) -> str:
        return (
            f"SELECT * FROM quack_query_by_name({_quote_string(self.alias)}, "
            f"{_quote_string(statement)})"
        )

    def cursor(self) -> "QuackCursorWrapper":
        return QuackCursorWrapper(self._raw, self)

    def execute(self, statement: str, parameters: Optional[Tuple] = None) -> Any:
        cursor = self.cursor()
        cursor.execute(statement, parameters)
        return cursor

    def begin(self) -> None:
        self.execute("BEGIN TRANSACTION")

    def commit(self) -> None:
        self._end_transaction("COMMIT")

    def rollback(self) -> None:
        self._end_transaction("ROLLBACK")

    def _end_transaction(self, statement: str) -> None:
        try:
            self.execute(statement)
        except duckdb.Error as e:
            # matches the local driver, where commit/rollback without an
            # active transaction are no-ops
            if "no transaction is active" not in str(e):
                raise


class QuackDialect(Dialect):
    """DuckDB client talking to a remote DuckDB over the Quack protocol."""

    driver = "quack"
    supports_statement_cache = False

    def create_connect_args(self, url: URL) -> Tuple[tuple, dict]:
        query = dict(url.query)
        opts: Dict[str, Any] = {
            "host": url.host or "localhost",
            "port": url.port,
            "token": url.password,
            "remote_database": url.database or None,
            "execution": query.pop("execution", "remote"),
            "alias": query.pop("alias", DEFAULT_ALIAS),
        }
        if "disable_ssl" in query:
            opts["disable_ssl"] = query.pop("disable_ssl")
        if "token" in query:
            if opts["token"]:
                raise ValueError("Pass the Quack token either as password or ?token=")
            opts["token"] = query.pop("token")
        opts["url_config"] = query
        return (), opts

    def connect(self, *cargs: Any, **cparams: Any) -> "Connection":
        host: str = cparams.pop("host", "localhost")
        port: Optional[int] = cparams.pop("port", None)
        token: Optional[str] = cparams.pop("token", None)
        remote_database: Optional[str] = cparams.pop("remote_database", None)
        execution: str = str(cparams.pop("execution", "remote")).lower()
        alias: str = cparams.pop("alias", DEFAULT_ALIAS)
        disable_ssl = cparams.pop("disable_ssl", None)
        # the client itself is always an in-memory database
        cparams.pop("database", None)

        if execution not in EXECUTION_MODES:
            raise ValueError(
                f"Invalid Quack execution mode {execution!r}, "
                f"expected one of {', '.join(EXECUTION_MODES)}"
            )
        if not _IDENTIFIER_RE.match(alias):
            raise ValueError(f"Invalid Quack alias {alias!r}")
        if remote_database and execution == "local":
            raise ValueError(
                "Selecting a remote database is only supported in remote execution mode"
            )

        conn = self._connect_duckdb(":memory:", *cargs, **cparams)
        try:
            _load_quack(conn)
            uri = f"quack:{_format_host(host)}" + (f":{port}" if port else "")
            options = ["TYPE quack"]
            if token:
                options.append(f"TOKEN {_quote_string(token)}")
            if disable_ssl is not None:
                ssl_off = _to_bool("disable_ssl", disable_ssl)
                options.append(f"DISABLE_SSL {'true' if ssl_off else 'false'}")
            conn.execute(
                f"ATTACH {_quote_string(uri)} AS {alias} ({', '.join(options)})"
            )

            if execution == "local":
                conn.execute(f"USE {alias}")
                return ConnectionWrapper(conn)

            wrapper = QuackConnectionWrapper(conn, alias)
            if remote_database:
                wrapper.execute(f"USE {_quote_identifier(remote_database)}")
            return wrapper
        except BaseException:
            conn.close()
            raise

    @classmethod
    def get_pool_class(cls, url: URL) -> Type[pool.Pool]:
        # every DBAPI connection is its own in-memory client database
        return pool.QueuePool


def _format_host(host: str) -> str:
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def _load_quack(conn: duckdb.DuckDBPyConnection) -> None:
    try:
        conn.execute("LOAD quack")
    except duckdb.Error:
        conn.execute("INSTALL quack")
        conn.execute("LOAD quack")
