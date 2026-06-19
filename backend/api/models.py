from pydantic import BaseModel, Field
from datetime import datetime
from typing import Literal


class QueryRequest(BaseModel):
    question: str
    row_limit: int = Field(default=1000, ge=1, le=10000)
    correlation_id: str | None = None


class SQLResult(BaseModel):
    sql: str
    explanation: str
    models_used: list[str]
    confidence: Literal["high", "medium", "low"]
    confidence_reason: str


class QueryResponse(BaseModel):
    sql_result: SQLResult
    rows: list[dict]
    row_count: int
    execution_time_ms: int
    warehouse_name: str
    correlation_id: str


class StatusResponse(BaseModel):
    cache_status: Literal["fresh", "stale", "unavailable"]
    last_refresh_utc: datetime | None
    model_count: int


class RefreshResponse(BaseModel):
    success: bool
    model_count: int | None
    error: str | None


class AuthRequest(BaseModel):
    password: str


class AuthResponse(BaseModel):
    authenticated: bool


class ColumnMetaSummary(BaseModel):
    name: str
    data_type: str
    description: str


class SchemaModelSummary(BaseModel):
    name: str
    fqn: str
    layer: Literal["bronze", "silver", "gold"]
    description: str
    column_count: int
    row_count: int
    last_updated: datetime | None


class SchemaModelDetail(BaseModel):
    name: str
    fqn: str
    layer: Literal["bronze", "silver", "gold"]
    description: str
    grain: str
    columns: list[ColumnMetaSummary]
    row_count: int
    last_updated: datetime | None
    depends_on: list[str]
    tags: list[str]
    compiled_sql_excerpt: str
    parents: list[str]
    children: list[str]
