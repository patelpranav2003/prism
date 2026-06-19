# Requirements Document

## Introduction

Prism is a natural language analytics assistant deployed as a Databricks App. It allows business users and analysts to ask plain-English questions about their data and receive real SQL-backed answers — with no SQL knowledge, no manual schema setup, and no login required beyond workspace identity. Prism auto-discovers the full dbt schema from GitLab CI artifacts (manifest.json, catalog.json, graph_summary.json) on startup, builds an in-memory semantic index, uses Claude to generate SQL, executes the SQL against a Databricks SQL warehouse, and returns results as an interactive table.

---

## Glossary

- **Prism**: The natural language analytics assistant application described in this document.
- **Schema_Index**: The in-memory data structure built from parsed GitLab CI artifacts that maps model names to their full metadata (columns, types, grain, lineage, layer, compiled SQL fingerprint).
- **Artifact_Fetcher**: The component responsible for downloading manifest.json, catalog.json, and graph_summary.json from GitLab CI.
- **Index_Builder**: The component that parses the three artifact files and constructs the Schema_Index, including embeddings.
- **Embedder**: The component that uses sentence-transformers (all-MiniLM-L6-v2) to produce vector representations of model metadata and user questions.
- **Retriever**: The component that computes cosine similarity between a question embedding and all model embeddings, returning the top-N ranked models.
- **Prompt_Builder**: The component that assembles the Claude API system prompt from retrieved model schemas, lineage context, and dialect rules.
- **SQL_Generator**: The component that calls the Claude API and parses its structured JSON response.
- **Query_Runner**: The component that executes SQL against a Databricks SQL warehouse using the workspace OAuth token.
- **Cache_Manager**: The component that holds the last successfully fetched artifact data, refreshes it on a 6-hour schedule, and serves stale data when a refresh fails.
- **Schema_Explorer**: The UI sidebar component that allows users to browse and search dbt models by name, column, and layer.
- **Layer**: The dbt medallion tier of a model — one of: Bronze, Silver, or Gold — inferred from tags or folder path.
- **Grain**: The unique key or granularity of a model's rows, taken from `meta.grain` if present, otherwise inferred from model name and compiled SQL patterns.
- **Confidence**: A three-value signal (high / medium / low) returned by the SQL_Generator indicating how certain Claude is about which models answer the question.
- **Workspace_OAuth_Token**: The OAuth token provided automatically by the Databricks Apps runtime, used for all SQL execution — never supplied manually by users.
- **GitLab_Token**: A GitLab personal access token with `read_api` scope, stored in a Databricks secret scope, used by the Artifact_Fetcher.
- **dbt_Model**: A single compiled SQL model defined in the dbt project, represented in manifest.json and catalog.json.
- **Admin_User**: A user who knows the admin password (stored as a bcrypt hash in the Databricks secret scope under the key `admin-password-hash`) and can access the Settings page.
- **python-dotenv**: Python package that loads environment variables from a `.env` file in the project root when running locally. In production (Databricks Apps), the `.env` file is absent and `load_dotenv()` is a no-op — all variables are already injected by the runtime from the secret scope.
- **Docker_Image**: A self-contained executable image built from the multi-stage `Dockerfile` that includes both the compiled React frontend and the Python backend. Used for local testing and evaluation without a Databricks workspace; published to GitHub Container Registry (GHCR) via GitHub Actions on every push to `main`.

---

## Requirements

### Requirement 1: Schema Artifact Discovery

**User Story:** As a data platform engineer, I want Prism to automatically fetch dbt schema artifacts from GitLab CI on startup, so that users never need to manually provide schema information.

#### Acceptance Criteria

1. WHEN the Prism application starts, THE Artifact_Fetcher SHALL fetch manifest.json, catalog.json, and graph_summary.json in parallel from the latest successful `pages` job on the `main` branch of the configured GitLab project via the GitLab Artifacts API.
2. THE Artifact_Fetcher SHALL construct artifact download URLs using the pattern: `GET /projects/:id/jobs/artifacts/main/raw/public/{filename}?job=pages` where `:id` is the value of the `GITLAB_PROJECT_ID` environment variable.
3. THE Artifact_Fetcher SHALL authenticate all GitLab API requests using the `GITLAB_TOKEN` secret retrieved from the Databricks secret scope, passed as the `PRIVATE-TOKEN` HTTP header.
4. IF the `GITLAB_TOKEN` secret is absent from the Databricks secret scope or the GitLab API returns HTTP 401 or 403, THEN THE Artifact_Fetcher SHALL log an error to the Databricks App logs including the secret scope name, the error type (absent or unauthorized), and set the cache status to "unavailable" — the token value SHALL NOT appear in any log output.
5. THE Artifact_Fetcher SHALL use the `httpx` library with async HTTP to fetch all three files concurrently within a single startup event, with a per-request timeout of 30 seconds.
6. IF any individual artifact fetch returns a non-200 HTTP status, THEN THE Artifact_Fetcher SHALL log the file name, HTTP status code, and response body truncated to 500 characters, and mark that specific file as failed; IF all three fetches fail, THE Cache_Manager SHALL set cache status to "unavailable".
7. IF all three artifact fetches succeed, THEN THE Cache_Manager SHALL store the complete content of all three files in memory together with a UTC timestamp, replacing any previously cached content, and set the cache status to "fresh".
8. WHEN a refresh cycle completes successfully, THE Cache_Manager SHALL atomically replace the previous cached content with the new content and update the last-refresh timestamp to the current UTC time.
9. IF one or two artifact fetches fail while at least one succeeds, THEN THE Artifact_Fetcher SHALL mark the failed files individually, log each failure, retain the last good cache for the failed files, and set cache status to "stale".

---

### Requirement 2: Cache Management and Background Refresh

**User Story:** As a business user, I want the app to always respond instantly without waiting for data refreshes, so that I am never blocked by background operations.

#### Acceptance Criteria

1. THE Cache_Manager SHALL retain the most recently successfully fetched artifact data in memory for the lifetime of the process; IF no successful fetch has yet completed, THE Cache_Manager SHALL set the cache status to "unavailable" and serve no cached data.
2. THE Cache_Manager SHALL schedule a background refresh of all three artifacts every 6 hours after the last successful fetch completes.
3. WHILE a background refresh is in progress, THE Cache_Manager SHALL continue serving all read requests from the previous good cache without interruption or added latency.
4. IF a background refresh fails for any artifact, THEN THE Cache_Manager SHALL preserve the last good cache, log the failure with a timestamp and artifact name, set the cache status to "stale", and schedule a retry after 5 minutes; IF the retry also fails, THE Cache_Manager SHALL continue scheduling retries every 5 minutes until a refresh succeeds, without limit.
5. WHEN the Schema_Index is being rebuilt after a successful artifact refresh, THE Cache_Manager SHALL continue serving queries from the previous Schema_Index until the new index passes validation (all models parsed with no fatal errors) and is atomically swapped in.
6. WHERE an Admin_User is authenticated and triggers a manual refresh via the Settings page, WHEN the "Refresh Schema Now" button is clicked, THE Cache_Manager SHALL initiate an immediate out-of-cycle refresh and the Settings UI SHALL display a success message including the new model count or a failure message including the error reason within 30 seconds.
7. THE Cache_Manager SHALL expose the following state to the UI at all times: last successful refresh timestamp in UTC, total model count from the current Schema_Index, and current cache status as one of: "fresh", "stale", or "unavailable".
8. IF the cache status is "stale" or "unavailable", THEN THE Prism UI SHALL display a non-blocking warning banner stating that schema data may not reflect the latest pipeline run, without disabling any user interactions.

---

### Requirement 3: Schema Index Construction

**User Story:** As a data analyst, I want Prism to understand the full structure of all dbt models including column types, grain, and lineage, so that it can answer questions accurately.

#### Acceptance Criteria

1. WHEN all three artifact files have been successfully fetched and stored in the Cache_Manager, THE Index_Builder SHALL construct a unified Schema_Index in memory and complete construction within 30 seconds for up to 500 dbt models.
2. THE Index_Builder SHALL extract the following fields from manifest.json for each dbt_Model: model name, database, schema, full qualified name (catalog.schema.table), all column names with descriptions, grain (from `meta.grain` if present), compiled SQL (first 500 characters), `depends_on.nodes` for direct parent models, tags, and folder path.
3. THE Index_Builder SHALL infer the Layer of each dbt_Model using the following priority order: (1) tags containing "gold", "silver", or "bronze" (case-insensitive); (2) folder path segment containing "gold", "silver", or "bronze" (case-insensitive); (3) default to "bronze" if neither is present.
4. THE Index_Builder SHALL merge catalog.json data into each model entry by overriding declared column types with actual column types from the last dbt run and recording the row count from catalog statistics.
5. THE Index_Builder SHALL build a lineage adjacency list from graph_summary.json, mapping each model to its direct parent and child models, for use during prompt construction.
6. IF grain is absent from `meta.grain`, THEN THE Index_Builder SHALL infer grain by scanning the first 500 characters of compiled SQL for the following specific patterns in order: (1) columns listed in a GROUP BY clause; (2) presence of a DISTINCT keyword; (3) model name suffixes matching `_by_{dimension}` (e.g., `_by_day`, `_by_brand`); IF none of these patterns match, grain SHALL be recorded as "unknown".
7. IF the Index_Builder encounters a field in a model entry that is absent or has an unexpected JSON type (e.g., null where a string is expected), THEN THE Index_Builder SHALL record that field as its zero value (empty string, empty list, or 0), log a warning including the model name and field name, and continue building the index for that model and all remaining models.
8. IF the Index_Builder fails to parse any of the three artifact files entirely due to a JSON parse error or unexpected top-level structure, THEN THE Index_Builder SHALL log the file name and the parse error message, halt index construction for that refresh cycle, and preserve the previous valid Schema_Index unchanged; IF no previous Schema_Index exists, THE system SHALL set cache status to "unavailable".

---

### Requirement 4: Semantic Model Embeddings

**User Story:** As a business user, I want Prism to understand the meaning of my question and match it to the right data models, so that I get relevant results even when I don't use exact column names.

#### Acceptance Criteria

1. WHEN the Schema_Index is built or rebuilt, THE Embedder SHALL generate a text representation for each dbt_Model in the format: `"{model_name}: {description}. Columns: {column_names_and_descriptions}"` and produce a vector embedding for each using the sentence-transformers `all-MiniLM-L6-v2` model.
2. THE Embedder SHALL store all model embeddings as a single numpy array in memory, with each row index corresponding to the same-index model in the Schema_Index.
3. WHEN a user submits a question, THE Embedder SHALL produce a vector embedding of the question text using the same `all-MiniLM-L6-v2` model instance loaded at startup.
4. WHEN the Retriever receives a question embedding, THE Retriever SHALL compute the cosine similarity between the question embedding and every row in the model embeddings array, then return the top-N models ordered by descending adjusted score, where N is the minimum of 5 and the total number of models in the index.
5. WHEN computing the adjusted score for each model, THE Retriever SHALL add 0.05 to the raw cosine similarity score of Gold layer models and 0.025 to the raw cosine similarity score of Silver layer models before ranking, such that a Gold model with any positive raw similarity will rank above a Bronze model with the same raw similarity score.
6. WHEN the Retriever returns ranked models, THE Retriever SHALL complete similarity computation and ranking within 2 seconds for an index of up to 500 models on the application host hardware.
7. THE Embedder SHALL load the `all-MiniLM-L6-v2` model once during application startup and reuse the same in-memory model instance for all subsequent embedding calls without reloading the model from disk.
8. IF all models in the index have a raw cosine similarity score below 0.1 for a given question, THEN THE Retriever SHALL still return the top-N models by score and SHALL set the confidence hint to "low" to signal the SQL_Generator that no strong match was found.

---

### Requirement 5: SQL Generation via Claude

**User Story:** As a business user, I want to ask a question in plain English and receive correct, executable SQL, so that I can get data answers without writing code.

#### Acceptance Criteria

1. WHEN the top candidate models are retrieved by the Retriever, THE Prompt_Builder SHALL construct a Claude API system prompt that includes: full column list with types and descriptions, grain, layer, and first 500 characters of compiled SQL for each retrieved model; lineage relationships between those models from the Schema_Index adjacency list; and Databricks SQL dialect rules.
2. THE Prompt_Builder SHALL include the following Databricks SQL dialect rules in every system prompt: always use fully qualified names in the format `catalog.schema.table`; use backticks for column names containing special characters; use `DATE_TRUNC`, `DATEADD`, and `DATEDIFF` for date operations; use `QUALIFY` for window function filtering; apply a default `LIMIT 1000` unless the user explicitly requests more rows; never use `SELECT *`.
3. WHEN a model's grain field is "unknown" or the compiled SQL contains no GROUP BY or DISTINCT and the model name does not contain a `_by_` suffix, THE Prompt_Builder SHALL include an explicit instruction in the system prompt directing Claude to add deduplication logic (e.g., DISTINCT or a ROW_NUMBER() window function) in the generated SQL for that model.
4. THE SQL_Generator SHALL call the Claude API using model `claude-sonnet-4-6` with a maximum of 2000 output tokens and SHALL request a response in the following strict JSON format: `{"sql": "...", "explanation": "...", "models_used": [...], "confidence": "high|medium|low", "confidence_reason": "..."}`.
5. THE SQL_Generator SHALL attempt to parse the Claude response as JSON and validate that all five required fields (`sql`, `explanation`, `models_used`, `confidence`, `confidence_reason`) are present and have the correct types; IF the response is not valid JSON or any required field is absent or has an incorrect type, THEN THE SQL_Generator SHALL treat the response as a generation failure and return the user-facing error message defined in criterion 6.
6. IF the Claude API returns a non-200 HTTP response, a network timeout (after 30 seconds), or a generation failure as defined in criterion 5, THEN THE SQL_Generator SHALL log the error with question text and outcome, and return the user-facing message: "Unable to generate SQL — please try rephrasing your question."
7. IF the confidence field in the Claude response is "low", THEN THE Prism UI SHALL display a non-dismissable inline warning banner above the SQL output reading: "I'm not fully certain which tables to use — please review the SQL before trusting these results."

---

### Requirement 6: SQL Execution and Auto-Retry

**User Story:** As a business user, I want to see real query results directly in the app, so that I don't need to copy SQL into another tool.

#### Acceptance Criteria

1. WHEN valid SQL is returned by the SQL_Generator, THE Query_Runner SHALL execute the SQL against the Databricks SQL warehouse specified by the `DATABRICKS_SQL_WAREHOUSE` environment variable using the `databricks-sql-connector` Python package.
2. THE Query_Runner SHALL authenticate to the Databricks SQL warehouse exclusively using the Workspace_OAuth_Token provided by the Databricks Apps runtime — no user credential input is required or accepted.
3. THE Query_Runner SHALL begin streaming result rows to the UI before the full result set has been received, such that the first row is visible in the UI before query execution has completed on the warehouse.
4. THE Query_Runner SHALL enforce a default row limit of 1000 rows, which the user may increase to a maximum of 10000 rows via a slider in the UI before submitting a question.
5. THE Prism UI SHALL display the following query metadata after execution completes: total row count returned, query execution time in milliseconds, and the display name of the SQL warehouse used.
6. IF the SQL execution returns an error response from the Databricks SQL warehouse, THEN THE Query_Runner SHALL automatically retry once by passing the original question text, the failed SQL, and the full warehouse error message to the SQL_Generator and requesting a corrected query.
7. IF the retry SQL execution also fails, THEN THE Prism UI SHALL display: a non-technical summary message (e.g., "The query could not be completed"), the failed SQL statement in a copyable code block, and a prompt directing the user to contact a data engineer — raw Databricks error stack traces SHALL NOT be shown.
8. IF the SQL_Generator is unavailable or returns a generation failure during the retry attempt in criterion 6, THEN THE Query_Runner SHALL skip the retry, surface the original warehouse error and failed SQL as described in criterion 7, and log the SQL_Generator unavailability.

---

### Requirement 7: Results Display

**User Story:** As a business user, I want to see my answer as a clean table at the top of the page, so that I get what I need without sifting through technical details.

#### Acceptance Criteria

1. WHEN query results are returned, THE Prism UI SHALL display the result rows as a sortable table — with each column header clickable to sort ascending or descending — as the first element on the results page, above all other content.
2. THE Prism UI SHALL provide a "Download CSV" button on the results table that exports all returned rows as a UTF-8 encoded CSV file, with column headers in the first row, using the format `prism_results_{timestamp}.csv` for the filename.
3. THE Prism UI SHALL display a collapsible "How I answered this" section below the results table, collapsed by default, containing: the plain-English explanation from the Claude response; the models_used list as clickable tags that open the Schema_Explorer scrolled to and expanded at the selected model's detail view; the generated SQL with syntax highlighting; and a copy-to-clipboard button for the SQL.
4. THE Prism UI SHALL display a confidence indicator alongside the "How I answered this" section showing the label "High", "Medium", or "Low" corresponding to the confidence value from the Claude response, with a distinct color for each level (e.g., green / amber / red).
5. THE Prism UI SHALL display a "Refine your question" text input below the "How I answered this" section, pre-populated with the text of the previous question, allowing the user to edit and resubmit a follow-up question.

---

### Requirement 8: Landing Page and Example Questions

**User Story:** As a business user with no data background, I want a simple, welcoming landing page, so that I can start getting answers without any training.

#### Acceptance Criteria

1. THE Prism UI SHALL display a landing page with the ZURU logo and the product name "Prism" visible in the header, a white background, and a single centered text input with placeholder text "Ask anything about your data...".
2. THE Prism UI SHALL display between 4 and 6 statically configured example questions as clickable chips below the main input on the landing page; WHEN a chip is clicked, THE Prism UI SHALL populate the input with that question text and submit it automatically without requiring a separate action from the user.
3. THE Prism UI SHALL display a schema health indicator on the landing page that reflects the current Cache_Manager state: WHEN cache status is "fresh", show total model count and elapsed time since last refresh (e.g., "455 models loaded · Updated 2 hours ago"); WHEN cache status is "stale", show the model count with a warning label; WHEN cache status is "unavailable", show "Schema unavailable — contact your data team".
4. IF the Schema_Index is not yet ready when the landing page loads, THEN THE Prism UI SHALL display a visible text or animated loading indicator (e.g., "Loading schema...") and disable the question input and example chips; WHEN the Schema_Index becomes available, THE Prism UI SHALL automatically remove the loading indicator and enable the input and chips without requiring a page reload.

---

### Requirement 9: Schema Explorer

**User Story:** As a data analyst, I want to browse all dbt models by layer and search by name or column, so that I can understand what data is available before asking a question.

#### Acceptance Criteria

1. THE Prism UI SHALL provide a collapsible sidebar Schema_Explorer that is hidden by default on viewports with width below 768px and visible by default on viewports with width of 768px or greater; the user SHALL be able to toggle visibility on any viewport width.
2. THE Schema_Explorer SHALL display all models grouped under three collapsible sections labeled "Gold", "Silver", and "Bronze" in that display order; each section SHALL be expanded by default when the Schema_Explorer is first opened.
3. THE Schema_Explorer SHALL include a search input that filters the model list within 300 milliseconds of the user stopping typing, matching any model whose name or any column name contains the search string as a case-insensitive substring.
4. WHEN a user clicks a model name in the Schema_Explorer, THE Prism UI SHALL display a detail panel showing: model description, full list of columns with their types and descriptions, grain value, last-updated timestamp sourced from catalog.json run statistics, and row count from the last dbt run; the detail panel SHALL be hidden until a model is selected.
5. THE Schema_Explorer SHALL reflect the current state of the Schema_Index; WHEN the Schema_Index is refreshed, THE Schema_Explorer SHALL update its displayed model list within 5 seconds of the new index becoming active.

---

### Requirement 10: Settings Page

**User Story:** As an Admin_User, I want to manage configuration and trigger manual refreshes, so that I can maintain Prism without redeploying the app.

#### Acceptance Criteria

1. THE Prism UI SHALL provide a Settings page that renders only a password prompt until the Admin_User enters the correct admin password; no settings controls SHALL be rendered or accessible in the DOM until authentication succeeds.
2. IF the Admin_User submits an incorrect password, THEN THE Prism UI SHALL display an inline error message "Incorrect password" and keep the password prompt visible without navigating away.
3. WHERE the Admin_User is authenticated, THE Settings page SHALL display the following configurable fields pre-filled from environment variables: GitLab project ID, GitLab token displayed as the last 4 characters preceded by asterisks (or fully masked if the token length is 4 characters or fewer), SQL warehouse HTTP path, and default row limit; IF an environment variable is absent, the corresponding field SHALL display an empty placeholder.
4. WHEN the Admin_User clicks "Refresh Schema Now", THE Cache_Manager SHALL initiate an immediate refresh; IF the refresh succeeds, THE Settings page SHALL display "Schema refreshed — {model_count} models loaded" within 30 seconds; IF the refresh fails, THE Settings page SHALL display "Refresh failed: {error_reason}" within 30 seconds.
5. WHILE the Settings page is visible, THE Settings page SHALL display the current cache status as one of "idle", "refreshing", or "error"; the last successful refresh timestamp in UTC; and the total model count from the current Schema_Index.
6. IF the Admin_User submits a non-empty updated value for the GitLab project ID or GitLab token fields, THEN THE Artifact_Fetcher SHALL use the updated values for all subsequent refresh cycles — both scheduled and manually triggered — without requiring an application restart.

---

### Requirement 11: Security and Credential Handling

**User Story:** As a platform engineer, I want all secrets to be stored and accessed securely, so that no credentials are exposed in logs, UI, or source code.

#### Acceptance Criteria

1. THE Artifact_Fetcher SHALL retrieve the `GITLAB_TOKEN` exclusively from the Databricks secret scope at runtime — the token SHALL NOT appear in source code, configuration files, or environment variable default values. THE `ANTHROPIC_API_KEY` and `ADMIN_PASSWORD_HASH` SHALL likewise be retrieved exclusively from the Databricks secret scope; none of the three secrets SHALL appear in `app.yaml`, `docker-compose.yml`, any environment variable default value, or any log output.
2. THE Query_Runner SHALL use the Workspace_OAuth_Token provided by the Databricks Apps runtime for all SQL execution — no user-provided credentials or manually managed service account tokens SHALL be used for any query.
3. THE Prism application SHALL mask secret values in all log output: for `GITLAB_TOKEN` and `ANTHROPIC_API_KEY`, only the last 4 characters SHALL appear with all preceding characters replaced by asterisks; for `DATABRICKS_SQL_WAREHOUSE`, the value SHALL be fully masked and SHALL NOT appear in any log output in any form.
4. THE Settings page SHALL display the GitLab token field with all characters except the last 4 replaced by asterisks, using a fixed display of 12 asterisk characters followed by the last 4 characters to avoid revealing token length; the token value SHALL only be updated in the Artifact_Fetcher configuration IF the Admin_User submits a non-empty replacement value.
5. THE Prism application SHALL enforce read-only access: THE application SHALL NOT execute any DDL or DML statement — including CREATE, INSERT, UPDATE, DELETE, DROP, ALTER, TRUNCATE, MERGE, and REPLACE — against any Databricks catalog, schema, or table.
6. IF `GITLAB_TOKEN` is absent from the Databricks secret scope at startup, THEN THE Prism application SHALL log a startup error identifying the missing secret by scope name and key name (without value), set cache status to "unavailable", and continue serving the UI with the schema unavailable state rather than crashing.

---

### Requirement 12: Error Handling and Observability

**User Story:** As a platform engineer, I want all errors to be logged with enough context to diagnose issues, so that I can troubleshoot production problems without accessing user sessions.

#### Acceptance Criteria

1. THE Prism application SHALL log all unhandled exceptions, component-level errors, and HTTP error responses (4xx and 5xx) to the Databricks App logs in a structured format including: UTC timestamp, severity level (ERROR, WARN, or INFO), component name, error type, correlation ID, and a human-readable message.
2. THE Prism UI SHALL never display raw exception stack traces, raw SQL warehouse error messages, or internal system file paths to non-Admin users.
3. WHEN an error occurs in any component, THE Prism UI SHALL display a static, non-technical error message indicating that an error occurred and prompting the user to retry or contact support, without revealing internal error details.
4. WHEN an artifact fetch attempt is made, THE Artifact_Fetcher SHALL log the HTTP status code, the request URL with the `PRIVATE-TOKEN` header value removed, the response size in bytes, and the outcome (success or error) for that attempt.
5. WHEN a SQL generation attempt is made, THE SQL_Generator SHALL log the question text truncated to 500 characters, the selected model names, the Claude model identifier used, the token count, and the outcome (success or error) for that attempt.
6. WHEN a query execution attempt is made, THE Query_Runner SHALL log the executed SQL truncated to 2000 characters, the warehouse ID, the execution time in milliseconds, the row count returned, and the outcome (success or error) for that attempt.
7. THE Prism application SHALL assign a single correlation ID to each user-initiated request and include that correlation ID in every log entry produced by the Artifact_Fetcher, SQL_Generator, and Query_Runner components for that request, enabling end-to-end tracing of a single question through all components.

---

### Requirement 13: Performance

**User Story:** As a business user, I want to receive a query result within 10 seconds for a typical question, so that the tool feels responsive and replaces manual data engineering requests.

#### Acceptance Criteria

1. WHEN a user submits a question, THE Prism application SHALL render at least one result row visible in the UI within 10 seconds, measured from the time the question is submitted to the time the first row appears, under normal operating conditions defined as: SQL warehouse already running (not cold-starting), Schema_Index fully built, and Claude API responding within its typical SLA.
2. WHEN the Index_Builder is triggered after a successful artifact fetch, THE Index_Builder SHALL complete initial index construction including all model embeddings within 30 seconds for a schema of up to 500 dbt models on the application host hardware.
3. WHEN the Retriever receives a question embedding, THE Retriever SHALL return the top-ranked model list within 2 seconds for a Schema_Index containing up to 500 models on the application host hardware.
4. THE Embedder SHALL load the `all-MiniLM-L6-v2` model once during application startup and SHALL produce a vector embedding for a single question within 100 milliseconds on the application host hardware for all calls after the initial load.
5. THE Cache_Manager SHALL serve all cached artifact data and Schema_Index read operations from memory within 1 millisecond, with no disk I/O performed during a read operation.

---

### Requirement 14: Deployment and Configuration

**User Story:** As a platform engineer, I want to deploy Prism to any Databricks workspace by setting four environment variables, so that any team running dbt with GitLab CI can adopt it.

#### Acceptance Criteria

1. THE Prism application SHALL be deployable to Databricks Apps by completing four steps: (1) create a Databricks secret scope named exactly `prism-secrets` via the Databricks CLI; (2) add three secrets to that scope (`gitlab-token`, `anthropic-api-key`, `admin-password-hash`); (3) set four non-secret environment variables (`GITLAB_BASE_URL`, `GITLAB_PROJECT_ID`, `DATABRICKS_SQL_WAREHOUSE`, `DATABRICKS_SERVER_HOSTNAME` optional) in the Databricks App UI; (4) run `databricks apps deploy prism --source-code-path .` or point the App UI at the public GitHub repository URL — no other manual setup is required.
2. THE `app.yaml` SHALL define the application entry point as `uvicorn backend.main:app --host 0.0.0.0 --port 8000` and declare the following three secrets sourced exclusively from the `prism-secrets` Databricks secret scope: `GITLAB_TOKEN` (key `gitlab-token`), `ANTHROPIC_API_KEY` (key `anthropic-api-key`), and `ADMIN_PASSWORD_HASH` (key `admin-password-hash`). Non-secret variables (`GITLAB_BASE_URL`, `GITLAB_PROJECT_ID`, `DATABRICKS_SQL_WAREHOUSE`) SHALL be set in the Databricks App UI and SHALL NOT be declared in `app.yaml`.
3. THE Prism application SHALL require no external databases, no Redis, no vector stores, and no additional infrastructure beyond the Databricks Apps runtime and a Databricks SQL warehouse.
4. WHERE the `DATABRICKS_SQL_WAREHOUSE` environment variable is set to an invalid or non-existent warehouse ID, THE Query_Runner SHALL log a warning and fall back to the workspace default SQL warehouse; IF no default warehouse is resolvable, THE Query_Runner SHALL return a user-facing error on query execution indicating that no SQL warehouse is configured.
5. THE Prism repository SHALL include a `.env.example` file listing all required environment variables with placeholder values and one-line descriptions, a `README.md` that describes the deployment process in exactly four numbered steps, and a `.gitignore` that prevents `.env` and all `.env.*` variants (except `.env.example`) from being committed to version control.

---

### Requirement 15: Round-Trip Schema Integrity

**User Story:** As a data engineer, I want to verify that the schema parsing pipeline correctly preserves all model metadata from ingestion through to query generation, so that SQL generated by Claude uses accurate column names and types.

#### Acceptance Criteria

1. THE Index_Builder SHALL preserve all column names exactly as they appear in manifest.json and catalog.json — no renaming, normalisation, or case transformation SHALL be applied during index construction.
2. WHEN a dbt_Model is present in both manifest.json and catalog.json, THE Index_Builder SHALL produce a Schema_Index entry where the column types are sourced from catalog.json and the column descriptions are sourced from manifest.json; WHEN a dbt_Model is present in manifest.json but absent from catalog.json, THE Index_Builder SHALL use the declared column types from manifest.json and record the row count as 0.
3. THE Prompt_Builder SHALL include in the Claude system prompt every column name and type from the Schema_Index entry for each retrieved model, up to a maximum of 300 columns per model; IF a model has more than 300 columns, THE Prompt_Builder SHALL include the first 300 columns ordered by their position in the Schema_Index and log a warning with the model name and total column count.
4. WHEN the SQL_Generator receives a Claude response containing SQL, THE SQL_Generator SHALL extract all column name references from the generated SQL and verify each against the Schema_Index entry for the corresponding model; WHEN an unrecognised column name is detected, THE SQL_Generator SHALL log a warning including the model name and unrecognised column name.
5. IF an unrecognised column name is detected as described in criterion 4, THEN THE SQL_Generator SHALL set the `confidence` field to "low" and include the unrecognised column name and model name in the `confidence_reason` field of the response returned to the UI.

---

### Requirement 16: Distribution and Local Development

**User Story:** As a platform engineer at any company running dbt with GitLab CI, I want to deploy Prism directly from a public GitHub repository to my Databricks workspace without forking or copying the source code, and I also want to run it locally in Docker for evaluation, so that adoption requires minimal setup.

#### Acceptance Criteria

1. THE Prism repository SHALL include a `Dockerfile` that produces a self-contained Docker image using a multi-stage build: a `node:20-slim` stage compiles the React frontend via `npm run build`, and a `python:3.11-slim` stage installs all Python dependencies, copies the compiled frontend into `frontend/dist/`, and runs uvicorn under a non-root user (`prism`). Running `docker run --env-file .env -p 8000:8000 <image>` SHALL start the application with the full UI and API available on port 8000.

2. THE Prism repository SHALL include a `docker-compose.yml` file that reads all credentials from a `.env` file via `env_file: .env`, exposes port 8000, and configures a healthcheck against `http://localhost:8000/api/status` with a 60-second start period. No credential values SHALL appear in `docker-compose.yml` itself, and no credentials SHALL be passed as command-line arguments.

3. THE Prism repository SHALL include a GitHub Actions workflow at `.github/workflows/docker-publish.yml` that automatically builds and publishes a Docker image to GitHub Container Registry (GHCR) on every push to `main` (tagged `:latest`) and on every GitHub Release (tagged with the semantic version, e.g. `:v1.2.0`). The workflow SHALL require no secrets beyond the automatically-provided `GITHUB_TOKEN` — no additional manual secret configuration SHALL be needed.

4. THE `AppConfig.from_env()` factory SHALL invoke `python-dotenv`'s `load_dotenv()` as its first action before reading any environment variable, so that a `.env` file in the project root is automatically loaded when running locally. This call SHALL be a silent no-op when no `.env` file is present (Databricks Apps production environment), with no error or warning logged.

5. A company deploying Prism to their Databricks workspace SHALL NOT be required to fork or copy the Prism source code into a private repository. Instead, they SHALL be able to point the Databricks App UI directly at the public GitHub repository URL. The only configuration required SHALL be: (1) create a Databricks secret scope named exactly `prism-secrets` in their workspace, (2) add three secrets to that scope (`gitlab-token`, `anthropic-api-key`, `admin-password-hash`), (3) set non-secret environment variables in the Databricks App UI, and (4) click Deploy.
