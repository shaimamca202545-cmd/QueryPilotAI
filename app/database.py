"""
database.py
-----------
Thin MySQL access layer built on mysql-connector-python.

Responsibilities:
- Read connection settings from environment (.env) only. No credentials
  are ever hard-coded here.
- Provide a pooled connection so requests don't pay full handshake cost.
- Execute arbitrary single SQL statements (SELECT / INSERT / UPDATE /
  DELETE / CREATE / ALTER / DROP / TRUNCATE / ...) and normalise the
  outcome into a predictable shape the rest of the app can reason about.
- Never crash the process on a MySQL error - always return a structured
  error the caller can turn into a human explanation.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

import mysql.connector
from mysql.connector import pooling, Error as MySQLError

MAX_RESULT_ROWS = int(os.getenv("MAX_RESULT_ROWS", "200"))

_POOL: pooling.MySQLConnectionPool | None = None
_POOL_LOCK_MSG = "Database pool not initialised"


@dataclass
class QueryResult:
    """Normalised outcome of executing one SQL statement."""

    success: bool
    statement_type: str = "UNKNOWN"          # SELECT / INSERT / UPDATE / ...
    columns: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    row_count: int = 0                       # rows returned (SELECT) or affected (write)
    truncated: bool = False
    elapsed_ms: float = 0.0
    error: str | None = None
    error_code: int | None = None


def _build_config() -> dict[str, Any]:
    host = os.getenv("MYSQL_HOST", "localhost")
    port = int(os.getenv("MYSQL_PORT", "3306"))
    user = os.getenv("MYSQL_USER", "root")
    password = os.getenv("MYSQL_PASSWORD", "")
    database = os.getenv("MYSQL_DATABASE", "")

    if not database:
        raise RuntimeError(
            "MYSQL_DATABASE is not set. Copy .env.example to .env and fill it in."
        )

    return {
        "host": host,
        "port": port,
        "user": user,
        "password": password,
        "database": database,
        "autocommit": False,
        "connection_timeout": 10,
        "use_pure": True,
    }


def init_pool(pool_size: int = 5) -> None:
    """Create (or recreate) the global connection pool. Call once at startup."""
    global _POOL
    config = _build_config()
    _POOL = pooling.MySQLConnectionPool(
        pool_name="query_pilot_pool",
        pool_size=pool_size,
        **config,
    )


def _get_pool() -> pooling.MySQLConnectionPool:
    if _POOL is None:
        init_pool()
    assert _POOL is not None, _POOL_LOCK_MSG
    return _POOL


def get_connection():
    """Get a live connection from the pool, reconnecting if needed."""
    conn = _get_pool().get_connection()
    if not conn.is_connected():
        conn.reconnect(attempts=2, delay=1)
    return conn


def check_connection() -> tuple[bool, str | None]:
    """Lightweight health check used by GET /health and the UI status dot."""
    try:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.fetchall()
            cur.close()
            return True, None
        finally:
            conn.close()
    except MySQLError as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001 - surface any startup/config issue too
        return False, str(exc)


_WRITE_KEYWORDS = {
    "INSERT": "INSERT",
    "UPDATE": "UPDATE",
    "DELETE": "DELETE",
    "CREATE": "CREATE",
    "ALTER": "ALTER",
    "DROP": "DROP",
    "TRUNCATE": "TRUNCATE",
    "REPLACE": "REPLACE",
}


def classify_statement(sql: str) -> str:
    first_word = sql.strip().split(None, 1)[0].upper() if sql.strip() else ""
    if first_word == "SELECT" or first_word == "SHOW" or first_word == "DESC" or first_word == "DESCRIBE" or first_word == "EXPLAIN":
        return "SELECT"
    return _WRITE_KEYWORDS.get(first_word, first_word or "UNKNOWN")


def _dedupe_columns(names: list[str]) -> list[str]:
    """
    MySQL happily returns two columns both called e.g. 'name' when a join
    selects unaliased same-named columns from different tables. Give each
    repeat a distinct key (name, name_2, name_3, ...) so no data is lost
    when rows are turned into column->value mappings.
    """
    seen: dict[str, int] = {}
    result = []
    for name in names:
        seen[name] = seen.get(name, 0) + 1
        result.append(name if seen[name] == 1 else f"{name}_{seen[name]}")
    return result


def execute_sql(sql: str) -> QueryResult:
    """
    Execute a single SQL statement and return a normalised QueryResult.
    Handles SELECT-style (fetch rows) and write/DDL-style (commit, rowcount)
    statements uniformly. Never raises - all MySQL errors are captured.
    """
    stmt_type = classify_statement(sql)
    start = time.perf_counter()

    conn = None
    try:
        conn = get_connection()
        # NOTE: intentionally NOT using dictionary=True here. A dict cursor keys
        # each row by column name, and generated SQL frequently joins tables
        # that share a column name (e.g. employees.name + departments.name)
        # without aliasing - a dict would silently drop/overwrite one of them.
        # Using a plain cursor + explicit dedup keeps every selected column.
        cursor = conn.cursor()
        cursor.execute(sql)

        if cursor.with_rows:
            raw_columns = [d[0] for d in cursor.description] if cursor.description else []
            columns = _dedupe_columns(raw_columns)
            raw_rows = cursor.fetchall()
            truncated = False
            if len(raw_rows) > MAX_RESULT_ROWS:
                raw_rows = raw_rows[:MAX_RESULT_ROWS]
                truncated = True
            rows = [dict(zip(columns, row)) for row in raw_rows]
            conn.commit()  # harmless for SELECT, needed if it was e.g. a CALL
            result = QueryResult(
                success=True,
                statement_type=stmt_type,
                columns=columns,
                rows=rows,
                row_count=len(rows),
                truncated=truncated,
                elapsed_ms=(time.perf_counter() - start) * 1000,
            )
        else:
            conn.commit()
            result = QueryResult(
                success=True,
                statement_type=stmt_type,
                columns=[],
                rows=[],
                row_count=cursor.rowcount if cursor.rowcount is not None and cursor.rowcount >= 0 else 0,
                elapsed_ms=(time.perf_counter() - start) * 1000,
            )
        cursor.close()
        return result

    except MySQLError as exc:
        if conn is not None:
            try:
                conn.rollback()
            except MySQLError:
                pass
        return QueryResult(
            success=False,
            statement_type=stmt_type,
            error=str(exc.msg) if hasattr(exc, "msg") and exc.msg else str(exc),
            error_code=getattr(exc, "errno", None),
            elapsed_ms=(time.perf_counter() - start) * 1000,
        )
    except Exception as exc:  # noqa: BLE001
        if conn is not None:
            try:
                conn.rollback()
            except MySQLError:
                pass
        return QueryResult(
            success=False,
            statement_type=stmt_type,
            error=str(exc),
            elapsed_ms=(time.perf_counter() - start) * 1000,
        )
    finally:
        if conn is not None:
            conn.close()
