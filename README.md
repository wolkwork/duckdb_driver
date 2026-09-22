# duckdb_driver

This project is a fork of [duckdb-engine](https://github.com/Mause/duckdb_engine).

[![Supported Python Versions](https://img.shields.io/pypi/pyversions/duckdb-driver)](https://pypi.org/project/duckdb-driver/) [![PyPI version](https://badge.fury.io/py/duckdb-driver.svg)](https://badge.fury.io/py/duckdb-driver) [![PyPI Downloads](https://img.shields.io/pypi/dm/duckdb-driver.svg)](https://pypi.org/project/duckdb-driver/) [![codecov](https://codecov.io/github/wolkwork/duckdb_driver/graph/badge.svg?token=UUZ316JOY0)](https://codecov.io/github/wolkwork/duckdb_driver)

Basic SQLAlchemy driver for [DuckDB](https://duckdb.org/)

<!--ts-->
- [duckdb\_driver](#duckdb_driver)
  - [Installation](#installation)
  - [Usage](#usage)
  - [Usage in IPython/Jupyter](#usage-in-ipythonjupyter)
  - [Configuration](#configuration)
  - [Connecting to a remote DuckDB (Quack)](#connecting-to-a-remote-duckdb-quack)
    - [Execution modes](#execution-modes)
    - [Using with Apache Superset](#using-with-apache-superset)
  - [How to register a pandas DataFrame](#how-to-register-a-pandas-dataframe)
  - [Things to keep in mind](#things-to-keep-in-mind)
    - [Auto-incrementing ID columns](#auto-incrementing-id-columns)
    - [Pandas `read_sql()` chunksize](#pandas-read_sql-chunksize)
    - [Unsigned integer support](#unsigned-integer-support)
  - [Alembic Integration](#alembic-integration)
  - [Preloading extensions (experimental)](#preloading-extensions-experimental)
  - [Registering Filesystems](#registering-filesystems)
  - [The name](#the-name)

<!-- Created by https://github.com/ekalinin/github-markdown-toc -->
<!-- Added by: me, at: Wed 20 Sep 2023 12:44:27 AWST -->

<!--te-->

## Installation
```sh
$ pip install duckdb-driver
```

## Usage

Once you've installed this package, you should be able to just use it, as SQLAlchemy does a python path search

```python
from sqlalchemy import Column, Integer, Sequence, String, create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm.session import Session

Base = declarative_base()


class FakeModel(Base):  # type: ignore
    __tablename__ = "fake"

    id = Column(Integer, Sequence("fakemodel_id_sequence"), primary_key=True)
    name = Column(String)


eng = create_engine("duckdb:///:memory:")
Base.metadata.create_all(eng)
session = Session(bind=eng)

session.add(FakeModel(name="Frank"))
session.commit()

frank = session.query(FakeModel).one()

assert frank.name == "Frank"
```

## Usage in IPython/Jupyter

With IPython-SQL and DuckDB-Driver you can query DuckDB natively in your notebook! Check out [DuckDB's documentation](https://duckdb.org/docs/guides/python/jupyter) or
Alex Monahan's great demo of this on [his blog](https://alex-monahan.github.io/2021/08/22/Python_and_SQL_Better_Together.html#an-example-workflow-with-duckdb).

## Configuration

You can configure DuckDB by passing `connect_args` to the create_engine function
```python
create_engine(
    'duckdb:///:memory:',
    connect_args={
        'read_only': False,
        'config': {
            'memory_limit': '500mb'
        }
    }
)
```

The supported configuration parameters are listed in the [DuckDB docs](https://duckdb.org/docs/sql/configuration)

## Connecting to a remote DuckDB (Quack)

> Requires DuckDB >= 1.5 and the [`quack`](https://duckdb.org/quack/) extension, which is still experimental upstream.

[Quack](https://duckdb.org/quack/) is DuckDB's client/server protocol: one DuckDB serves, another connects. On the server:

```sql
CALL quack_serve('quack:0.0.0.0:9494', token = 'super_secret', allow_other_hostname = true);
```

Then connect from SQLAlchemy with the `quack://` scheme:

```python
from sqlalchemy import create_engine, text

engine = create_engine("quack://:super_secret@db.example.com:9494")

with engine.connect() as conn:
    conn.execute(text("SELECT region, sum(amount) FROM sales GROUP BY ALL")).fetchall()
```

URL format: `quack://[:token]@host[:port][/database][?option=value...]`

| Part / option | Default | Description |
|---|---|---|
| `host` | `localhost` | Server hostname. |
| `port` | `9494` | Server port. |
| password (`:token@`) | none | Token the server checks (the `token` given to `quack_serve`, or whatever your `quack_authentication_function` accepts). Can also be passed as `connect_args={"token": ...}`. The username part is ignored. |
| `/database` | server default | Server-side database to `USE` after connecting (remote mode only). |
| `execution` | `remote` | `remote` or `local`, see below. |
| `disable_ssl` | extension default (no TLS for localhost, TLS otherwise) | Set `true`/`false` to force plain HTTP / HTTPS. `quack_serve` itself listens on plain HTTP, so non-local servers are expected to sit behind a TLS-terminating reverse proxy. |
| `alias` | `quack` | Name of the attached catalog on the client. |

Other query parameters are applied as DuckDB settings on the *local* client, like with `duckdb://`. `duckdb+quack://` is accepted as an alias of `quack://`.

### Execution modes

* **`remote` (default)** — every statement is sent verbatim to the server (through `quack_query_by_name`) and executed there entirely: joins, aggregations, server-side macros and extensions, and reflection queries (`information_schema`, `duckdb_tables()`, ...) all see the server. Only results travel back. Transactions (`BEGIN`/`COMMIT`/`ROLLBACK`) and session state (`USE`, `SET`) live on the server connection. Caveats:
  * The Quack protocol has no bind parameters, so parameters are rendered into SQL literals client-side (`None`, `bool`, `int`, `float`, `Decimal`, `str`, `bytes`, `date`/`time`/`datetime`/`timedelta`, `UUID`, lists and dicts are supported; anything else raises `TypeError`). `executemany` sends one statement per parameter set.
  * Only the result of the first statement is returned when a string contains several statements.
  * Client-only features such as registering a pandas DataFrame are not available.
* **`local`** — the server is `ATTACH`ed and set as default catalog, and the local DuckDB plans and executes queries. Quack pushes projections and simple filters down to the server, but joins and aggregations run on the client. Useful to combine remote with local data. Caveats (current Quack limitations): a query can't scan more than one remote table (so no joins between remote tables), and a local `ROLLBACK` does not undo writes already sent to the server.

Each pooled SQLAlchemy connection is a separate in-memory client DuckDB with its own Quack connection, so the regular `QueuePool` is used and `pool_pre_ping` really checks the server.

### Using with Apache Superset

Use a URI like `quack://:{token}@{host}:9494/`. Some notes:

* Superset's default `PREVENT_UNSAFE_DB_CONNECTIONS = True` rejects every `duckdb://` / `duckdb+...://` URI (local DuckDB can read the Superset host's filesystem). `quack://` is not on that blocklist, which is why it is the primary scheme. Keep the default `execution=remote` so that user SQL is only ever executed by the server; `execution=local` runs SQL in a DuckDB inside the Superset process.
* All SQL from Superset users runs on the server with the server process' privileges. Harden the server accordingly, e.g. `SET enable_external_access = false` (after attaching what it needs) and/or a `quack_authorization_function`.
* Superset has no engine spec for the `quack` backend yet, so it falls back to its generic one (no time grains, generic SQL parsing). A `QuackEngineSpec` in Superset (subclassing its `DuckDBEngineSpec`, plus mapping `quack` to sqlglot's DuckDB dialect) is needed to get the same experience as the `duckdb` engine.
* Superset uses `NullPool` by default, so every query opens a new client and Quack connection.

## How to register a pandas DataFrame

```python
conn = create_engine("duckdb:///:memory:").connect()

# with SQLAlchemy 1.3
conn.execute("register", ("dataframe_name", pd.DataFrame(...)))

# with SQLAlchemy 1.4+
conn.execute(text("register(:name, :df)"), {"name": "test_df", "df": df})

conn.execute("select * from dataframe_name")
```

## Things to keep in mind
Duckdb's SQL parser is based on the PostgreSQL parser, but not all features in PostgreSQL are supported in duckdb. Because the `duckdb_driver` dialect is derived from the `postgresql` dialect, `SQLAlchemy` may try to use PostgreSQL-only features. Below are some caveats to look out for.

### Auto-incrementing ID columns
When defining an Integer column as a primary key, `SQLAlchemy` uses the `SERIAL` datatype for PostgreSQL. Duckdb does not yet support this datatype because it's a non-standard PostgreSQL legacy type, so a workaround is to use the `SQLAlchemy.Sequence()` object to auto-increment the key. For more information on sequences, you can find the [`SQLAlchemy Sequence` documentation here](https://docs.sqlalchemy.org/en/14/core/defaults.html#associating-a-sequence-as-the-server-side-default).

The following example demonstrates how to create an auto-incrementing ID column for a simple table:

```python
>>> import sqlalchemy
>>> engine = sqlalchemy.create_engine('duckdb:////path/to/duck.db')
>>> metadata = sqlalchemy.MetaData(engine)
>>> user_id_seq = sqlalchemy.Sequence('user_id_seq')
>>> users_table = sqlalchemy.Table(
...     'users',
...     metadata,
...     sqlalchemy.Column(
...         'id',
...         sqlalchemy.Integer,
...         user_id_seq,
...         server_default=user_id_seq.next_value(),
...         primary_key=True,
...     ),
... )
>>> metadata.create_all(bind=engine)
```

### Pandas `read_sql()` chunksize

**NOTE**: this is no longer an issue in versions `>=0.5.0` of `duckdb`

The `pandas.read_sql()` method can read tables from `duckdb_engine` into DataFrames, but the `sqlalchemy.engine.result.ResultProxy` trips up when `fetchmany()` is called. Therefore, for now `chunksize=None` (default) is necessary when reading duckdb tables into DataFrames. For example:

```python
>>> import pandas as pd
>>> import sqlalchemy
>>> engine = sqlalchemy.create_engine('duckdb:////path/to/duck.db')
>>> df = pd.read_sql('users', engine)                ### Works as expected
>>> df = pd.read_sql('users', engine, chunksize=25)  ### Throws an exception
```

### Unsigned integer support

Unsigned integers are supported by DuckDB, and are available in [`duckdb_engine.datatypes`](duckdb_engine/datatypes.py).

## Alembic Integration

SQLAlchemy's companion library `alembic` can optionally be used to manage database migrations.

This support can be enabling by adding an Alembic implementation class for the `duckdb` dialect.

```python
from alembic.ddl.impl import DefaultImpl

class AlembicDuckDBImpl(DefaultImpl):
    """Alembic implementation for DuckDB."""

    __dialect__ = "duckdb"
```

After loading this class with your program, Alembic will no longer raise an error when generating or applying migrations.

## Preloading extensions (experimental)

> DuckDB 0.9.0+ includes builtin support for autoinstalling and autoloading of extensions, see [the extension documentation](http://duckdb.org/docs/archive/0.9.0/extensions/overview#autoloadable-extensions) for more information.

Until the DuckDB python client allows you to natively preload extensions, I've added experimental support via a `connect_args` parameter

```python
from sqlalchemy import create_engine

create_engine(
    'duckdb:///:memory:',
    connect_args={
        'preload_extensions': ['https'],
        'config': {
            's3_region': 'ap-southeast-1'
        }
    }
)
```

## Registering Filesystems

> DuckDB allows registering filesystems from [fsspec](https://filesystem-spec.readthedocs.io/), see [documentation](https://duckdb.org/docs/guides/python/filesystems.html) for more information.

Support is provided under `connect_args` parameter

```python
from sqlalchemy import create_engine
from fsspec import filesystem

create_engine(
    'duckdb:///:memory:',
    connect_args={
        'register_filesystems': [filesystem('gcs')],
    }
)
```

## The name

Yes, i thought forking and looking to maintain this was a good moment to also rename it to `duckdb-driver`.
