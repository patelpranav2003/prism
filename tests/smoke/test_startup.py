"""
tests/smoke/test_startup.py

Smoke tests verifying the application can start up correctly.

Checks:
  1. All required environment variables are readable (if present).
  2. Embedder loads the sentence-transformer model in <60s.
  3. FastAPI app can be constructed without crashing.
  4. /api/status returns a valid StatusResponse shape (with a mocked cache).

Requirements: 1.1, 4.7, 14.1, 14.2

These tests are safe to run locally without live secrets — they either skip
gracefully when secrets are absent or mock external dependencies.

Run with:
    pytest tests/smoke/ -v
"""
from __future__ import annotations

import os
import time
from unittest.mock import MagicMock, patch

import pytest

from backend.search.embedder import Embedder


# ---------------------------------------------------------------------------
# Helper — build an AppConfig from env when secrets are present
# ---------------------------------------------------------------------------


def _config_from_env_or_skip():
    """Return AppConfig if all env vars are set, otherwise skip."""
    required = [
        "GITLAB_BASE_URL",
        "GITLAB_PROJECT_ID",
        "GITLAB_TOKEN",
        "DATABRICKS_SQL_WAREHOUSE",
        "ANTHROPIC_API_KEY",
        "ADMIN_PASSWORD_HASH",
    ]
    missing = [v for v in required if not os.environ.get(v)]
    if missing:
        pytest.skip(f"Live secrets not available — missing: {', '.join(missing)}")

    from backend.config import AppConfig
    return AppConfig.from_env()


# ---------------------------------------------------------------------------
# Test 1: Embedder loads within the 60-second startup budget
# ---------------------------------------------------------------------------


def test_embedder_loads_within_60_seconds():
    """Embedder must load the sentence-transformer model in under 60 seconds
    (Requirement 4.7 — app must start within 60s)."""
    embedder = Embedder()
    start = time.monotonic()
    embedder.load()
    elapsed = time.monotonic() - start

    assert elapsed < 60.0, (
        f"Embedder took {elapsed:.1f}s to load — exceeds 60s startup budget"
    )


# ---------------------------------------------------------------------------
# Test 2: Embedder model produces correct output shape
# ---------------------------------------------------------------------------


def test_embedder_produces_384_dim_vector():
    """Embedder must produce a 384-dimensional float32 vector."""
    embedder = Embedder()
    embedder.load()

    vec = embedder.embed_question("What is the total revenue?")

    assert vec.shape == (384,), f"Expected (384,), got {vec.shape}"
    assert vec.dtype.name == "float32"


# ---------------------------------------------------------------------------
# Test 3: FastAPI application constructs without error
# ---------------------------------------------------------------------------


def test_create_app_does_not_crash():
    """create_app() must return a FastAPI instance without raising."""
    # We patch the lifespan so no I/O happens during construction.
    from contextlib import asynccontextmanager
    from unittest.mock import patch

    @asynccontextmanager
    async def _noop_lifespan(app):
        yield

    with patch("backend.main.lifespan", _noop_lifespan):
        from backend.main import create_app
        app = create_app()

    assert app is not None
    assert app.title == "Prism"


# ---------------------------------------------------------------------------
# Test 4: /api/status route shape matches StatusResponse
# ---------------------------------------------------------------------------


def test_status_route_returns_correct_shape():
    """GET /api/status must return cache_status, model_count, last_refresh_utc."""
    from fastapi.testclient import TestClient
    from datetime import datetime, timezone

    # Build a fake CacheMeta
    fake_meta = MagicMock()
    fake_meta.status = "fresh"
    fake_meta.last_refresh_utc = datetime(2026, 1, 1, tzinfo=timezone.utc)
    fake_meta.model_count = 42

    fake_cache = MagicMock()
    fake_cache.get_meta = MagicMock(return_value=fake_meta)

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _noop_lifespan(app):
        yield

    with patch("backend.main.lifespan", _noop_lifespan):
        from backend.main import create_app
        app = create_app()

    # Override the dependency
    from backend.api.routes import get_cache
    app.dependency_overrides[get_cache] = lambda: fake_cache

    client = TestClient(app, raise_server_exceptions=True)
    resp = client.get("/api/status")

    assert resp.status_code == 200
    data = resp.json()
    assert "cache_status" in data
    assert "model_count" in data
    assert "last_refresh_utc" in data
    assert data["cache_status"] in {"fresh", "stale", "unavailable"}
    assert isinstance(data["model_count"], int)


# ---------------------------------------------------------------------------
# Test 5: AppConfig.from_env() reads all required variables (live secrets test)
# ---------------------------------------------------------------------------


def test_app_config_from_env_reads_all_secrets():
    """When secrets are injected via env, AppConfig.from_env() must read them all."""
    config = _config_from_env_or_skip()

    assert config.gitlab_base_url
    assert config.gitlab_project_id
    assert config.gitlab_token
    assert config.databricks_sql_warehouse
    assert config.anthropic_api_key
    assert config.admin_password_hash
