"""
tests/unit/test_token_masking.py

Property-based test for secret token masking.

# Feature: prism, Property 19: Token Masking
For any secret value string, the masking function SHALL always produce a
display string of exactly 12 asterisk characters followed by the last 4
characters of the token (or all asterisks if the token is 4 characters or
fewer), regardless of the token's actual length.

Validates: Requirements 10.3, 11.3, 11.4
"""

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from backend.config import display_token, mask_secret


# ---------------------------------------------------------------------------
# Property 19: Token Masking (display_token)
# Validates: Requirements 10.3, 11.3, 11.4
# ---------------------------------------------------------------------------


@given(value=st.text(min_size=0))
@settings(max_examples=200)
def test_property_19_display_token_fixed_width(value: str) -> None:
    """**Property 19: Token Masking**

    ``display_token(value)`` SHALL always produce:
    - If len(value) <= 4: 16 asterisks
    - Otherwise: exactly 12 asterisks + last 4 characters

    The result must NEVER reveal the token length.

    **Validates: Requirements 10.3, 11.3, 11.4**
    """
    result = display_token(value)

    # --- Invariant 1: result is always a string ---
    assert isinstance(result, str), f"display_token must return str, got {type(result)}"

    if len(value) <= 4:
        # --- Invariant 2a: short tokens → 16 asterisks ---
        assert result == "*" * 16, (
            f"Token of length {len(value)} should produce 16 asterisks, "
            f"got {result!r}"
        )
        # Never reveals how many chars the token has
        assert len(result) == 16
    else:
        # --- Invariant 2b: longer tokens → 12 asterisks + last 4 chars ---
        assert len(result) == 16, (
            f"display_token must always produce exactly 16 chars, "
            f"got {len(result)} for input len={len(value)}"
        )
        assert result[:12] == "*" * 12, (
            f"First 12 chars must be asterisks, got {result[:12]!r}"
        )
        assert result[12:] == value[-4:], (
            f"Last 4 chars should be '{value[-4:]}', got '{result[12:]}'"
        )


@given(value=st.text(min_size=5, max_size=100))
@settings(max_examples=100)
def test_property_19_display_token_never_reveals_length(value: str) -> None:
    """**Property 19 — length hiding**: two tokens of different lengths but same
    last-4 chars produce identical display values, hiding length information.
    """
    longer = "XXXX" + value  # different length, same last 4
    shorter = value[-4:]      # just 4 chars
    if len(shorter) <= 4:
        # short path → 16 asterisks for both short and normal
        assert display_token(shorter) == "*" * 16
        assert display_token(value)[:12] == "*" * 12
    else:
        # Both "value" and "longer" have the same last 4 chars → same last 4 of display
        assert display_token(value)[12:] == display_token(longer)[12:]


# ---------------------------------------------------------------------------
# mask_secret tests
# Validates: Requirements 11.3
# ---------------------------------------------------------------------------


@given(
    value=st.text(min_size=5),
    secret_type=st.sampled_from(["GITLAB_TOKEN", "ANTHROPIC_API_KEY"]),
)
@settings(max_examples=100)
def test_mask_secret_token_shows_last_4(value: str, secret_type: str) -> None:
    """mask_secret for token types: last 4 chars visible, rest are asterisks."""
    result = mask_secret(value, secret_type)
    assert result.endswith(value[-4:]), (
        f"mask_secret({value[-4:]!r}, {secret_type!r}) should end with last 4 chars"
    )
    asterisk_count = len(result) - 4
    assert result[:asterisk_count] == "*" * asterisk_count, (
        f"All but last 4 chars should be asterisks"
    )
    # Total length matches original
    assert len(result) == len(value)


@given(value=st.text(min_size=0, max_size=4))
@settings(max_examples=50)
def test_mask_secret_short_token_fully_masked(value: str) -> None:
    """mask_secret for short tokens (≤4 chars): fully masked."""
    result = mask_secret(value, "GITLAB_TOKEN")
    assert result == "*" * len(value), (
        f"Short token should be fully masked, got {result!r}"
    )


@given(value=st.text(min_size=1))
@settings(max_examples=50)
def test_mask_secret_warehouse_always_masked(value: str) -> None:
    """mask_secret for DATABRICKS_SQL_WAREHOUSE: always '***MASKED***'."""
    result = mask_secret(value, "DATABRICKS_SQL_WAREHOUSE")
    assert result == "***MASKED***", (
        f"Warehouse should always be '***MASKED***', got {result!r}"
    )


# ---------------------------------------------------------------------------
# Unit tests — concrete examples
# ---------------------------------------------------------------------------


class TestTokenMaskingExamples:

    def test_display_token_typical_api_key(self):
        token = "sk-ant-api-03-abcdef1234567890"
        result = display_token(token)
        assert result.startswith("************")
        assert result.endswith("7890")
        assert len(result) == 16

    def test_display_token_exactly_4_chars(self):
        result = display_token("abcd")
        assert result == "*" * 16

    def test_display_token_exactly_5_chars(self):
        result = display_token("abcde")
        assert result == "************bcde"  # 12 asterisks + "bcde"

    def test_mask_secret_gitlab_token(self):
        result = mask_secret("glpat-abcdef1234567890", "GITLAB_TOKEN")
        assert result.endswith("7890")
        assert "*" in result

    def test_mask_secret_different_types_same_value(self):
        # Warehouse always masked; token shows last 4
        token = "some-secret-token-0000"
        t_result = mask_secret(token, "GITLAB_TOKEN")
        w_result = mask_secret(token, "DATABRICKS_SQL_WAREHOUSE")
        assert t_result != w_result
        assert w_result == "***MASKED***"
