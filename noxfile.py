from contextlib import contextmanager
from typing import Generator

import nox
from nox_uv import session

nox.options.default_venv_backend = "uv"
nox.options.error_on_external_run = True


@contextmanager
def group(title: str) -> Generator[None, None, None]:
    import github_action_utils as gha

    try:
        gha.start_group(title)
        yield
    except Exception as e:
        gha.end_group()
        gha.error(f"{title} failed with {e}")
        raise
    else:
        gha.end_group()


# TODO: "0.5.1", "0.6.1", "0.7.1", "0.8.1"
# TODO: 3.11, 3.12, 3.13
@session(python=["3.10", "3.11"], uv_groups=["dev", "nox"])
@nox.parametrize("duckdb", ["1.4.3"])
@nox.parametrize(
    "sqlalchemy",
    [
        "1.3",
        "1.4",
    ],
)
def tests(session: nox.Session, duckdb: str, sqlalchemy: str) -> None:
    tests_core(session, duckdb, sqlalchemy)


def tests_core(session: nox.Session, duckdb: str, sqlalchemy: str) -> None:
    with group(f"{session.name} - Install"):
        operator = "==" if sqlalchemy.count(".") == 2 else "~="
        session.install(f"sqlalchemy{operator}{sqlalchemy}")
        if duckdb == "master":
            session.install("duckdb", "--pre", "-U")
        else:
            session.install(f"duckdb=={duckdb}")
    with group(f"{session.name} Test"):
        session.run(
            "pytest",
            "--junitxml=results.xml",
            "--cov",
            "--cov-report",
            "xml:coverage.xml",
            "--verbose",
            "-rs",
            "--remote-data",
            env={
                "SQLALCHEMY_WARN_20": "true",
            },
        )


@nox.session(py=["3.11"])
def mypy(session: nox.Session) -> None:
    session.run("mypy", "duckdb_driver/", external=True)
