"""
schema.py
---------
Inspects the connected MySQL database (tables, columns, types, primary
keys, foreign keys / relationships) and caches the result so it can be
reused both for the sidebar UI and for grounding the LLM's SQL generation
in the *real* schema instead of a guess.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

from app.database import get_connection

_CACHE: dict[str, Any] = {"data": None, "ts": 0.0}
_CACHE_TTL_SECONDS = 60


@dataclass
class ColumnInfo:
    name: str
    data_type: str
    is_nullable: bool
    is_primary_key: bool
    default: str | None = None
    extra: str | None = None  # e.g. auto_increment


@dataclass
class ForeignKeyInfo:
    column: str
    references_table: str
    references_column: str


@dataclass
class TableInfo:
    name: str
    columns: list[ColumnInfo] = field(default_factory=list)
    primary_keys: list[str] = field(default_factory=list)
    foreign_keys: list[ForeignKeyInfo] = field(default_factory=list)
    row_count: int | None = None


def _fetch_all(cursor, sql: str, params: tuple = ()) -> list[dict]:
    cursor.execute(sql, params)
    return cursor.fetchall()


def inspect_schema(database: str | None = None, use_cache: bool = True) -> list[TableInfo]:
    """Introspect all tables in the current database via INFORMATION_SCHEMA."""
    now = time.time()
    if use_cache and _CACHE["data"] is not None and (now - _CACHE["ts"]) < _CACHE_TTL_SECONDS:
        return _CACHE["data"]

    db_name = database or os.getenv("MYSQL_DATABASE", "")
    conn = get_connection()
    try:
        cursor = conn.cursor(dictionary=True)

        tables_raw = _fetch_all(
            cursor,
            """
            SELECT TABLE_NAME
            FROM INFORMATION_SCHEMA.TABLES
            WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'
            ORDER BY TABLE_NAME
            """,
            (db_name,),
        )

        columns_raw = _fetch_all(
            cursor,
            """
            SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, IS_NULLABLE,
                   COLUMN_KEY, COLUMN_DEFAULT, EXTRA
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = %s
            ORDER BY TABLE_NAME, ORDINAL_POSITION
            """,
            (db_name,),
        )

        fks_raw = _fetch_all(
            cursor,
            """
            SELECT TABLE_NAME, COLUMN_NAME, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME
            FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE
            WHERE TABLE_SCHEMA = %s AND REFERENCED_TABLE_NAME IS NOT NULL
            """,
            (db_name,),
        )

        cursor.close()

        tables: dict[str, TableInfo] = {
            row["TABLE_NAME"]: TableInfo(name=row["TABLE_NAME"]) for row in tables_raw
        }

        for row in columns_raw:
            t = tables.get(row["TABLE_NAME"])
            if t is None:
                continue
            is_pk = row["COLUMN_KEY"] == "PRI"
            col = ColumnInfo(
                name=row["COLUMN_NAME"],
                data_type=row["DATA_TYPE"],
                is_nullable=(row["IS_NULLABLE"] == "YES"),
                is_primary_key=is_pk,
                default=row["COLUMN_DEFAULT"],
                extra=row["EXTRA"] or None,
            )
            t.columns.append(col)
            if is_pk:
                t.primary_keys.append(col.name)

        for row in fks_raw:
            t = tables.get(row["TABLE_NAME"])
            if t is None:
                continue
            t.foreign_keys.append(
                ForeignKeyInfo(
                    column=row["COLUMN_NAME"],
                    references_table=row["REFERENCED_TABLE_NAME"],
                    references_column=row["REFERENCED_COLUMN_NAME"],
                )
            )

        # best-effort row counts, non-fatal if it fails on a huge/locked table
        conn2 = conn
        count_cursor = conn2.cursor()
        for t in tables.values():
            try:
                count_cursor.execute(f"SELECT COUNT(*) FROM `{t.name}`")
                (cnt,) = count_cursor.fetchone()
                t.row_count = cnt
            except Exception:  # noqa: BLE001
                t.row_count = None
        count_cursor.close()

        result = list(tables.values())
        _CACHE["data"] = result
        _CACHE["ts"] = now
        return result
    finally:
        conn.close()


def invalidate_schema_cache() -> None:
    _CACHE["data"] = None
    _CACHE["ts"] = 0.0


def schema_to_prompt_text(tables: list[TableInfo]) -> str:
    """Render the schema as a compact, LLM-friendly text block."""
    lines: list[str] = []
    for t in tables:
        col_parts = []
        for c in t.columns:
            tag = []
            if c.is_primary_key:
                tag.append("PK")
            if not c.is_nullable:
                tag.append("NOT NULL")
            if c.extra:
                tag.append(c.extra)
            tag_str = f" [{', '.join(tag)}]" if tag else ""
            col_parts.append(f"{c.name} {c.data_type}{tag_str}")
        line = f"TABLE {t.name} ({', '.join(col_parts)})"
        lines.append(line)
        for fk in t.foreign_keys:
            lines.append(
                f"  FOREIGN KEY {t.name}.{fk.column} -> {fk.references_table}.{fk.references_column}"
            )
    return "\n".join(lines)


def schema_to_dict(tables: list[TableInfo]) -> list[dict]:
    """JSON-serialisable shape for the /schema endpoint and sidebar."""
    out = []
    for t in tables:
        out.append(
            {
                "name": t.name,
                "row_count": t.row_count,
                "primary_keys": t.primary_keys,
                "columns": [
                    {
                        "name": c.name,
                        "type": c.data_type,
                        "nullable": c.is_nullable,
                        "primary_key": c.is_primary_key,
                        "extra": c.extra,
                    }
                    for c in t.columns
                ],
                "foreign_keys": [
                    {
                        "column": fk.column,
                        "references_table": fk.references_table,
                        "references_column": fk.references_column,
                    }
                    for fk in t.foreign_keys
                ],
            }
        )
    return out
