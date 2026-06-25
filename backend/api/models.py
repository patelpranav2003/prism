from pydantic import BaseModel, Field
from datetime import datetime
from typing import Literal, Optional


class ConversationMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class QueryRequest(BaseModel):
    question: str
    row_limit: int = Field(default=1000, ge=1, le=10000)
    history: list[ConversationMessage] = Field(default_factory=list)
    correlation_id: Optional[str] = None


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
    last_refresh_utc: Optional[datetime]
    model_count: int
    owner_name: Optional[str] = None
    owner_title: Optional[str] = None
    owner_email: Optional[str] = None
    team_name: Optional[str] = None
    company_name: Optional[str] = None


class RefreshResponse(BaseModel):
    success: bool
    model_count: Optional[int]
    error: Optional[str]


class AuthRequest(BaseModel):
    password: str


class AuthResponse(BaseModel):
    authenticated: bool


class AppIdentityResponse(BaseModel):
    owner_name: str = ""
    owner_title: str = ""
    owner_email: str = ""
    team_name: str = ""
    company_name: str = ""


class AppIdentityRequest(BaseModel):
    password: str
    owner_name: str = ""
    owner_title: str = ""
    owner_email: str = ""
    team_name: str = ""
    company_name: str = ""


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
    last_updated: Optional[datetime]


class SchemaModelDetail(BaseModel):
    name: str
    fqn: str
    layer: Literal["bronze", "silver", "gold"]
    description: str
    grain: str
    columns: list[ColumnMetaSummary]
    row_count: int
    last_updated: Optional[datetime]
    depends_on: list[str]
    tags: list[str]
    compiled_sql_excerpt: str
    parents: list[str]
    children: list[str]
