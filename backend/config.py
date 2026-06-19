"""
backend/config.py

Application configuration loaded from environment variables,
plus secret-masking helpers for safe log output.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv


# ---------------------------------------------------------------------------
# Configuration dataclass
# ---------------------------------------------------------------------------


@dataclass
class AppConfig:
    """All runtime configuration derived from environment variables.

    Secrets (gitlab_token, anthropic_api_key) are retrieved from the
    Databricks secret scope at startup and stored here; they must NEVER
    appear in log output — use mask_secret() / display_token() instead.
    """

    gitlab_base_url: str
    gitlab_project_id: str
    gitlab_token: str            # from Databricks secret scope
    databricks_sql_warehouse: str
    admin_password_hash: str     # bcrypt hash of the admin password

    # LLM provider — at least one must be set. Anthropic takes priority.
    anthropic_api_key: str = ""  # from Databricks secret scope (ANTHROPIC_API_KEY)
    openrouter_api_key: str = "" # fallback: use OpenRouter if no Anthropic key (OPENROUTER_API_KEY)

    # Optional: Databricks server hostname. When empty the connector infers it
    # from the DATABRICKS_HOST environment variable (set automatically in Apps).
    databricks_server_hostname: str = ""

    default_row_limit: int = 1000
    refresh_interval_hours: int = 6
    retry_interval_minutes: int = 5

    @classmethod
    def from_env(cls) -> "AppConfig":
        """Construct AppConfig from environment variables.

        Loads a .env file automatically when running locally — silently ignored
        in production (Databricks Apps) where secrets come from the secret scope.

        Raises ``KeyError`` if any required variable is absent.
        """
        load_dotenv()  # no-op if .env doesn't exist
        anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
        openrouter_key = os.environ.get("OPENROUTER_API_KEY", "")
        if not anthropic_key and not openrouter_key:
            raise KeyError(
                "At least one of ANTHROPIC_API_KEY or OPENROUTER_API_KEY must be set"
            )
        return cls(
            gitlab_base_url=os.environ["GITLAB_BASE_URL"],
            gitlab_project_id=os.environ["GITLAB_PROJECT_ID"],
            gitlab_token=os.environ["GITLAB_TOKEN"],
            databricks_sql_warehouse=os.environ["DATABRICKS_SQL_WAREHOUSE"],
            admin_password_hash=os.environ["ADMIN_PASSWORD_HASH"],
            anthropic_api_key=anthropic_key,
            openrouter_api_key=openrouter_key,
            databricks_server_hostname=os.environ.get("DATABRICKS_SERVER_HOSTNAME", ""),
            default_row_limit=int(os.environ.get("DEFAULT_ROW_LIMIT", "1000")),
            refresh_interval_hours=int(os.environ.get("REFRESH_INTERVAL_HOURS", "6")),
            retry_interval_minutes=int(os.environ.get("RETRY_INTERVAL_MINUTES", "5")),
        )


# ---------------------------------------------------------------------------
# Secret masking helpers
# ---------------------------------------------------------------------------


def mask_secret(value: str, secret_type: str) -> str:
    """Return a masked representation of *value* suitable for log output.

    Rules:
    - GITLAB_TOKEN / ANTHROPIC_API_KEY: replace all but the last 4 chars
      with asterisks.  If the value is 4 chars or shorter, mask entirely.
    - DATABRICKS_SQL_WAREHOUSE: always return ``"***MASKED***"``.
    - Any other secret_type: return value unchanged (caller should not pass
      unknown sensitive types here).
    """
    if secret_type in ("GITLAB_TOKEN", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY"):
        if len(value) <= 4:
            return "*" * len(value)
        return "*" * (len(value) - 4) + value[-4:]
    elif secret_type == "DATABRICKS_SQL_WAREHOUSE":
        return "***MASKED***"
    return value


def display_token(value: str) -> str:
    """Return a fixed-width masked token for display in the Settings UI.

    Always produces exactly 12 asterisks followed by the last 4 characters,
    regardless of the actual token length — this prevents length disclosure.
    If the token is 4 characters or shorter, return 16 asterisks.
    """
    if len(value) <= 4:
        return "*" * 16
    return "************" + value[-4:]  # exactly 12 asterisks + last 4
