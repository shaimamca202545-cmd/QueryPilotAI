"""
main.py
-------
QueryPilot AI - FastAPI application wiring together schema introspection,
Ollama (llama3.2) SQL generation, MySQL execution, and human-language
answer synthesis.

Endpoints:
    GET  /         - serves the chat UI
    POST /chat      - main conversational endpoint
    GET  /schema     - current database schema (for the sidebar)
    GET  /health     - MySQL + Ollama connectivity status
"""

from __future__ import annotations

import logging
import os
import uuid
from collections import defaultdict, deque
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # must happen before other app modules read os.getenv at import time

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from app import database, schema as schema_module
from app.models import ChatRequest, ChatResponse, HealthResponse, SchemaResponse
from app.result_formatter import build_result_preview, build_table_payload, sanity_check_answer

from app.sql_generator import (
    OllamaError,
    check_ollama_health,
    generate_answer,
    generate_error_explanation,
    generate_sql,
)
from app.sql_validator import validate_sql

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("query_pilot")

BASE_DIR = Path(__file__).resolve().parent.parent

app = FastAPI(title="QueryPilot AI", description="AI-Powered SQL Database Engineer")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# session_id -> deque of {"role": "user"/"assistant", "content": str, "sql": str|None}
_HISTORY: dict[str, deque] = defaultdict(lambda: deque(maxlen=12))
MAX_CORRECTION_ATTEMPTS = 2


@app.on_event("startup")
def on_startup() -> None:
    try:
        database.init_pool()
        logger.info("MySQL connection pool initialised.")
    except Exception as exc:  # noqa: BLE001
        logger.warning("MySQL pool could not be initialised at startup: %s", exc)


def _history_to_text(session_id: str) -> str:
    turns = _HISTORY.get(session_id)
    if not turns:
        return ""
    lines = []
    for turn in turns:
        if turn["role"] == "user":
            lines.append(f"User: {turn['content']}")
        else:
            sql_part = f" (SQL: {turn['sql']})" if turn.get("sql") else ""
            lines.append(f"Assistant: {turn['content']}{sql_part}")
    return "\n".join(lines)


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/health", response_model=HealthResponse)
def health():
    mysql_ok, mysql_err = database.check_connection()
    ollama_ok, ollama_err = check_ollama_health()
    return HealthResponse(
        status="ok" if (mysql_ok and ollama_ok) else "degraded",
        mysql_connected=mysql_ok,
        mysql_error=mysql_err,
        ollama_connected=ollama_ok,
        ollama_error=ollama_err,
        database=os.getenv("MYSQL_DATABASE"),
        model=os.getenv("OLLAMA_MODEL", "llama3.2"),
    )


@app.get("/schema", response_model=SchemaResponse)
def get_schema():
    try:
        tables = schema_module.inspect_schema()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"Could not read database schema: {exc}") from exc
    return SchemaResponse(
        database=os.getenv("MYSQL_DATABASE", ""),
        tables=schema_module.schema_to_dict(tables),
    )


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    session_id = req.session_id or str(uuid.uuid4())
    question = req.message.strip()

     # 1. Ground the model in the real schema.
    try:
        tables = schema_module.inspect_schema()
        schema_text = schema_module.schema_to_prompt_text(tables)
    except Exception as exc:  # noqa: BLE001
        return ChatResponse(
            session_id=session_id,
            reply=f"I can't reach the database right now, so I can't answer that. ({exc})",
            error=str(exc),
        )

    # Check that the requested entity actually exists in the database.
    # This prevents "students" from being incorrectly answered using
    # the employees table.
    entity_ok, entity_message = _requested_entity_is_available(question, tables)

    if not entity_ok:
        _record_turn(session_id, question, entity_message, None)
        return ChatResponse(
            session_id=session_id,
            reply=entity_message,
            error="requested_entity_unavailable",
        )

    history_text = _history_to_text(session_id)

    # 2. Generate SQL.
    try:
        sql = generate_sql(schema_text, history_text, question)
    except OllamaError as exc:
        return ChatResponse(session_id=session_id, reply=str(exc), error=str(exc))

    validation = validate_sql(sql)
    corrected = False

    # 3. Execute, with one automatic correction attempt on failure.
    result = None
    attempts = 0
    last_sql = sql
    last_error = validation.reason

    if validation.ok:
        result = database.execute_sql(sql)
        if not result.success:
            last_error = result.error

    while (not validation.ok or (result and not result.success)) and attempts < MAX_CORRECTION_ATTEMPTS:
        attempts += 1
        try:
            fixed_sql = generate_sql(
                schema_text, history_text, question,
                previous_sql=last_sql, error=last_error or "unknown error",
            )
        except OllamaError as exc:
            return ChatResponse(session_id=session_id, reply=str(exc), error=str(exc))

        last_sql = fixed_sql
        validation = validate_sql(fixed_sql)
        if not validation.ok:
            last_error = validation.reason
            continue
        result = database.execute_sql(fixed_sql)
        corrected = True
        if not result.success:
            last_error = result.error

    # 4. Still failing after correction attempt(s) -> explain in plain language.
    if not validation.ok:
        explanation = (
            f"I couldn't safely run that request: {validation.reason} "
            "Could you rephrase it?"
        )
        _record_turn(session_id, question, explanation, None)
        return ChatResponse(
            session_id=session_id,
            reply=explanation,
            sql=last_sql,
            statement_type=validation.statement_type,
            error=validation.reason,
        )

    if result is None:
        explanation = "Something unexpected happened before the query could run. Please try again."
        _record_turn(session_id, question, explanation, last_sql)
        return ChatResponse(session_id=session_id, reply=explanation, sql=last_sql, error="internal_error")

    if not result.success:
        explanation = generate_error_explanation(question, last_sql, result.error or "unknown error")
        _record_turn(session_id, question, explanation, last_sql)
        return ChatResponse(
            session_id=session_id,
            reply=explanation,
            sql=last_sql,
            statement_type=result.statement_type,
            error=result.error,
            corrected=corrected,
        )

        # 5. Success - summarise in natural language.
    preview = build_result_preview(result)
    try:
        answer = generate_answer(question, result.statement_type, result.row_count, result.columns, preview)
        answer = sanity_check_answer(answer, result)
    except OllamaError as exc:
        answer = f"The query ran successfully, but I couldn't generate a summary ({exc}). See the results below."

    table_payload = build_table_payload(result)
    _record_turn(session_id, question, answer, last_sql)

    return ChatResponse(
        session_id=session_id,
        reply=answer,
        sql=last_sql,
        statement_type=result.statement_type,
        columns=table_payload["columns"],
        rows=table_payload["rows"],
        row_count=table_payload["row_count"],
        truncated=table_payload["truncated"],
        elapsed_ms=table_payload["elapsed_ms"],
        corrected=corrected,
    )


def _record_turn(session_id: str, question: str, reply: str, sql: str | None) -> None:
    _HISTORY[session_id].append({"role": "user", "content": question, "sql": None})
    _HISTORY[session_id].append({"role": "assistant", "content": reply, "sql": sql})

def _requested_entity_is_available(question: str, tables) -> tuple[bool, str]:
    """
    Prevent the model from answering for an entity that does not exist
    in the current database schema.
    """
    question_lower = question.lower()

    # Convert available table names into a simple lookup set.
    available_tables = {
        table.name.lower()
        for table in tables
    }

    # Common natural-language entity -> possible database table names.
    entity_aliases = {
        "student": ["student", "students"],
        "employee": ["employee", "employees"],
        "department": ["department", "departments"],
        "course": ["course", "courses"],
    }

    for entity, possible_tables in entity_aliases.items():
        # Only check when the entity is explicitly mentioned.
        if entity in question_lower or f"{entity}s" in question_lower:
            if not any(table in available_tables for table in possible_tables):
                return (
                    False,
                    f"I can't answer that because the database does not contain a "
                    f"{entity}s table."
                )

    return True, ""

@app.post("/reset/{session_id}")
def reset_session(session_id: str):
    _HISTORY.pop(session_id, None)
    return {"ok": True}
