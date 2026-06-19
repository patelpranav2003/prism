"""
tests/unit/test_config.py

Unit tests for backend/config.py:
- mask_secret() behaviour for each secret type
- display_token() fixed-width masking
- AppConfig.from_env() reads correct env vars

Property-based tests:
- Property 19: Token Masking (validates Requirements 10.3, 11.3, 11.4)
"""

import os
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from backend.config import AppConfig, display_token, mask_secret


# ---------------------------------------------------------------------------
# Unit tests — mask_secret
# ---------------------------------------------------------------------------


class TestMaskSecret:
    def test_gitlab_token_long(self):
        value = "glpat-abcdefgh1234"
        result = mask_secret(value, "GITLAB_TOKEN")
        assert result.endswith("1234")
        assert "*" in result
        assert len(result) == len(value)

    def test_gitlab_token_exactly_4_chars(self):
        result = mask_secret("abcd", "GITLAB_TOKEN")
        assert result == "****"

    def test_gitlab_token_shorter_than_4_chars(self):
        result = mask_secret("ab", "GITLAB_TOKEN")
        assert result == "**"

    def test_anthropic_api_key(self):
        value = "sk-ant-api03-xxxxxxxxxxxx1234"
        result = mask_secret(value, "ANTHROPIC_API_KEY")
        assert result.endswith("1234")
        assert len(result) == len(value)

    def test_databricks_warehouse_always_fully_masked(self):
        assert mask_secret("any-warehouse-id", "DATABRICKS_SQL_WAREHOUSE") == "***MASKED***"
        assert mask_secret("short", "DATABRICKS_SQL_WAREHOUSE") == "***MASKED***"

    def test_unknown_type_returns_value_unchanged(self):
        assert mask_secret("somevalue", "OTHER") == "somevalue"


# ---------------------------------------------------------------------------
# Unit tests — display_token
# ---------------------------------------------------------------------------


class TestDisplayToken:
    def test_long_token_has_12_asterisks_plus_last_4(self):
        token = "glpat-abcdefghijklmnop1234"
        result = display_token(token)
        assert result == "************" + token[-4:]
        assert len(result) == 16

    def test_exactly_4_chars_returns_16_asterisks(self):
        assert display_token("abcd") == "*" * 16

    def test_shorter_than_4_chars_returns_16_asterisks(self):
        assert display_token("ab") == "*" * 16

    def test_5_chars_shows_last_4(self):
        result = display_token("xabcd")
        assert result == "************abcd"
        assert len(result) == 16

    def test_output_always_16_chars_for_any_long_token(self):
        for length in range(5, 50):
            token = "x" * length
            result = display_token(token)
            assert len(result) == 16, f"Expected 16 chars for token of length {length}"


# ---------------------------------------------------------------------------
# Property-based test — Property 19: Token Masking
# Validates: Requirements 10.3, 11.3, 11.4
# ---------------------------------------------------------------------------


@given(value=st.text(min_size=0, max_size=200))
@settings(max_examples=100)
def test_property_19_display_token_always_16_chars(value: str):
    """Property 19: Token Masking
    
    For any secret value string, display_token SHALL always produce a display
    string of exactly 12 asterisk characters followed by the last 4 characters
    of the token (or all asterisks if the token is 4 characters or fewer).

    Validates: Requirements 10.3, 11.3, 11.4
    """
    result = display_token(value)
    # Result must always be exactly 16 characters
    assert len(result) == 16, f"display_token({value!r}) returned {result!r} (len={len(result)})"

    if len(value) <= 4:
        # Fully masked — all asterisks
        assert result == "*" * 16
    else:
        # 12 asterisks + last 4 chars
        assert result[:12] == "*" * 12
        assert result[12:] == value[-4:]


@given(
    value=st.text(min_size=5, max_size=200),
    secret_type=st.sampled_from(["GITLAB_TOKEN", "ANTHROPIC_API_KEY"]),
)
@settings(max_examples=100)
def test_property_19_mask_secret_length_preserving(value: str, secret_type: str):
    """mask_secret for token types preserves string length and shows last 4 chars."""
    result = mask_secret(value, secret_type)
    assert len(result) == len(value)
    assert result[-4:] == value[-4:]
    assert result[:-4] == "*" * (len(value) - 4)


@given(value=st.text(min_size=0, max_size=4))
@settings(max_examples=100)
def test_property_19_mask_secret_short_tokens_fully_masked(value: str):
    """Short tokens (≤4 chars) are fully masked for GITLAB_TOKEN."""
    result = mask_secret(value, "GITLAB_TOKEN")
    assert result == "*" * len(value)


# ---------------------------------------------------------------------------
# Unit tests — AppConfig.from_env
# ---------------------------------------------------------------------------


class TestAppConfigFromEnv:
    def test_reads_required_env_vars(self, monkeypatch):
        monkeypatch.setenv("GITLAB_BASE_URL", "https://gitlab.example.com/api/v4")
        monkeypatch.setenv("GITLAB_PROJECT_ID", "42")
        monkeypatch.setenv("GITLAB_TOKEN", "glpat-test")
        monkeypatch.setenv("DATABRICKS_SQL_WAREHOUSE", "wh-id")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setenv("ADMIN_PASSWORD_HASH", "$2b$12$hash")

        config = AppConfig.from_env()
        assert config.gitlab_base_url == "https://gitlab.example.com/api/v4"
        assert config.gitlab_project_id == "42"
        assert config.gitlab_token == "glpat-test"
        assert config.databricks_sql_warehouse == "wh-id"
        assert config.anthropic_api_key == "sk-ant-test"
        assert config.admin_password_hash == "$2b$12$hash"
        assert config.default_row_limit == 1000
        assert config.refresh_interval_hours == 6
        assert config.retry_interval_minutes == 5

    def test_reads_optional_env_vars(self, monkeypatch):
        monkeypatch.setenv("GITLAB_BASE_URL", "https://gitlab.example.com/api/v4")
        monkeypatch.setenv("GITLAB_PROJECT_ID", "42")
        monkeypatch.setenv("GITLAB_TOKEN", "glpat-test")
        monkeypatch.setenv("DATABRICKS_SQL_WAREHOUSE", "wh-id")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setenv("ADMIN_PASSWORD_HASH", "$2b$12$hash")
        monkeypatch.setenv("DEFAULT_ROW_LIMIT", "500")
        monkeypatch.setenv("REFRESH_INTERVAL_HOURS", "12")
        monkeypatch.setenv("RETRY_INTERVAL_MINUTES", "10")

        config = AppConfig.from_env()
        assert config.default_row_limit == 500
        assert config.refresh_interval_hours == 12
        assert config.retry_interval_minutes == 10

    def test_raises_on_missing_required_var(self, monkeypatch):
        # Clear all potentially set env vars
        for key in [
            "GITLAB_BASE_URL", "GITLAB_PROJECT_ID", "GITLAB_TOKEN",
            "DATABRICKS_SQL_WAREHOUSE", "ANTHROPIC_API_KEY", "ADMIN_PASSWORD_HASH",
        ]:
            monkeypatch.delenv(key, raising=False)

        with pytest.raises(KeyError):
            AppConfig.from_env()
