# Prism — Private Setup & Workflow Guide

This file lives in `private/` and is never synced to the public repo.

---

## Two-Repo Structure

| Repo | Visibility | Contains |
|---|---|---|
| `prism-private` | Private | Everything — code, `.kiro/`, `.claude/`, `private/` |
| `prism` | Public | Code only — no `.kiro/`, no `.claude/`, no `private/`, no `sync-public.yml` |

**Rule:** Never edit the public repo directly. All changes go through the private repo.

### Sync flow (private → public)

```
Private main
    ↓  strips .kiro/ .claude/ private/ sync-public.yml
Public dev   (force push)
    ↓  merge --no-ff
Public main
```

---

## One-Time Setup

### 1. Initialize git and push to private repo

```bash
cd "C:\Users\ZTI\Desktop\Projects\prism"

git init
git add .
git commit -m "initial commit"

git remote add origin https://github.com/YOUR_USERNAME/prism-private.git
git push -u origin main

# Create dev branch and push it
git checkout -b dev
git push -u origin dev
```

### 2. Create the public repo on GitHub

Go to github.com → New repository → name it `prism` → Public → **do not initialize with README**.

### 3. Generate a GitHub Personal Access Token (PAT)

GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens:
- Repository access: only `prism` (public repo)
- Permissions:
  - **Contents → Read and write**
  - **Workflows → Read and write** ← required so the sync can push workflow files

Copy the token — you only see it once.

### 4. Add secrets to the private repo

Go to `prism-private` on GitHub → Settings → Secrets and variables → Actions → New repository secret:

| Secret name | Value |
|---|---|
| `PUBLIC_REPO_TOKEN` | The PAT you just generated |
| `PUBLIC_REPO_NAME` | `YOUR_USERNAME/prism` |

### 5. Set up the Databricks secret scope

Run these once per Databricks workspace:

```bash
# Create the scope (name must be exactly prism-secrets)
databricks secrets create-scope prism-secrets

# GitLab token
databricks secrets put-secret prism-secrets gitlab-token --string-value "glpat-..."

# Anthropic API key
databricks secrets put-secret prism-secrets anthropic-api-key --string-value "sk-ant-..."

# Admin password hash — generate it first (see Password Management below)
databricks secrets put-secret prism-secrets admin-password-hash --string-value "$2b$12$..."
```

---

## Password Management

The Settings page password is stored as a bcrypt hash in the secret scope — never as plain text.

### Generate a hash

```bash
python -c "import bcrypt; print(bcrypt.hashpw(b'YOUR_PASSWORD', bcrypt.gensalt()).decode())"
```

### Update the password

```bash
databricks secrets put-secret prism-secrets admin-password-hash --string-value "$2b$12$newHashHere"
```

No redeployment needed — the app reads the secret fresh on each auth attempt.

---

## Daily Workflow

### Normal work (always on dev branch)

```bash
git checkout dev
# make your changes
git add .
git commit -m "your message"
git push origin dev
```

### Merge dev into main when ready to release

```bash
git checkout main
git merge dev
git push origin main
git checkout dev   # switch back to dev to continue working
```

### Sync to public (when you're ready)

The workflow pushes private `main` → public `dev`, then merges public `dev` → public `main` automatically.

**From terminal:**
```bash
gh workflow run sync-public.yml
```

**From GitHub UI:**
`prism-private` → Actions → Sync to Public Repo → Run workflow → Run workflow

### Check sync status

```bash
gh run list --workflow=sync-public.yml
```

---

## Environment Variables Reference

### Secrets (Databricks secret scope `prism-secrets`)

| Secret key | Description | Required |
|---|---|---|
| `gitlab-token` | GitLab PAT with `read_api` scope | Yes |
| `anthropic-api-key` | Anthropic API key (`sk-ant-...`) | One of these two |
| `openrouter-api-key` | OpenRouter API key (`sk-or-...`) — fallback if no Anthropic key | One of these two |
| `admin-password-hash` | bcrypt hash of the Settings page password | Yes |

### Non-secret variables (set in Databricks App UI)

| Variable | Example | Required |
|---|---|---|
| `GITLAB_BASE_URL` | `https://gitlab.yourcompany.com/api/v4` | Yes |
| `GITLAB_PROJECT_ID` | `123` | Yes |
| `DATABRICKS_SQL_WAREHOUSE` | `abc123def456` or `/sql/1.0/warehouses/abc123` | Yes |
| `DATABRICKS_SERVER_HOSTNAME` | `adb-xxx.azuredatabricks.net` | No (auto-detected from `DATABRICKS_HOST`, which Databricks Apps sets automatically) |
| `DEFAULT_ROW_LIMIT` | `1000` | No |
| `REFRESH_INTERVAL_HOURS` | `6` | No |
| `RETRY_INTERVAL_MINUTES` | `5` | No |

> **LLM provider:** Set at least one of `ANTHROPIC_API_KEY` or `OPENROUTER_API_KEY` (via secret scope). Anthropic takes priority if both are set. OpenRouter uses `anthropic/claude-sonnet-4-6` by default.

### Local development only (`.env` file)

These variables are only needed for local dev — they are not used or needed in production.

| Variable | Example | Notes |
|---|---|---|
| `DATABRICKS_TOKEN` | `dapi...` | Databricks Personal Access Token for local dev auth. In production, workspace OAuth is used automatically. |
| `DATABRICKS_HOST` | `https://adb-xxx.azuredatabricks.net` | Alternative to `DATABRICKS_SERVER_HOSTNAME` — the runner strips `https://` automatically. Use whichever is more convenient in your `.env`. |

### Local `.env` file

```bash
cp .env.example .env
# Fill in all values — python-dotenv loads this automatically on startup
# Never commit .env — it is in .gitignore
```

---

## Local Development

### Prerequisites

- Python 3.11+
- Node.js 24+ — [nodejs.org](https://nodejs.org/)
- Docker (Option B only)

### Python (native)

```bash
# Create and activate virtual environment
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS/Linux

pip install -r requirements.txt

cp .env.example .env   # fill in real values

uvicorn backend.main:app --reload --port 8000

# Frontend in a separate terminal
cd frontend && npm install && npm run dev
# Vite proxies /api/* → http://localhost:8000
```

### Docker

```bash
docker compose up
# App at http://localhost:8000
```

---

## Databricks Deployment

### Full deploy

```bash
# Build frontend first
cd frontend && npm install && npm run build && cd ..

# Deploy
databricks apps deploy prism --source-code-path .
```

### Redeploy after a code change

Same command — `databricks apps deploy` is idempotent.

### View live logs

```bash
databricks apps logs prism --follow
```

---

## Running Tests

```bash
# All tests
pytest

# Unit / property-based tests only (no secrets needed)
pytest tests/unit/ --hypothesis-profile=prism

# Integration + smoke tests (need live secrets in .env)
pytest tests/integration/ tests/smoke/
```

---

## Accuracy & SQL Generation Improvements

Recent changes that improve query accuracy — useful context when debugging or extending the generation pipeline.

### Join hints from dbt relationship tests (Task 32)

Prism now extracts all `relationships` test nodes from `manifest.json` at startup and stores them as `SchemaIndex.join_hints`. On every query the `PromptBuilder` injects a `## Join Keys` section listing exact FK→PK column pairs so Claude never guesses join columns.

- **59 hints** extracted from the current manifest (logged at startup as `ManifestParser: extracted 59 join hint(s) from relationship tests`)
- Fallback: when no relationship tests exist for the selected models, shared `_id`/`_key` columns that appear in exactly two selected models are shown as potential join candidates
- Code: `backend/discovery/manifest_parser.py` → `parse_join_hints()`, `backend/generation/prompt_builder.py` → `_join_hints_block()`

### max_tokens increased to 4096 (Task 33)

`_MAX_TOKENS` in `backend/generation/sql_generator.py` was raised from 2000 → 4096 to prevent silent JSON truncation on complex multi-join queries.

### Targeted 2-attempt SQL retry (Task 34)

When Databricks returns a SQL error, `QueryRunner._auto_retry()` now does up to two retries instead of one:

| Attempt | Prompt contents |
|---|---|
| Retry 1 | Original question + error classified into type (`column_not_found`, `table_not_found`, `type_mismatch`, `ambiguous_column`, `syntax_error`, `division_by_zero`) + targeted fix instruction |
| Retry 2 | Both error messages + both failed SQL strings + second error's fix hint |

The DDL/DML guard runs on every retry SQL before it reaches the warehouse — no write operations are possible under any circumstances.

Code: `backend/execution/databricks_runner.py` → `_classify_error()`, `_RETRY_INSTRUCTIONS`, `_auto_retry()`, `_generate_and_validate()`

### Automatic chart visualization (Task 35)

Query results now include an automatically-selected chart when the data shape supports it. No extra LLM call is made — a heuristic `chart_advisor` inspects column names and value types immediately after SQL execution.

**Chart type rules (priority order):**

| Condition | Chart |
|---|---|
| Date column + numeric columns + no categorical column | Line |
| Categorical + 1 numeric + distribution keyword in question + ≤ 15 rows | Pie |
| Categorical + numeric columns | Bar |
| Exactly 2 numeric, no categorical, no date | Scatter |
| Anything else | None (table only) |

The `not cat_cols` guard on the line rule is critical — without it, a query like "impressions by brand per month" (date × brand × impressions) would plot all rows as one flat jagged line instead of returning `none` and falling back to the table.

**Frontend behaviour:**
- Chart/Table toggle appears above results when a chart is detected; defaults to Chart tab.
- Bar and line charts scroll horizontally beyond 10 data points (min 60 px/point).
- X-axis labels rotate −40° beyond 6 points to prevent overlap.
- ISO date ticks formatted as "Jan 2026" (monthly) or "Jun 15" (daily).
- Table display capped at 100 rows; CSV export delivers all returned rows.
- Default `row_limit` raised from 1,000 → 10,000.

**Code:**
- `backend/generation/chart_advisor.py` — `suggest_chart(rows, question) -> ChartSuggestion`
- `backend/api/models.py` — `ChartSuggestion` model; `chart` field on `QueryResponse`; `row_limit` default = 10000
- `backend/api/routes.py` — calls `suggest_chart()` after execution, passes result to `QueryResponse`
- `frontend/src/components/ChartView.tsx` — Recharts renderer (bar/line/area/pie/scatter)
- `frontend/src/components/AssistantMessage.tsx` — Chart/Table toggle
- `frontend/src/components/ResultsTable.tsx` — 100-row display cap, full CSV export

---

## If an External PR Comes In on the Public Repo

Someone opened a PR on `prism` (public) and you merged it. Bring it into private:

```bash
# Add public as a remote (one-time)
git remote add public https://github.com/YOUR_USERNAME/prism.git

# Fetch and cherry-pick the merged commit into dev
git checkout dev
git fetch public
git cherry-pick <commit-hash>

# Update specs in .kiro/ if the change warrants it
git push origin dev

# Merge dev into main when ready
git checkout main
git merge dev
git push origin main

# Sync back to public
gh workflow run sync-public.yml
```

---

## What the Sync Workflow Does

**Step 1 — Strip private files and push to public `dev`:**
```
.kiro/                              ← specs
.claude/                            ← Claude Code settings
private/                            ← this folder
.github/workflows/sync-public.yml  ← the sync workflow itself
```
Everything else (code, tests, Dockerfile, docker-compose.yml, docker-publish.yml, README.md) is pushed to public `dev`.

**Step 2 — Merge public `dev` into public `main`** (done automatically in the same workflow run).

---

## Docker Image (GHCR)

The `docker-publish.yml` workflow lives in the public repo and publishes to:

```
ghcr.io/YOUR_USERNAME/prism:latest     ← on every push to main (via sync)
ghcr.io/YOUR_USERNAME/prism:v1.0.0    ← on every GitHub Release on the public repo
```

To cut a release: create a GitHub Release on the **public** repo with a tag like `v1.0.0`.
