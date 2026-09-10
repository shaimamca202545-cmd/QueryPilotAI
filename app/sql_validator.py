"""
sql_validator.py
-----------------
Sanity/safety checks applied to LLM-generated SQL before execution.

This app intentionally allows SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER,
DROP, TRUNCATE and other DML/DDL - it is a full "AI database engineer", not a
read-only assistant. The validator's job is narrower than "only allow safe
statements": it exists to catch things that are never legitimate output from
the SQL-generation prompt - statement stacking/injection, file-system access
functions, and empty/non-SQL responses - and to give a clear reason when a
generated statement should be rejected outright rather than sent to MySQL.
"""

from __future__ import annotations

import os
import re

ALLOW_WRITE_QUERIES = os.getenv("ALLOW_WRITE_QUERIES", "true").strip().lower() == "true"

_READ_STATEMENTS = {"SELECT", "SHOW", "DESCRIBE", "DESC", "EXPLAIN"}
_WRITE_STATEMENTS = {"INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP", "TRUNCATE", "REPLACE"}
_ALL_ALLOWED = _READ_STATEMENTS | _WRITE_STATEMENTS

# Functions/keywords that read or write the server's filesystem / OS - never
# something a natural-language DB assistant should be issuing.
_DANGEROUS_PATTERNS = [
    re.compile(r"\bINTO\s+OUTFILE\b", re.IGNORECASE),
    re.compile(r"\bINTO\s+DUMPFILE\b", re.IGNORECASE),
    re.compile(r"\bLOAD_FILE\s*\(", re.IGNORECASE),
    re.compile(r"\bLOAD\s+DATA\b", re.IGNORECASE),
    re.compile(r"\bSYSTEM\s*\(", re.IGNORECASE),
    re.compile(r"\bGRANT\b", re.IGNORECASE),
    re.compile(r"\bREVOKE\b", re.IGNORECASE),
    re.compile(r"\bCREATE\s+USER\b", re.IGNORECASE),
    re.compile(r"\bDROP\s+USER\b", re.IGNORECASE),
    re.compile(r"\bSET\s+GLOBAL\b", re.IGNORECASE),
    re.compile(r"\bINFORMATION_SCHEMA\.PROCESSLIST\b", re.IGNORECASE),
]


class ValidationResult:
    def __init__(self, ok: bool, statement_type: str = "UNKNOWN", reason: str | None = None):
        self.ok = ok
        self.statement_type = statement_type
        self.reason = reason


def _strip_trailing_semicolons(sql: str) -> str:
    return sql.strip().rstrip(";").strip()


def _count_statements(sql: str) -> int:
    """
    Rough statement-stacking detector: count semicolons that are not inside a
    quoted string. Generated SQL should be exactly one statement.
    """
    count = 0
    in_single = False
    in_double = False
    for ch in sql:
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == ";" and not in_single and not in_double:
            count += 1
    return count


def _extract_select_list(sql: str) -> str | None:
    """Return the raw text between SELECT [DISTINCT] and the top-level FROM, or None."""
    m = re.match(r"^\s*SELECT\s+(DISTINCT\s+)?", sql, re.IGNORECASE)
    if not m:
        return None
    start = m.end()
    depth = 0
    i = start
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0 and sql[i:i + 4].upper() == "FROM" and (i == 0 or not sql[i - 1].isalnum()) and (i + 4 >= n or not sql[i + 4].isalnum()):
            return sql[start:i]
        i += 1
    return None


def _split_top_level(expr_list: str) -> list[str]:
    parts, current, depth = [], "", 0
    for ch in expr_list:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += ch
    parts.append(current)
    return [p.strip() for p in parts if p.strip()]


_AS_ALIAS_RE = re.compile(r"^(.*?)\s+AS\s+[`\"\[]?\w+[`\"\]]?$", re.IGNORECASE)


def _normalize_projection(expr: str) -> str:
    """Strip an explicit AS alias and normalise whitespace/case/backticks for comparison."""
    m = _AS_ALIAS_RE.match(expr.strip())
    source = m.group(1) if m else expr
    source = source.strip().strip("`").replace("`", "")
    return re.sub(r"\s+", " ", source).strip().lower()


def find_duplicate_projection(sql: str) -> str | None:
    """
    Detect the common small-model failure mode of selecting the exact same
    column/expression twice for what should be two different output fields
    (e.g. `SELECT d.name, d.name FROM employees e JOIN departments d ...`
    when the intent was employee name + department name). Returns the
    offending expression if found, else None. Only meaningful for SELECT.
    """
    select_list = _extract_select_list(sql)
    if not select_list:
        return None
    items = _split_top_level(select_list)
    if len(items) < 2:
        return None
    if any(item.strip() == "*" for item in items):
        return None
    seen: set[str] = set()
    for item in items:
        norm = _normalize_projection(item)
        if not norm or norm == "*":
            continue
        # aggregate/computed expressions repeated on purpose (e.g. two different
        # AVG(...) with different args) won't collide once normalised; only a
        # true duplicate source expression will.
        if norm in seen:
            return item.strip()
        seen.add(norm)
    return None


def validate_sql(sql: str) -> ValidationResult:
    if not sql or not sql.strip():
        return ValidationResult(False, "UNKNOWN", "The model returned an empty statement.")

    body = _strip_trailing_semicolons(sql)
    if not body:
        return ValidationResult(False, "UNKNOWN", "The model returned an empty statement.")

    # Reject stacked statements (more than one semicolon-terminated statement).
    semicolon_count = _count_statements(sql)
    if semicolon_count > 1:
        return ValidationResult(False, "UNKNOWN", "Multiple SQL statements were generated; only a single statement is allowed per request.")

    first_word_match = re.match(r"^\s*([A-Za-z]+)", body)
    first_word = first_word_match.group(1).upper() if first_word_match else ""

    statement_type = first_word if first_word in _ALL_ALLOWED else first_word

    if first_word not in _ALL_ALLOWED:
        return ValidationResult(False, statement_type, f"'{first_word}' is not a supported SQL statement type.")

    if first_word in _WRITE_STATEMENTS and not ALLOW_WRITE_QUERIES:
        return ValidationResult(
            False, statement_type,
            "Write/DDL queries are currently disabled for this workspace (ALLOW_WRITE_QUERIES=false).",
        )

    for pattern in _DANGEROUS_PATTERNS:
        if pattern.search(body):
            return ValidationResult(False, statement_type, "That statement touches server-level operations that this assistant is not permitted to run.")

    if statement_type == "SELECT":
        dup = find_duplicate_projection(body)
        if dup:
            return ValidationResult(
                False, statement_type,
                f"The SELECT list picks the same column/expression twice ('{dup}') for what should be two "
                "different fields - likely selecting one table's column when it should pull from each "
                "joined table separately, with distinct aliases.",
            )

    return ValidationResult(True, statement_type, None)
