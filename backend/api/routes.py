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
import re
import time

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, Request, status

from backend.api.models import (
    AppIdentityRequest,
    AppIdentityResponse,
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


def get_settings_store(request: Request):
    return request.app.state.settings_store


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _correlation_id() -> str:
    return correlation_id_var.get("")


# Matches catalog.schema.table FQNs with optional backtick quoting,
# e.g. marketing-low.amazon_dsp_bronze.product or `marketing-low`.`amazon_dsp_silver`.`ad__day`
_FQN_RE = re.compile(r"`?([\w-]+)`?\.`?([\w_]+)`?\.`?([\w_]+)`?")

# Short common English words that carry no domain signal for model lookup
_KEYWORD_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "for", "to", "in", "of", "on", "at", "by",
    "is", "are", "was", "do", "we", "our", "me", "my", "it", "its", "have",
    "has", "can", "you", "all", "any", "how", "why", "what", "when", "which",
    "who", "give", "get", "show", "list", "find", "use", "make", "need",
    "with", "from", "into", "out", "up", "as", "be", "this", "that", "not",
})


def _extract_fqns(text: str) -> list[str]:
    """Return all catalog.schema.table references found in *text*."""
    return [f"{m.group(1)}.{m.group(2)}.{m.group(3)}" for m in _FQN_RE.finditer(text)]


def _domain_keywords(text: str) -> list[str]:
    """Extract significant domain words (3+ chars, not stopwords) from *text*."""
    return [
        w for w in re.findall(r"[a-z]+", text.lower())
        if len(w) >= 3 and w not in _KEYWORD_STOPWORDS
    ]


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

    # --- Embed question (enrich with prior user questions for retrieval) ---
    # Current question leads so its domain signal (e.g. "amazon dsp") drives
    # retrieval. Prior USER questions add context for domain-ambiguous follow-ups
    # ("show this for all brands"). Assistant messages are excluded — their table
    # names anchor retrieval to the prior domain and drown out domain switches.
    try:
        embedding_query = body.question
        if body.history:
            prior_user = [m.content for m in body.history if m.role == "user"]
            if prior_user:
                embedding_query = body.question + " | " + " | ".join(prior_user)
        question_vec = embedder.embed_question(embedding_query)
    except Exception as exc:
        logger.error("routes.query: embedding failed — %s", exc)
        raise _error_response("Failed to process your question. Please try again.")

    # --- Retrieve relevant models ---
    ranked_models = retriever.retrieve(question_vec, top_n=20)
    if not ranked_models:
        raise _error_response(
            "No relevant models found in the schema for your question.",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    from backend.models import RankedModel
    MAX_MODELS = 40
    model_by_name = {m.name: m for m in index.models}
    existing_names = {rm.model.name for rm in ranked_models}

    # --- Keyword-based retrieval enrichment ---
    # Semantic similarity alone can miss models when a domain keyword ("amazon dsp",
    # "walmart connect") appears in the question. We extract words from the question,
    # keep only those that actually appear in at least one model FQN (filtering out
    # generic English words like "models", "clicks", "total" that never appear in
    # FQNs), then add every model whose FQN contains ALL surviving keywords.
    # No cap here — all matching domain models are always included.
    all_fqns_lower = {m.fqn.lower().replace("-", "_") for m in index.models}
    raw_kws = _domain_keywords(body.question)
    domain_kws = [w for w in raw_kws if any(w in fqn for fqn in all_fqns_lower)]
    if domain_kws:
        for m in index.models:
            fqn_lower = m.fqn.lower().replace("-", "_")
            if all(kw in fqn_lower for kw in domain_kws) and m.name not in existing_names:
                ranked_models.append(
                    RankedModel(model=m, raw_similarity=0.2, adjusted_score=0.2, confidence_hint=None)
                )
                existing_names.add(m.name)

    # --- Inject any tables the user explicitly named in their question ---
    # Technical instruction messages ("use table X joined on Y") have poor
    # embedding signal. Extracting FQNs directly ensures the named tables are
    # always in the prompt regardless of retrieval score.
    explicit_fqns = _extract_fqns(body.question)
    if explicit_fqns:
        model_by_fqn = {m.fqn: m for m in index.models}
        for fqn in explicit_fqns:
            m = model_by_fqn.get(fqn)
            if m and m.name not in existing_names:
                ranked_models.append(
                    RankedModel(model=m, raw_similarity=1.0, adjusted_score=1.0, confidence_hint=None)
                )
                existing_names.add(m.name)

    # --- Expand depends_on parents via full BFS (no hop limit, model cap) ---
    # Single-level expansion misses dimension tables that are 2+ hops away
    # (e.g. ad__day → campaign → advertiser). A fixed hop count is fragile
    # for deep dbt lineages. Instead we do a full BFS across all ancestor
    # levels, stopping only when we run out of new parents or hit MAX_MODELS.
    frontier = list(ranked_models)
    while frontier and len(ranked_models) < MAX_MODELS:
        next_frontier = []
        for rm in frontier:
            for dep in rm.model.depends_on:
                if len(ranked_models) >= MAX_MODELS:
                    break
                dep_name = dep.rsplit(".", 1)[-1] if "." in dep else dep
                if dep_name not in existing_names:
                    dep_model = model_by_name.get(dep_name)
                    if dep_model is not None:
                        new_rm = RankedModel(
                            model=dep_model,
                            raw_similarity=0.0,
                            adjusted_score=0.0,
                            confidence_hint=None,
                        )
                        ranked_models.append(new_rm)
                        next_frontier.append(new_rm)
                        existing_names.add(dep_name)
        frontier = next_frontier

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
        history=body.history or None,
    )
    if isinstance(sql_result, GenerationError):
        raise _error_response(str(sql_result), status.HTTP_502_BAD_GATEWAY)

    # --- Execute SQL (skip for conversational / schema-exploration responses) ---
    execution_time_ms = 0
    rows: list[dict] = []
    if sql_result.sql.strip():
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
async def get_status(
    cache=Depends(get_cache),
    config=Depends(get_config),
    settings_store=Depends(get_settings_store),
) -> StatusResponse:
    """Return current cache status, model count, last refresh time, and app identity."""
    meta = cache.get_meta()
    identity = settings_store.load()
    return StatusResponse(
        cache_status=meta.status,
        last_refresh_utc=meta.last_refresh_utc,
        model_count=meta.model_count,
        owner_name=identity.owner_name or config.owner_name or None,
        owner_title=identity.owner_title or config.owner_title or None,
        owner_email=identity.owner_email or config.owner_email or None,
        team_name=identity.team_name or config.team_name or None,
        company_name=identity.company_name or config.company_name or None,
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
# GET /api/settings/info  (public — identity is already shown in the UI)
# POST /api/settings/info (admin only — requires password)
# ---------------------------------------------------------------------------


@router.get("/settings/info", response_model=AppIdentityResponse)
async def get_app_identity(
    settings_store=Depends(get_settings_store),
    config=Depends(get_config),
) -> AppIdentityResponse:
    """Return the current app identity fields (settings store takes priority over env vars)."""
    identity = settings_store.load()
    return AppIdentityResponse(
        owner_name=identity.owner_name or config.owner_name,
        owner_title=identity.owner_title or config.owner_title,
        owner_email=identity.owner_email or config.owner_email,
        team_name=identity.team_name or config.team_name,
        company_name=identity.company_name or config.company_name,
    )


@router.post("/settings/info", response_model=AppIdentityResponse)
async def save_app_identity(
    body: AppIdentityRequest,
    settings_store=Depends(get_settings_store),
    config=Depends(get_config),
) -> AppIdentityResponse:
    """Admin: persist app identity fields. Requires the admin password."""
    if not _check_password(body.password, config.admin_password_hash):
        raise _error_response("Incorrect password.", status.HTTP_403_FORBIDDEN)

    from backend.settings_store import AppIdentity
    identity = AppIdentity(
        owner_name=body.owner_name,
        owner_title=body.owner_title,
        owner_email=body.owner_email,
        team_name=body.team_name,
        company_name=body.company_name,
    )
    try:
        settings_store.save(identity)
    except RuntimeError as exc:
        raise _error_response(str(exc))

    return AppIdentityResponse(
        owner_name=identity.owner_name,
        owner_title=identity.owner_title,
        owner_email=identity.owner_email,
        team_name=identity.team_name,
        company_name=identity.company_name,
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
