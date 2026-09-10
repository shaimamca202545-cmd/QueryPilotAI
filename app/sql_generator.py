"""
sql_generator.py
-----------------
All communication with the local Ollama runtime (llama3.2) lives here.
No other LLM provider is used anywhere in this app.
"""

from __future__ import annotations

import os
import re

import httpx

from app.prompts import build_answer_prompt, build_error_prompt, build_sql_prompt

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2")
_TIMEOUT = httpx.Timeout(60.0, connect=5.0)

_FENCE_RE = re.compile(r"```(?:sql)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)


class OllamaError(RuntimeError):
    """Raised when the local Ollama server is unreachable or returns an error."""


def _generate(prompt: str, *, temperature: float = 0.1) -> str:
    """Call Ollama's /api/generate endpoint (non-streaming) and return raw text."""
    try:
        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.post(
                f"{OLLAMA_HOST}/api/generate",
                json={
                    "model": OLLAMA_MODEL,
                    "prompt": prompt,
                    "stream": False,
                    "options": {"temperature": temperature},
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return (data.get("response") or "").strip()
    except httpx.ConnectError as exc:
        raise OllamaError(
            f"Could not reach Ollama at {OLLAMA_HOST}. Is 'ollama serve' running "
            f"and is the '{OLLAMA_MODEL}' model pulled?"
        ) from exc
    except httpx.HTTPStatusError as exc:
        raise OllamaError(f"Ollama returned an error: {exc.response.status_code} {exc.response.text}") from exc
    except httpx.TimeoutException as exc:
        raise OllamaError("Ollama timed out generating a response.") from exc


def _clean_sql(raw: str) -> str:
    """Strip markdown code fences / stray prose the model might add despite instructions."""
    text = raw.strip()
    match = _FENCE_RE.search(text)
    if match:
        text = match.group(1).strip()
    # If the model added a leading explanation line before the statement, try to
    # find the first SQL keyword and cut from there.
    keywords = (
        "SELECT", "INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP",
        "TRUNCATE", "REPLACE", "SHOW", "DESCRIBE", "WITH",
    )
    upper = text.upper()
    earliest = None
    for kw in keywords:
        idx = upper.find(kw)
        if idx != -1 and (earliest is None or idx < earliest):
            earliest = idx
    if earliest is not None and earliest > 0:
        text = text[earliest:]
    text = text.strip().strip("`").strip()
    if not text.endswith(";"):
        text += ";"
    return text


def generate_sql(
    schema_text: str,
    history_text: str,
    question: str,
    previous_sql: str | None = None,
    error: str | None = None,
) -> str:
    """Ask llama3.2 for a single MySQL statement answering `question`."""
    system_prompt = build_sql_prompt(schema_text, history_text, previous_sql, error)
    full_prompt = f"{system_prompt}\n\nUSER REQUEST: {question}\n\nSQL:"
    raw = _generate(full_prompt, temperature=0.0)
    return _clean_sql(raw)


def generate_answer(question: str, statement_type: str, row_count: int, columns: list[str], result_preview: str) -> str:
    """Ask llama3.2 to turn a query result into a natural-language answer."""
    prompt = build_answer_prompt(question, statement_type, row_count, columns, result_preview)
    answer = _generate(prompt, temperature=0.0)
    return answer or "I ran the query, but I couldn't summarise the result. See the details below."


def generate_error_explanation(question: str, sql: str, error: str) -> str:
    """Ask llama3.2 to translate a MySQL error into a plain-language explanation."""
    prompt = build_error_prompt(question, sql, error)
    try:
        text = _generate(prompt, temperature=0.2)
        return text or _fallback_error_text(error)
    except OllamaError:
        return _fallback_error_text(error)


def _fallback_error_text(error: str) -> str:
    return (
        "I couldn't complete that request because the database rejected the query "
        f"({error}). Could you rephrase it or double-check the table/column names?"
    )


def check_ollama_health() -> tuple[bool, str | None]:
    try:
        with httpx.Client(timeout=httpx.Timeout(5.0)) as client:
            resp = client.get(f"{OLLAMA_HOST}/api/tags")
            resp.raise_for_status()
            models = [m.get("name", "") for m in resp.json().get("models", [])]
            if not any(OLLAMA_MODEL in m for m in models):
                return False, f"Model '{OLLAMA_MODEL}' not found in Ollama. Run: ollama pull {OLLAMA_MODEL}"
            return True, None
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
