# Implementation Plan: Prism

## Overview

Implement Prism as a Databricks App with a Python/FastAPI backend and a React/TypeScript frontend. The build proceeds in layers: project scaffolding → discovery pipeline (artifact fetch, parsing, index building, caching) → search (embedder, retriever) → generation (prompt builder, SQL generator) → execution (query runner) → API layer → React frontend. Each phase wires directly into the next, so there is no orphaned code.

---

## Tasks

- [x] 1. Project scaffolding and configuration
  - Create the full directory tree (`backend/`, `frontend/src/`, `tests/unit/`, `tests/integration/`, `tests/smoke/`)
  - Write `backend/config.py`: `AppConfig` dataclass populated from environment variables; `mask_secret()` and `display_token()` helpers
  - Write `app.yaml` (uvicorn entry point + secret scope references), `.env.example`, and `README.md` (four-step deploy guide)
  - Write `requirements.txt` pinning: `fastapi`, `uvicorn`, `httpx`, `anthropic`, `databricks-sql-connector`, `sentence-transformers`, `numpy`, `hypothesis`, `pytest`, `bcrypt`
  - Set up `pytest.ini` / `pyproject.toml` with Hypothesis profile `prism` (100 examples, suppress too-slow)
  - Set up `frontend/` with Vite + React + TypeScript (`npm create vite@latest`)
  - _Requirements: 14.1, 14.2, 14.3, 14.5_

- [x] 2. Data models
  - [x] 2.1 Implement `ColumnMeta`, `ModelMeta`, `LineageNode`, `ArtifactBundle`, `SchemaIndex`, `RankedModel`, `CacheState`, `SQLResult` dataclasses in `backend/` (shared types module)
    - Implement all dataclass fields exactly as specified in the design
    - _Requirements: 3.2, 4.2, 6.1_
  - [x] 2.2 Implement Pydantic API models in `backend/api/models.py`: `QueryRequest`, `QueryResponse`, `SQLResult`, `StatusResponse`, `RefreshResponse`
    - Enforce `row_limit` field constraint `ge=1, le=10000`
    - _Requirements: 6.4, 13.1_
  - [x] 2.3 Write property test for `SchemaIndex` and `RankedModel` construction (Hypothesis `st.builds`)
    - **Property 4: Schema Merge Fidelity**
    - **Validates: Requirements 3.4, 15.2**

- [x] 3. Artifact fetcher
  - [x] 3.1 Implement `ArtifactFetcher` in `backend/discovery/gitlab_fetcher.py`
    - `fetch_all()` and `_fetch_one()` using `httpx.AsyncClient` + `asyncio.gather`, 30-second timeout
    - Auth: `PRIVATE-TOKEN` header from Databricks secret scope
    - URL pattern: `{base_url}/projects/{project_id}/jobs/artifacts/main/raw/public/{filename}?job=pages`
    - 401/403 or missing token → log masked info, return `FetchError`
    - Partial failure → log per-file, return partial bundle
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 11.1_
  - [x] 3.2 Write property test for URL construction
    - **Property 1: Artifact URL Construction**
    - **Validates: Requirements 1.2**

- [x] 4. Schema parsers and index builder
  - [x] 4.1 Implement `ManifestParser.parse()` in `backend/discovery/manifest_parser.py`
    - Extract all fields listed in design; zero-value fallback on missing/null with WARN log
    - Raise `ParseError` on top-level JSON failure
    - _Requirements: 3.2, 3.7_
  - [x] 4.2 Implement `CatalogParser.merge()` in `backend/discovery/catalog_parser.py`
    - Override column types from catalog; set `row_count` from catalog stats; no case transformation on column names
    - Models absent from catalog: retain manifest types, `row_count = 0`
    - _Requirements: 3.4, 15.1, 15.2_
  - [x] 4.3 Implement `GraphParser.parse()` in `backend/discovery/graph_parser.py`
    - Returns `dict[str, LineageNode]`; zero-value fallback for missing nodes
    - _Requirements: 3.5_
  - [x] 4.4 Implement `IndexBuilder.build()` in `backend/discovery/index_builder.py`
    - Orchestrate manifest parse → catalog merge → graph parse → layer inference → grain inference
    - Layer inference (priority): tag → folder path → default `"bronze"`
    - Grain inference (priority): GROUP BY → DISTINCT → `_by_` suffix → `"unknown"`
    - On any artifact total parse failure: preserve previous `SchemaIndex`, log file + error; if no prior index, set `"unavailable"`
    - _Requirements: 3.1, 3.3, 3.6, 3.7, 3.8_
  - [x] 4.5 Write property test for layer inference priority
    - **Property 3: Layer Inference Priority Order**
    - **Validates: Requirements 3.3**
  - [x] 4.6 Write property test for zero-value fallback on missing fields
    - **Property 5: Missing Field Zero-Value Handling**
    - **Validates: Requirements 3.7**
  - [x] 4.7 Write property test for column name round-trip preservation
    - **Property 6: Column Name Preservation (Round-Trip Fidelity)**
    - **Validates: Requirements 3.2, 15.1**
  - [x] 4.8 Write property test for schema merge fidelity
    - **Property 4: Schema Merge Fidelity**
    - **Validates: Requirements 3.4, 15.2**

- [x] 5. Cache manager
  - [x] 5.1 Implement `CacheManager` in `backend/discovery/cache_manager.py`
    - `get_index()`, `get_status()`, `get_meta()`, `refresh()`, `swap_index()` (atomic via `asyncio.Lock`)
    - Background refresh loop: `asyncio.create_task` on 6-hour cycle; 5-minute retry on failure
    - While refreshing: serve previous good index for all reads
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.7_
  - [x] 5.2 Write property test for cache status response completeness
    - **Property 2: Cache Status Response Completeness**
    - **Validates: Requirements 2.7**

- [x] 6. Checkpoint — Ensure all discovery and cache tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 7. Embedder and retriever
  - [x] 7.1 Implement `Embedder` in `backend/search/embedder.py`
    - `load()` called once at startup; `embed_models()` returns `np.ndarray` shape `(N, 384)`; `embed_question()` returns `(384,)`
    - Text representation: `"{model_name}: {description}. Columns: {col1} ({type1}) {desc1}, ..."`
    - _Requirements: 4.1, 4.2, 4.3, 4.7, 13.4_
  - [x] 7.2 Implement `Retriever.retrieve()` in `backend/search/retriever.py`
    - Cosine similarity via numpy; Gold +0.05, Silver +0.025 boosts; return top-`min(5, N)` sorted by `adjusted_score` desc
    - If all raw scores < 0.1: still return top-N with `confidence_hint="low"`
    - Must complete within 2s for ≤500 models
    - _Requirements: 4.4, 4.5, 4.6, 4.8_
  - [x] 7.3 Write property test for model text representation completeness
    - **Property 7: Model Text Representation Completeness**
    - **Validates: Requirements 4.1**
  - [x] 7.4 Write property test for retrieval ranking by adjusted score
    - **Property 8: Retrieval Ranking by Adjusted Score**
    - **Validates: Requirements 4.4**
  - [x] 7.5 Write property test for layer score boost computation
    - **Property 9: Layer Score Boost Computation**
    - **Validates: Requirements 4.5**

- [x] 8. Prompt builder
  - [x] 8.1 Implement `PromptBuilder.build()` in `backend/generation/prompt_builder.py`
    - Schema block per model: FQN, all columns (≤300) with types + descriptions, grain, layer, compiled SQL excerpt
    - Lineage block: parent/child relationships from adjacency list
    - Dialect rules block: FQN names, backtick columns, `DATE_TRUNC`/`DATEADD`/`DATEDIFF`/`QUALIFY`/`LIMIT 1000`, no `SELECT *`
    - Deduplication instruction when grain is `"unknown"` OR no GROUP BY/DISTINCT/`_by_` pattern in compiled SQL
    - Log WARNING + model name + column count when model has >300 columns; include first 300
    - _Requirements: 5.1, 5.2, 5.3, 15.3_
  - [x] 8.2 Write property test for prompt schema content completeness
    - **Property 10: Prompt Schema Content Completeness**
    - **Validates: Requirements 5.1, 5.2**
  - [x] 8.3 Write property test for deduplication instruction injection
    - **Property 11: Deduplication Instruction Injection**
    - **Validates: Requirements 5.3**
  - [x] 8.4 Write property test for column truncation at 300
    - **Property 23: Prompt Column Truncation**
    - **Validates: Requirements 15.3**

- [x] 9. SQL generator
  - [x] 9.1 Implement `SQLGenerator.generate()` in `backend/generation/sql_generator.py`
    - Call Claude API (`claude-sonnet-4-6`, `max_tokens=2000`); parse structured JSON response
    - Validate all five required fields (`sql`, `explanation`, `models_used`, `confidence`, `confidence_reason`) with correct types
    - Non-200, timeout (30s), or invalid JSON → return `GenerationError` with user-facing message
    - Post-generation: extract column refs from SQL, cross-check against `SchemaIndex`; unrecognised → WARN + set `confidence="low"` + include in `confidence_reason`
    - Log: question[:500], selected model names, Claude model ID, token count, outcome
    - _Requirements: 5.4, 5.5, 5.6, 15.4, 15.5_
  - [x] 9.2 Write property test for Claude response validation
    - **Property 12: Claude Response Validation**
    - **Validates: Requirements 5.5**
  - [x] 9.3 Write property test for unrecognised column handling
    - **Property 24: Unrecognised Column Handling**
    - **Validates: Requirements 15.4, 15.5**

- [x] 10. Query runner
  - [x] 10.1 Implement `QueryRunner.execute()` in `backend/execution/databricks_runner.py`
    - `databricks-sql-connector` with workspace OAuth; stream rows via cursor iteration as `AsyncIterator[ResultRow]`
    - Enforce `row_limit` in [1, 10000]; inject `LIMIT {row_limit}` if not already present
    - DDL/DML guard: `PROHIBITED` regex (word-boundary, case-insensitive) → raise `SecurityError` before execution
    - Auto-retry once on warehouse error: call `SQLGenerator.generate()` with original question + failed SQL + error msg; double failure → surface safe error + failed SQL in copyable block
    - Fallback to workspace default warehouse if `DATABRICKS_SQL_WAREHOUSE` is invalid; log WARN
    - Log: SQL[:2000], warehouse ID, execution time ms, row count, outcome
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7, 6.8, 11.2, 11.5, 14.4_
  - [x] 10.2 Write property test for row limit enforcement
    - **Property 13: Row Limit Enforcement**
    - **Validates: Requirements 6.4**
  - [x] 10.3 Write property test for DDL/DML blocking
    - **Property 20: DDL/DML Blocking**
    - **Validates: Requirements 11.5**

- [x] 11. Checkpoint — Ensure all backend logic tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 12. Observability: structured logging and secret masking
  - [x] 12.1 Implement structured log formatter in `backend/` (e.g., `logging_config.py`)
    - JSON log entries containing: UTC timestamp, severity, component name, error type, correlation ID, human-readable message
    - `mask_secret()` and `display_token()` helpers wired into all log calls that reference secrets
    - `contextvars.ContextVar` for correlation ID; middleware sets it per request
    - _Requirements: 11.3, 11.4, 12.1, 12.4, 12.5, 12.6, 12.7_
  - [x] 12.2 Write property test for token masking
    - **Property 19: Token Masking**
    - **Validates: Requirements 10.3, 11.3, 11.4**
  - [x] 12.3 Write property test for structured log entry completeness
    - **Property 21: Structured Log Entry Completeness**
    - **Validates: Requirements 12.1, 12.4, 12.5, 12.6**
  - [x] 12.4 Write property test for correlation ID propagation
    - **Property 22: Correlation ID Propagation**
    - **Validates: Requirements 12.7**

- [x] 13. FastAPI routes and middleware
  - [x] 13.1 Implement correlation ID middleware in `backend/main.py`
    - UUID4 per request; inject into `ContextVar`, response headers, and JSON body
    - _Requirements: 12.7_
  - [x] 13.2 Implement all API routes in `backend/api/routes.py`
    - `POST /api/query`: embed question → retrieve → build prompt → generate SQL → execute → return `QueryResponse`
    - `GET /api/status`: return `StatusResponse` from `CacheManager`
    - `POST /api/refresh`: admin-triggered manual refresh; return `RefreshResponse`
    - `POST /api/auth`: validate bcrypt admin password hash
    - `GET /api/schema`: full model list for Schema_Explorer
    - `GET /api/schema/{model}`: detail panel data for one model
    - Consistent `{"error": "...", "correlation_id": "..."}` error shape on all failure paths
    - Mount compiled React static files at `/`
    - _Requirements: 2.6, 6.3, 9.4, 10.1, 10.4, 13.1_
  - [x] 13.3 Wire startup lifecycle in `backend/main.py`
    - On startup: load embedder model, trigger `CacheManager.refresh()`, start 6-hour background loop
    - Expose `/api/status` as the health surface polled by the frontend
    - _Requirements: 1.1, 2.2, 4.7_

- [x] 14. Checkpoint — Ensure all API layer tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 15. React frontend — core components
  - [x] 15.1 Implement `SearchBar` component (`frontend/src/components/SearchBar.tsx`)
    - Text input with placeholder "Ask anything about your data..."; example question chips (4–6) that populate and auto-submit the input
    - Disable input and chips while schema is loading; re-enable when status turns available
    - _Requirements: 8.1, 8.2, 8.4_
  - [x] 15.2 Implement `ConfidenceIndicator` component (`frontend/src/components/ConfidenceIndicator.tsx`)
    - Map `"high"` → green "High", `"medium"` → amber "Medium", `"low"` → red "Low"
    - Accessible ARIA label on the badge
    - _Requirements: 7.4_
  - [x] 15.3 Implement `SchemaHealthBar` component (`frontend/src/components/SchemaHealthBar.tsx`)
    - `"fresh"`: model count + elapsed time; `"stale"`: model count + warning label; `"unavailable"`: "Schema unavailable — contact your data team"
    - _Requirements: 8.3_
  - [x] 15.4 Write Vitest unit tests for `ConfidenceIndicator` display mapping
    - **Property 15: Confidence Indicator Display Mapping**
    - **Validates: Requirements 7.4**
  - [x] 15.5 Write Vitest unit tests for `SchemaHealthBar` display mapping
    - **Property 16: Schema Health Indicator Display Mapping**
    - **Validates: Requirements 8.3**

- [x] 16. React frontend — results and SQL display components
  - [x] 16.1 Implement `ResultsTable` component (`frontend/src/components/ResultsTable.tsx`)
    - Sortable table (click column header → asc/desc toggle); streaming row display; "Download CSV" button exporting UTF-8 CSV with headers as `prism_results_{timestamp}.csv`
    - _Requirements: 7.1, 7.2_
  - [x] 16.2 Implement `SQLViewer` component (`frontend/src/components/SQLViewer.tsx`)
    - Syntax-highlighted SQL display; copy-to-clipboard button
    - _Requirements: 7.3_
  - [x] 16.3 Implement `ExplanationPanel` component (`frontend/src/components/ExplanationPanel.tsx`)
    - Collapsible "How I answered this" section (collapsed by default); plain-English explanation; `models_used` as clickable tags that scroll Schema_Explorer to the selected model; embeds `SQLViewer` and `ConfidenceIndicator`
    - Non-dismissable low-confidence inline warning banner when `confidence === "low"`
    - _Requirements: 5.7, 7.3, 7.4_
  - [x] 16.4 Write Vitest unit test for CSV export correctness
    - **Property 14: CSV Export Correctness**
    - **Validates: Requirements 7.2**

- [x] 17. React frontend — Schema Explorer
  - [x] 17.1 Implement `SchemaExplorer` component (`frontend/src/components/SchemaExplorer.tsx`)
    - Collapsible sidebar; hidden by default at <768px, visible at ≥768px; toggle button on any viewport
    - Three collapsible sections: Gold → Silver → Bronze (in that display order), each expanded by default
    - Search input filtering within 300ms (debounce); case-insensitive match on model name or any column name
    - Model detail panel (hidden until selection): description, columns (name + type + description), grain, last-updated, row count
    - Reflects current `SchemaIndex` state; updates within 5s of index refresh
    - _Requirements: 9.1, 9.2, 9.3, 9.4, 9.5_
  - [x] 17.2 Write Vitest unit tests for Schema_Explorer model grouping
    - **Property 17: Schema_Explorer Model Grouping**
    - **Validates: Requirements 9.2**
  - [x] 17.3 Write Vitest unit tests for Schema_Explorer search filtering
    - **Property 18: Schema_Explorer Search Filtering**
    - **Validates: Requirements 9.3**

- [x] 18. React frontend — pages and routing
  - [x] 18.1 Implement `Home` page (`frontend/src/pages/Home.tsx`)
    - ZURU logo + "Prism" in header; white background; centered `SearchBar`; `SchemaHealthBar`; loading state when schema not ready
    - _Requirements: 8.1, 8.3, 8.4_
  - [x] 18.2 Implement `Results` page (`frontend/src/pages/Results.tsx`)
    - `ResultsTable` (first element); `ExplanationPanel` below; "Refine your question" input pre-populated with previous question; `SchemaExplorer` sidebar; query metadata row (row count, execution time, warehouse name)
    - _Requirements: 6.5, 7.1, 7.3, 7.5_
  - [x] 18.3 Implement `Settings` page (`frontend/src/pages/Settings.tsx`)
    - Password prompt gate; no settings DOM rendered until authenticated; wrong password → inline "Incorrect password" message
    - Authenticated view: GitLab project ID, token (masked `************{last4}`), warehouse HTTP path, default row limit; "Refresh Schema Now" button; cache status ("idle" / "refreshing" / "error"), last refresh UTC, model count
    - _Requirements: 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 11.4_
  - [x] 18.4 Wire React Router and `App.tsx`; configure Vite proxy to forward `/api/*` to `http://localhost:8000` during development
    - _Requirements: 14.1_

- [x] 19. Checkpoint — Ensure all frontend tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 20. Integration wiring and smoke tests
  - [x] 20.1 Write `tests/integration/test_full_query_flow.py`
    - End-to-end test with mocked warehouse and real Claude (or mocked Claude); verify question → SQL → rows flow
    - Constructs `SchemaIndex` with required `built_at` and `model_count` fields
    - _Requirements: 6.1, 6.3, 13.1_
  - [x] 20.2 Write `tests/smoke/test_startup.py`
    - Verify: embedder loads in <60s, produces 384-dim vectors, `create_app()` starts without error, `/api/status` returns valid shape, `AppConfig.from_env()` raises cleanly when secrets absent
    - _Requirements: 1.1, 4.7, 14.1, 14.2_
  - [x] 20.3 Write `tests/smoke/test_performance.py`
    - Assert: index build <30s for 500 models, retrieval <2s for 500 models, single embed <100ms, 10 repeated retrievals <2s total
    - _Requirements: 13.2, 13.3, 13.4_

- [x] 21. Final checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 22. Docker containerization and distribution
  - Create multi-stage `Dockerfile`: `node:24-slim` stage builds React frontend via `npm run build`; `python:3.11-slim` stage installs Python deps, copies compiled frontend, runs under non-root user `prism`
  - Create `docker-compose.yml` with `env_file: .env`, port `8000:8000`, healthcheck polling `/api/status` with 60s start_period; no credentials in the compose file itself
  - Create `.github/workflows/docker-publish.yml`: authenticate to GHCR with auto-provided `secrets.GITHUB_TOKEN`; publish `:latest` on push to `main`, publish `:vX.Y.Z` on GitHub Release; no additional secrets required
  - _Requirements: 16.1, 16.2, 16.3_

- [x] 23. Local development support and config hardening
  - Create `.gitignore` covering: `.env`, `.env.*` (preserving `.env.example`), `__pycache__/`, `.venv/`, `frontend/node_modules/`, `frontend/dist/`, `.databricks/`, `.docker/`, IDE and OS artifacts
  - Add `python-dotenv==1.0.1` to `requirements.txt`
  - Add `from dotenv import load_dotenv` and `load_dotenv()` as first call in `AppConfig.from_env()` — no-op when `.env` is absent (Databricks Apps production)
  - Add `databricks_server_hostname: str = ""` optional field to `AppConfig`; populate from `DATABRICKS_SERVER_HOSTNAME` env var; passed to `databricks-sql-connector` when non-empty, allowing explicit host override in Docker/local environments
  - _Requirements: 16.4, 16.5_

- [x] 24. OpenRouter fallback LLM provider
  - Add `openrouter_api_key: str = ""` to `AppConfig`; make `anthropic_api_key` optional (default `""`); validate at startup that at least one key is set
  - Add `openai>=1.0.0` to `requirements.txt` (OpenRouter uses the OpenAI SDK)
  - Refactor `SQLGenerator.generate()` into `_call_anthropic()` and `_call_openrouter()` private methods; select provider based on which key is configured — Anthropic takes priority
  - Update `mask_secret()` to mask `OPENROUTER_API_KEY` identically to `ANTHROPIC_API_KEY`
  - Update `.env.example` with `OPENROUTER_API_KEY` as a commented-out alternative
  - _Requirements: 11.1, 11.3_

- [x] 25. Local development PAT authentication and Python 3.11 requirement
  - Modify `databricks_runner.py` to check `DATABRICKS_TOKEN` env var; if set use it as PAT via `credentials_provider`; otherwise fall back to workspace OAuth (production behavior unchanged)
  - Add `DATABRICKS_HOST` and `DATABRICKS_TOKEN` to `.env.example` under a "local dev only" comment
  - Document Python 3.11+ and Node.js 24+ as prerequisites in `README.md`; add venv creation steps
  - Add `frontend/package-lock.json` to repo for reproducible Docker builds (`npm ci`)
  - _Requirements: 6.2, 16.4_

- [ ] 27. Email-based admin authentication (future)
  - Replace bcrypt `ADMIN_PASSWORD_HASH` gate in `POST /api/auth` with `X-Forwarded-Email` header validation
  - Add `ADMIN_ALLOWED_EMAILS` env var (comma-separated) to `AppConfig`; set in Databricks App UI
  - Update Settings page: remove password input; read authenticated email from `X-Forwarded-Email` header set by Databricks Apps SSO; show email in UI
  - Remove `admin_password_hash` from `AppConfig`, `app.yaml`, and `requirements.txt` (`bcrypt`)
  - _Requirements: [future Requirement 17]_

---

## Notes

- Tasks marked with `*` are optional property-based tests that can be skipped for a faster MVP
- Each task references specific requirements for traceability
- Property-based tests use Hypothesis (`tests/unit/`) with 100 examples minimum per property
- Frontend tests use Vitest + React Testing Library
- The `mask_secret()` helper must be applied at every log call site that touches a secret value — wire it in task 12.1 before any other component logs secrets
- Integration and smoke tests require live or mocked external services and are safe to skip in local dev
- Tasks 22–23 are independent of the original 21 tasks and can be deployed separately
- Task 24 depends on Databricks Apps SSO being enabled in the workspace

---

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1", "2.1", "2.2"] },
    { "id": 1, "tasks": ["2.3", "3.1", "4.1", "4.2", "4.3"] },
    { "id": 2, "tasks": ["3.2", "4.4"] },
    { "id": 3, "tasks": ["4.5", "4.6", "4.7", "4.8", "5.1"] },
    { "id": 4, "tasks": ["5.2", "6", "7.1", "7.2"] },
    { "id": 5, "tasks": ["7.3", "7.4", "7.5", "8.1"] },
    { "id": 6, "tasks": ["8.2", "8.3", "8.4", "9.1"] },
    { "id": 7, "tasks": ["9.2", "9.3", "10.1"] },
    { "id": 8, "tasks": ["10.2", "10.3", "11", "12.1"] },
    { "id": 9, "tasks": ["12.2", "12.3", "12.4", "13.1"] },
    { "id": 10, "tasks": ["13.2", "13.3"] },
    { "id": 11, "tasks": ["14", "15.1", "15.2", "15.3"] },
    { "id": 12, "tasks": ["15.4", "15.5", "16.1", "16.2", "16.3"] },
    { "id": 13, "tasks": ["16.4", "17.1"] },
    { "id": 14, "tasks": ["17.2", "17.3", "18.1", "18.2", "18.3"] },
    { "id": 15, "tasks": ["18.4", "19"] },
    { "id": 16, "tasks": ["20.1", "20.2"] },
    { "id": 17, "tasks": ["20.3", "21"] },
    { "id": 18, "tasks": ["22", "23"] },
    { "id": 19, "tasks": ["24"] }
  ]
}
```
