# Prism — Natural Language Analytics Assistant

Prism lets business users ask plain-English questions about their data and receive real SQL-backed answers — no SQL knowledge required. It auto-discovers dbt schemas from GitLab CI artifacts, uses Claude to generate SQL, and executes queries against a Databricks SQL warehouse.

### How it achieves accuracy

Prism uses several techniques to ensure correct SQL — especially for multi-table questions from non-technical users:

- **Explicit join keys from dbt relationship tests** — Prism parses all `relationships` test nodes in `manifest.json` and extracts FK→PK column pairs. On every query, these are injected into the Claude prompt as a `## Join Keys` section so Claude never guesses which columns to join on. A fallback detects shared `_id`/`_key` columns across exactly two selected models when no relationship tests are defined.
- **BFS lineage expansion** — After semantic retrieval, Prism walks `depends_on` ancestors across all levels (capped at 40 models) to ensure dimension and lookup tables are always present as join candidates.
- **4096-token output budget** — The LLM response cap is set to 4096 tokens to prevent silent JSON truncation on complex multi-join queries with verbose explanations.
- **Targeted 2-attempt SQL retry** — When Databricks returns an error, Prism classifies it (column not found, type mismatch, ambiguous column, syntax error, etc.) and sends a targeted fix instruction to Claude for Retry 1. If Retry 1 also fails, Retry 2 includes both error messages and both failed SQL strings so Claude has the full failure context. Raw error stack traces are never shown to users.
- **Full compiled SQL in context** — The full compiled SQL for each model is stored and shown to Claude (first 1500 chars in the prompt), giving it accurate grain, filter, and join pattern hints.
- **Automatic chart visualization** — After execution, a heuristic `chart_advisor` (no extra LLM call) inspects column names and value types to pick the best chart type: line for time-series, bar for categorical breakdowns, pie for small distributions, scatter for two-numeric correlations. Results are shown in a Chart/Table toggle view; bar and line charts scroll horizontally when there are many data points. The table view caps display at 100 rows while CSV export delivers all returned rows (up to 10,000).

---

## Deploy to Databricks Apps

### 1. Create the secret scope and add secrets

```bash
# Create the scope (once per workspace)
databricks secrets create-scope prism-secrets

# GitLab token
databricks secrets put-secret prism-secrets gitlab-token --string-value "glpat-..."

# LLM provider — add at least one (Anthropic takes priority if both are set)
databricks secrets put-secret prism-secrets anthropic-api-key  --string-value "sk-ant-..."
# databricks secrets put-secret prism-secrets openrouter-api-key --string-value "sk-or-..."

# Admin password hash
databricks secrets put-secret prism-secrets admin-password-hash \
  --string-value "$(python -c "import bcrypt; print(bcrypt.hashpw(b'yourpassword', bcrypt.gensalt()).decode())")"
```

The secret scope must be named exactly **`prism-secrets`**.

### 2. Set environment variables in the Databricks App UI

| Variable | Example |
|---|---|
| `GITLAB_BASE_URL` | `https://gitlab.example.com/api/v4` |
| `GITLAB_PROJECT_ID` | `123` |
| `DATABRICKS_SQL_WAREHOUSE` | `your-warehouse-id` |
| `DATABRICKS_SERVER_HOSTNAME` | *(optional — leave blank to auto-detect)* |

> **App Identity (optional):** After deployment, navigate to **`/admin`** in the app and fill in the App Identity section (owner name, title, email, team, company). These are stored in `prism_settings.json` and shown as a footer line in the chat UI. This step is entirely optional — the app opens directly to the chat page on first load and works without it. No environment variables or redeployment needed.

The following environment variables are optional fallbacks for App Identity. The Settings UI takes priority over these if both are set:

| Variable | Description |
|---|---|
| `PRISM_OWNER_NAME` | Owner display name |
| `PRISM_OWNER_TITLE` | Owner job title |
| `PRISM_OWNER_EMAIL` | Owner email address |
| `PRISM_TEAM_NAME` | Team name |
| `PRISM_COMPANY_NAME` | Company name |

### 3. Build the frontend

```bash
cd frontend && npm install && npm run build
```

### 4. Deploy

```bash
databricks apps deploy prism --source-code-path .
```

The app URL is shown in the Databricks Apps UI once deployment completes.

> **Other companies:** You can point the Databricks App UI directly at this repository's GitHub URL — no forking or copying the code is needed. Just complete steps 1–2 above in your own workspace, then deploy from the URL. The landing page displays the Prism gradient brand logo (triangle SVG with gradient fill) and the product name "Prism".

---

## Local Development

### Prerequisites

| Tool | Version | Download |
|---|---|---|
| Python | 3.11+ | [python.org](https://www.python.org/downloads/) |
| Node.js | 24+ | [nodejs.org](https://nodejs.org/) |
| Docker | any | [docker.com](https://www.docker.com/products/docker-desktop/) — Option B only |

### Option A — Python

```bash
# Create and activate a virtual environment
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS/Linux

# Install Python dependencies
pip install -r requirements.txt

# Copy the env template and fill in real values
cp .env.example .env

# Start the backend (auto-loads .env)
uvicorn backend.main:app --reload --port 8000

# In a separate terminal — start the frontend dev server
cd frontend && npm install && npm run dev
# Vite proxies /api/* to http://localhost:8000
```

**Local dev authentication:** Set `DATABRICKS_TOKEN` in `.env` to a Databricks Personal Access Token (starts with `dapi...`). Production deployments use workspace OAuth automatically and do not need this variable.

### Option B — Docker

```bash
cp .env.example .env   # fill in real values

docker compose up
# App runs at http://localhost:8000
```

---

## Running Tests

```bash
# All tests
pytest

# Unit / property-based tests only
pytest tests/unit/ --hypothesis-profile=prism

# Integration and smoke tests (require live secrets in .env)
pytest tests/integration/ tests/smoke/
```

---

## Project Structure

```
prism/
├── app.yaml                     # Databricks Apps entry point + secret refs
├── Dockerfile                   # Multi-stage build (Node → Python)
├── docker-compose.yml           # Local dev via Docker
├── requirements.txt             # Pinned Python dependencies
├── pyproject.toml               # Pytest + Hypothesis configuration
├── .env.example                 # Environment variable template
├── .github/workflows/
│   └── docker-publish.yml       # Publishes Docker image to GHCR on push/release
├── backend/
│   ├── config.py                # AppConfig + secret masking helpers
│   ├── settings_store.py        # Persistent app identity store (reads/writes prism_settings.json)
│   ├── main.py                  # FastAPI app, startup lifecycle, middleware
│   ├── api/
│   │   ├── models.py            # Pydantic request/response models
│   │   └── routes.py            # HTTP endpoints
│   ├── discovery/
│   │   ├── gitlab_fetcher.py    # Downloads dbt artifacts from GitLab CI
│   │   ├── manifest_parser.py   # Parses manifest.json
│   │   ├── catalog_parser.py    # Merges catalog.json (column types, row counts)
│   │   ├── graph_parser.py      # Builds lineage adjacency list
│   │   ├── index_builder.py     # Orchestrates parsing → embeddings
│   │   └── cache_manager.py     # In-memory cache + 6-hour background refresh
│   ├── search/
│   │   ├── embedder.py          # sentence-transformers all-MiniLM-L6-v2
│   │   └── retriever.py         # Cosine similarity + Gold/Silver score boosts
│   ├── generation/
│   │   ├── prompt_builder.py    # Assembles Claude system prompt
│   │   └── sql_generator.py     # Calls Claude API, validates JSON response
│   └── execution/
│       └── databricks_runner.py # Executes SQL, streams rows, auto-retry
├── frontend/
│   └── src/                     # React + TypeScript source
└── tests/
    ├── unit/                    # Hypothesis property-based tests (24 properties)
    ├── integration/             # End-to-end flow tests (mocked warehouse)
    └── smoke/                   # Startup time and performance benchmarks
```
