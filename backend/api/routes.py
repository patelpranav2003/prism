"""
backend/api/routes.py

FastAPI router with all six Prism REST endpoints.

Endpoints:
  POST /api/query     — Submit a natural language question
  GET  /api/status    — Cache status, model count, last refresh
  POST /api/refresh   — Admin: trigger manual schema refresh
  POST /api/auth      — Admin: validate bcrypt password
  GET  /api/schema    — Full model list for the Schema Explorer
  GET  /api/schema/{model} — Detail panel data for one model

All endpoints inject a correlation ID via the middleware set in main.py.
Error responses follow the shape: {"error": "...", "correlation_id": "..."}

Requirements: 2.6, 6.3, 9.4, 10.1, 10.4, 13.1
"""

from __future__ import annotations

import logging
import time

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, Request, status

from backend.api.models import (
    AuthRequest,
    AuthResponse,
    ColumnMetaSummary,
    QueryRequest,
    QueryResponse,
    RefreshResponse,
    SchemaModelDetail,
    SchemaModelSummary,
    SQLResult as PydanticSQLResult,
    StatusResponse,
)
from backend.exceptions import GenerationError, SecurityError
from backend.logging_config import correlation_id_var

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


# ---------------------------------------------------------------------------
# Dependency helpers — read shared state attached to app.state by main.py
# ---------------------------------------------------------------------------


def get_cache(request: Request):
    return request.app.state.cache


def get_embedder(request: Request):
    return request.app.state.embedder


def get_retriever(request: Request):
    return request.app.state.retriever


def get_prompt_builder_factory(request: Request):
    return request.app.state.prompt_builder_factory


def get_sql_generator_factory(request: Request):
    return request.app.state.sql_generator_factory


def get_query_runner_factory(request: Request):
    return request.app.state.query_runner_factory


def get_config(request: Request):
    return request.app.state.config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _correlation_id() -> str:
    return correlation_id_var.get("")


def _error_response(msg: str, http_status: int = 500) -> HTTPException:
    return HTTPException(
        status_code=http_status,
        detail={"error": msg, "correlation_id": _correlation_id()},
    )


# ---------------------------------------------------------------------------
# POST /api/query
# ---------------------------------------------------------------------------


@router.post("/query", response_model=QueryResponse)
async def query(
    body: QueryRequest,
    cache=Depends(get_cache),
    embedder=Depends(get_embedder),
    retriever=Depends(get_retriever),
    prompt_builder_factory=Depends(get_prompt_builder_factory),
    sql_generator_factory=Depends(get_sql_generator_factory),
    query_runner_factory=Depends(get_query_runner_factory),
    config=Depends(get_config),
) -> QueryResponse:
    """Submit a plain-English question and receive SQL-backed results."""
    cid = _correlation_id()
    logger.info(
        "routes.query: received question[:500]=%r; correlation_id=%s",
        body.question[:500],
        cid,
    )

    # --- Check schema is ready ---
    index = cache.get_index()
    if index is None:
        raise _error_response(
            "Schema is not yet available — please try again in a moment.",
            status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    # --- Embed question ---
    try:
        question_vec = embedder.embed_question(body.question)
    except Exception as exc:
        logger.error("routes.query: embedding failed — %s", exc)
        raise _error_response("Failed to process your question. Please try again.")

    # --- Retrieve relevant models ---
    ranked_models = retriever.retrieve(question_vec, top_n=5)
    if not ranked_models:
        raise _error_response(
            "No relevant models found in the schema for your question.",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    model_names = [rm.model.name for rm in ranked_models]

    # --- Build prompt ---
    prompt_builder = prompt_builder_factory(index)
    system_prompt = prompt_builder.build(ranked_models, body.question)

    # --- Generate SQL ---
    sql_generator = sql_generator_factory(index)
    sql_result = await sql_generator.generate(
        system_prompt=system_prompt,
        question=body.question,
        model_names=model_names,
    )
    if isinstance(sql_result, GenerationError):
        raise _error_response(str(sql_result), status.HTTP_502_BAD_GATEWAY)

    # --- Execute SQL ---
    start_ms = int(time.monotonic() * 1000)
    try:
        query_runner = query_runner_factory(sql_generator)
        rows = await query_runner.execute(
            sql=sql_result.sql,
            row_limit=body.row_limit,
            question=body.question,
            system_prompt=system_prompt,
            model_names=model_names,
        )
    except SecurityError as exc:
        logger.warning("routes.query: DDL/DML blocked — %s", exc)
        raise _error_response(
            "Unable to execute — invalid query type.",
            status.HTTP_400_BAD_REQUEST,
        )
    except RuntimeError as exc:
        raise _error_response(str(exc), status.HTTP_502_BAD_GATEWAY)
    except Exception as exc:
        logger.error("routes.query: execution error — %s", exc)
        raise _error_response(
            "Query execution failed. Please try rephrasing your question."
        )

    execution_time_ms = int(time.monotonic() * 1000) - start_ms

    return QueryResponse(
        sql_result=PydanticSQLResult(
            sql=sql_result.sql,
            explanation=sql_result.explanation,
            models_used=sql_result.models_used,
            confidence=sql_result.confidence,
            confidence_reason=sql_result.confidence_reason,
        ),
        rows=rows,
        row_count=len(rows),
        execution_time_ms=execution_time_ms,
        warehouse_name=config.databricks_sql_warehouse,
        correlation_id=cid,
    )


# ---------------------------------------------------------------------------
# GET /api/status
# ---------------------------------------------------------------------------


@router.get("/status", response_model=StatusResponse)
async def get_status(cache=Depends(get_cache)) -> StatusResponse:
    """Return current cache status, model count, and last refresh time."""
    meta = cache.get_meta()
    return StatusResponse(
        cache_status=meta.status,
        last_refresh_utc=meta.last_refresh_utc,
        model_count=meta.model_count,
    )


# ---------------------------------------------------------------------------
# POST /api/refresh
# ---------------------------------------------------------------------------


@router.post("/refresh", response_model=RefreshResponse)
async def trigger_refresh(
    request: Request,
    body: AuthRequest,
    cache=Depends(get_cache),
    config=Depends(get_config),
) -> RefreshResponse:
    """Admin: trigger a manual schema refresh after password validation."""
    if not _check_password(body.password, config.admin_password_hash):
        raise _error_response("Incorrect password.", status.HTTP_403_FORBIDDEN)

    logger.info("routes.refresh: manual refresh triggered; correlation_id=%s", _correlation_id())
    result = await cache.refresh()

    return RefreshResponse(
        success=result.success,
        model_count=result.model_count,
        error=result.error,
    )


# ---------------------------------------------------------------------------
# POST /api/auth
# ---------------------------------------------------------------------------


@router.post("/auth", response_model=AuthResponse)
async def authenticate(
    body: AuthRequest,
    config=Depends(get_config),
) -> AuthResponse:
    """Validate the admin password without performing any action."""
    valid = _check_password(body.password, config.admin_password_hash)
    if not valid:
        return AuthResponse(authenticated=False)
    return AuthResponse(authenticated=True)


# ---------------------------------------------------------------------------
# GET /api/schema
# ---------------------------------------------------------------------------


@router.get("/schema", response_model=list[SchemaModelSummary])
async def list_schema(cache=Depends(get_cache)) -> list[SchemaModelSummary]:
    """Return the full model list for the Schema Explorer."""
    index = cache.get_index()
    if index is None:
        return []

    return [
        SchemaModelSummary(
            name=m.name,
            fqn=m.fqn,
            layer=m.layer,
            description=m.description,
            column_count=len(m.columns),
            row_count=m.row_count,
            last_updated=m.last_updated,
        )
        for m in index.models
    ]


# ---------------------------------------------------------------------------
# GET /api/schema/{model}
# ---------------------------------------------------------------------------


@router.get("/schema/{model_name}", response_model=SchemaModelDetail)
async def get_schema_model(
    model_name: str,
    cache=Depends(get_cache),
) -> SchemaModelDetail:
    """Return detail panel data for one model."""
    index = cache.get_index()
    if index is None:
        raise _error_response(
            "Schema not available.", status.HTTP_503_SERVICE_UNAVAILABLE
        )

    model = next((m for m in index.models if m.name == model_name), None)
    if model is None:
        raise _error_response(f"Model '{model_name}' not found.", status.HTTP_404_NOT_FOUND)

    lineage = index.lineage.get(model_name)

    return SchemaModelDetail(
        name=model.name,
        fqn=model.fqn,
        layer=model.layer,
        description=model.description,
        grain=model.grain,
        columns=[
            ColumnMetaSummary(name=c.name, data_type=c.data_type, description=c.description)
            for c in model.columns
        ],
        row_count=model.row_count,
        last_updated=model.last_updated,
        depends_on=model.depends_on,
        tags=model.tags,
        compiled_sql_excerpt=model.compiled_sql_excerpt,
        parents=lineage.parents if lineage else [],
        children=lineage.children if lineage else [],
    )


# ---------------------------------------------------------------------------
# Password helper
# ---------------------------------------------------------------------------


def _check_password(plain: str, hashed: str) -> bool:
    """Return True if *plain* matches the bcrypt *hashed* password."""
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except Exception:
        return False
