"""DuckDB access layer: single connection, serialized access, parameterized SQL only."""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Any, Iterator, Sequence

import duckdb

import config

_SCHEMA_SQL = (config.BASE_DIR / "db" / "schema.sql").read_text(encoding="utf-8")
_conn: duckdb.DuckDBPyConnection | None = None
_lock = threading.RLock()


def get_conn() -> duckdb.DuckDBPyConnection:
    """Lazily create the process-wide connection and apply idempotent DDL."""
    global _conn
    if _conn is None:
        with _lock:
            if _conn is None:
                config.init_dirs()
                con = duckdb.connect(str(config.DB_PATH))
                for statement in _SCHEMA_SQL.split(";"):
                    if statement.strip():
                        con.execute(statement)
                _conn = con
    return _conn


def sql(con: duckdb.DuckDBPyConnection, statement: str, params: Sequence[Any] = ()):
    """Execute a statement on a connection. Values are always bound, never interpolated."""
    if params:
        return con.execute(statement, list(params))
    return con.execute(statement)


def as_dicts(cursor) -> list[dict[str, Any]]:
    if not cursor.description:
        return []
    columns = [item[0] for item in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def fetch_all(statement: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
    with _lock:
        return as_dicts(sql(get_conn(), statement, params))


def fetch_one(statement: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
    rows = fetch_all(statement, params)
    return rows[0] if rows else None


def execute(statement: str, params: Sequence[Any] = ()) -> None:
    with _lock:
        sql(get_conn(), statement, params)


@contextmanager
def tx() -> Iterator[duckdb.DuckDBPyConnection]:
    """Atomic unit for a domain operation and its audit record."""
    with _lock:
        con = get_conn()
        con.execute("BEGIN TRANSACTION")
        try:
            yield con
        except Exception:
            try:
                con.execute("ROLLBACK")
            except Exception:
                pass
            raise
        else:
            con.execute("COMMIT")


def reset_connection() -> None:
    """Test helper: drop the cached connection so a new DB path can be used."""
    global _conn
    with _lock:
        if _conn is not None:
            try:
                _conn.close()
            except Exception:
                pass
        _conn = None
