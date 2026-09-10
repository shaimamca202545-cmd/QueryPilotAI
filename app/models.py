"""models.py - request/response schemas for the FastAPI endpoints."""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)
    session_id: Optional[str] = None


class ChatResponse(BaseModel):
    session_id: str
    reply: str
    sql: Optional[str] = None
    statement_type: Optional[str] = None
    columns: list[str] = Field(default_factory=list)
    rows: list[dict[str, Any]] = Field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    elapsed_ms: float = 0.0
    error: Optional[str] = None
    corrected: bool = False


class HealthResponse(BaseModel):
    status: str
    mysql_connected: bool
    mysql_error: Optional[str] = None
    ollama_connected: bool
    ollama_error: Optional[str] = None
    database: Optional[str] = None
    model: Optional[str] = None


class SchemaResponse(BaseModel):
    database: str
    tables: list[dict[str, Any]]
