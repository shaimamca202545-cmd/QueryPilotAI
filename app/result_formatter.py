"""
result_formatter.py
--------------------
Turns a raw QueryResult into (a) a compact text preview suitable for
feeding back into the LLM for human-language summarisation, and (b) a
JSON-safe table structure for the frontend's result table.
"""

from __future__ import annotations

import datetime
import decimal
import re 
from typing import Any

from app.database import QueryResult

MAX_PREVIEW_ROWS = 25


def _json_safe(value: Any) -> Any:
    if isinstance(value, (decimal.Decimal,)):
        return float(value)
    if isinstance(value, (datetime.date, datetime.datetime, datetime.time)):
        return str(value)
    if isinstance(value, (bytes, bytearray)):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return value.hex()
    return value


def rows_to_json_safe(rows: list[dict]) -> list[dict]:
    return [{k: _json_safe(v) for k, v in row.items()} for row in rows]


def build_result_preview(result: QueryResult) -> str:
    """Compact, readable text block describing the result, for the LLM prompt."""
    if not result.success:
        return f"(query failed: {result.error})"

    if result.statement_type != "SELECT" and not result.rows:
        return f"{result.row_count} row(s) affected by this {result.statement_type} statement."

    if not result.rows:
        return "(no rows returned)"

    preview_rows = result.rows[:MAX_PREVIEW_ROWS]
    lines = [", ".join(result.columns)]
    for row in preview_rows:
        safe_row = {k: _json_safe(v) for k, v in row.items()}
        lines.append(", ".join(str(safe_row.get(c, "")) for c in result.columns))

    text = "\n".join(lines)
    remaining = result.row_count - len(preview_rows)
    if remaining > 0:
        text += f"\n... and {remaining} more row(s) not shown here."
    if result.truncated:
        text += "\n(Note: result was truncated to the first rows for display.)"
    return text


def build_table_payload(result: QueryResult) -> dict:
    """JSON-serialisable structure the frontend renders as a table."""
    return {
        "success": result.success,
        "statement_type": result.statement_type,
        "columns": result.columns,
        "rows": rows_to_json_safe(result.rows),
        "row_count": result.row_count,
        "truncated": result.truncated,
        "elapsed_ms": round(result.elapsed_ms, 1),
        "error": result.error,
    }

_LEADING_SENTENCE_RE = re.compile(r"^([^\n]*?[.!?])(\s+|$)")
_INT_TOKEN_RE = re.compile(r"\b\d+\b")
_SAFE_LEAD_INS = ("here's", "here is", "the breakdown", "below is")


def _flat_value_strings(rows: list[dict]) -> set[str]:
    """All values that actually appear in the result, as normalised strings."""
    values: set[str] = set()
    for row in rows:
        for v in row.values():
            if v is None:
                continue
            s = _json_safe(v)
            values.add(str(s))
            try:
                f = float(s)
                if f == int(f):
                    values.add(str(int(f)))
            except (TypeError, ValueError):
                pass
    return values


def sanity_check_answer(answer: str, result: "QueryResult") -> str:
    """
    Deterministic guard against a known small-model failure mode: opening the
    answer with a headline number copied from ROW_COUNT (how many result rows
    came back) rather than an actual data value - e.g. "There are 3 employees
    working in each department" when 3 is the number of department rows, not
    an employee count.

    Only intervenes when the leading sentence contains the row_count as a
    standalone number that does NOT otherwise appear anywhere in the actual
    result values.
    """
    if result.statement_type != "SELECT" or result.row_count <= 1 or not answer.strip():
        return answer

    match = _LEADING_SENTENCE_RE.match(answer.strip())
    if not match:
        return answer
    leading_sentence = match.group(1)

    tokens = _INT_TOKEN_RE.findall(leading_sentence)
    if str(result.row_count) not in tokens:
        return answer

    flat_values = _flat_value_strings(result.rows)
    if str(result.row_count) in flat_values:
        return answer  # coincidence, not the bug - leave it alone

    remainder = answer.strip()[match.end():].lstrip()
    if not remainder:
        return answer

    if not remainder.lower().startswith(_SAFE_LEAD_INS):
        remainder = "Here's what the data shows:\n\n" + remainder
    return remainder