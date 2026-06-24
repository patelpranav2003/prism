"""
backend/main.py

FastAPI application entry point for Prism.

Startup lifecycle (Requirement 1.1, 2.2, 4.7):
  1. Configure structured JSON logging.
  2. Load AppConfig from environment variables.
  3. Load the sentence-transformer embedder model.
  4. Trigger an immediate CacheManager.refresh() to fetch and index dbt artifacts.
  5. Start the 6-hour background refresh loop.
  6. Attach all shared state to app.state for route dependencies.

Middleware:
  - Correlation ID injection: UUID4 per request, set in ContextVar, echoed in response headers.

Static file serving:
  - Compiled React app is served from ``frontend/dist/`` under ``/``.
  - The ``/api/*`` prefix routes to the FastAPI router first.

Requirements: 1.1, 2.2, 4.7, 12.7, 14.1, 14.2
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend.api.routes import router
from backend.config import AppConfig
from backend.discovery.cache_manager import CacheManager
from backend.discovery.gitlab_fetcher import ArtifactFetcher
from backend.discovery.index_builder import IndexBuilder
from backend.generation.prompt_builder import PromptBuilder
from backend.generation.sql_generator import SQLGenerator
from backend.logging_config import configure_logging, correlation_id_var
from backend.search.embedder import Embedder
from backend.search.retriever import Retriever

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lifespan (startup / shutdown)
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan handler — runs startup logic, then yields."""
    # --- Logging ---
    configure_logging(level=os.environ.get("LOG_LEVEL", "INFO"))

    # --- Configuration ---
    logger.info("Prism: loading application configuration")
    config = AppConfig.from_env()

    # --- Embedder ---
    logger.info("Prism: loading sentence-transformer model")
    embedder = Embedder()
    embedder.load()

    # --- Discovery pipeline ---
    fetcher = ArtifactFetcher(config)
    index_builder = IndexBuilder()
    cache = CacheManager(config, fetcher, index_builder, embedder)

    # --- Initial refresh (synchronous first call) ---
    logger.info("Prism: performing initial schema refresh")
    await cache.refresh()

    # --- Background refresh loop ---
    cache.start_background_refresh()

    # --- Retriever ---
    retriever = Retriever(cache)

    # --- Factories for per-request objects (need the active index) ---
    def prompt_builder_factory(index):
        return PromptBuilder(index)

    def sql_generator_factory(index):
        return SQLGenerator(config, index)

    def query_runner_factory(sql_gen):
        from backend.execution.databricks_runner import QueryRunner
        return QueryRunner(config, sql_gen)

    # --- Attach to app.state ---
    app.state.config = config
    app.state.embedder = embedder
    app.state.cache = cache
    app.state.retriever = retriever
    app.state.prompt_builder_factory = prompt_builder_factory
    app.state.sql_generator_factory = sql_generator_factory
    app.state.query_runner_factory = query_runner_factory

    logger.info("Prism: startup complete")
    yield

    logger.info("Prism: shutting down")


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------


def create_app() -> FastAPI:
    """Construct and configure the FastAPI application."""
    app = FastAPI(
        title="Prism",
        description="Natural language analytics assistant for Databricks",
        version="1.0.0",
        lifespan=lifespan,
    )

    # --- Correlation ID middleware ---
    @app.middleware("http")
    async def inject_correlation_id(request: Request, call_next) -> Response:
        cid = request.headers.get("X-Correlation-ID") or str(uuid4())
        token = correlation_id_var.set(cid)
        try:
            response = await call_next(request)
        finally:
            correlation_id_var.reset(token)
        response.headers["X-Correlation-ID"] = cid
        return response

    # --- CORS (for Vite dev proxy) ---
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # --- API routes ---
    app.include_router(router)

    # --- Static React SPA ---
    _dist = os.path.join(os.path.dirname(__file__), "..", "frontend", "dist")
    if os.path.isdir(_dist):
        # Serve compiled assets (JS/CSS/images) under /assets
        _assets = os.path.join(_dist, "assets")
        if os.path.isdir(_assets):
            app.mount("/assets", StaticFiles(directory=_assets), name="static-assets")

        # Catch-all: serve index.html for all non-API paths so React Router works.
        # API routes registered above take priority; this only fires for unmatched paths.
        @app.get("/{full_path:path}")
        async def spa_fallback(full_path: str) -> FileResponse:
            # Serve exact files (favicon.ico, robots.txt, etc.) when they exist
            candidate = os.path.join(_dist, full_path)
            if full_path and os.path.isfile(candidate):
                return FileResponse(candidate)
            return FileResponse(os.path.join(_dist, "index.html"))
    else:
        logger.warning(
            "Prism: frontend/dist not found — static file serving disabled. "
            "Run `npm run build` in the frontend/ directory."
        )

    return app


# ---------------------------------------------------------------------------
# WSGI / ASGI entry point
# ---------------------------------------------------------------------------

app = create_app()
