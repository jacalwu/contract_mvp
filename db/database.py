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


def _statements() -> list[str]:
    """Split the schema into single statements.

    Line comments (``-- ...``) are stripped first so that a semicolon inside a
    comment can never truncate the statement it sits in.
    """
    lines = []
    for line in _SCHEMA_SQL.splitlines():
        cut = line.find("--")
        lines.append(line[:cut] if cut != -1 else line)
    return [part.strip() for part in "\n".join(lines).split(";") if part.strip()]


def get_conn() -> duckdb.DuckDBPyConnection:
    """Lazily create the process-wide connection and apply idempotent DDL."""
    global _conn
    if _conn is None:
        with _lock:
            if _conn is None:
                config.init_dirs()
                con = duckdb.connect(str(config.DB_PATH))
                for statement in _statements():
                    con.execute(statement)
                _migrate(con)
                _conn = con
    return _conn


# Columns added after the first release. CREATE TABLE IF NOT EXISTS cannot add them
# to an existing database, so they are applied idempotently on connect.
_MIGRATIONS = (
    ("stakeholders", "color", "VARCHAR"),
    ("comments", "anchor_start", "INTEGER"),
    ("comments", "anchor_end", "INTEGER"),
    ("comments", "quoted_text", "VARCHAR"),
    ("comments", "color", "VARCHAR"),
    ("comments", "resolved_at", "TIMESTAMP"),
    ("stakeholders", "side", "VARCHAR"),
)


def _migrate(con: duckdb.DuckDBPyConnection) -> None:
    for table, column, sql_type in _MIGRATIONS:
        con.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {sql_type}")
    # give reviewers created before colours existed a stable swatch
    rows = con.execute(
        "SELECT stakeholder_id FROM stakeholders WHERE color IS NULL ORDER BY contract_id, created_at, email"
    ).fetchall()
    for index, (stakeholder_id,) in enumerate(rows):
        con.execute(
            "UPDATE stakeholders SET color = ? WHERE stakeholder_id = ?",
            [config.REVIEW_PALETTE[index % len(config.REVIEW_PALETTE)], stakeholder_id],
        )
    # Backfill side on contracts created before the buyer/vendor split: alternate
    # sides within each contract so an existing roster stays balanced (BUYER, VENDOR, …).
    con.execute(
        """
        UPDATE stakeholders
        SET side = m.side
        FROM (
            SELECT
                stakeholder_id,
                CASE WHEN (ROW_NUMBER() OVER (PARTITION BY contract_id ORDER BY created_at, email)) % 2 = 1
                     THEN 'BUYER' ELSE 'VENDOR' END AS side
            FROM stakeholders
            WHERE side IS NULL
        ) m
        WHERE stakeholders.stakeholder_id = m.stakeholder_id
        """
    )
    # Any remaining NULLs (e.g. a single-stakeholder contract) default to BUYER so the
    # NOT NULL constraint is satisfied; those contracts simply cannot reach APPROVED
    # until a VENDOR is added.
    con.execute("UPDATE stakeholders SET side = 'BUYER' WHERE side IS NULL")


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
