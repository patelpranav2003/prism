"""
tests/unit/test_cache_status.py

Property-based test for CacheManager status response completeness.

# Feature: prism, Property 2: Cache Status Response Completeness
For any internal CacheState, the /api/status response SHALL always contain
all three fields: cache_status, last_refresh_utc, and model_count, with
appropriate zero values when no index exists.

Validates: Requirements 2.7
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from backend.discovery.cache_manager import CacheManager, CacheMeta
from backend.models import ArtifactBundle, CacheState, SchemaIndex


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_status_strategy = st.sampled_from(["fresh", "stale", "unavailable"])

_datetime_strategy = st.one_of(
    st.none(),
    st.datetimes(
        min_value=datetime(2020, 1, 1),
        max_value=datetime(2030, 12, 31),
        timezones=st.just(timezone.utc),
    ),
)


def _make_minimal_index(model_count: int) -> SchemaIndex:
    return SchemaIndex(
        models=[],
        embeddings=np.empty((0,), dtype=np.float32),
        lineage={},
        built_at=datetime.now(tz=timezone.utc),
        model_count=model_count,
    )


# ---------------------------------------------------------------------------
# Property 2: Cache Status Response Completeness
# Validates: Requirements 2.7
# ---------------------------------------------------------------------------


@given(
    status=_status_strategy,
    last_refresh_utc=_datetime_strategy,
    model_count=st.integers(min_value=0, max_value=5000),
    has_index=st.booleans(),
)
@settings(max_examples=100)
def test_property_2_cache_status_response_completeness(
    status: str,
    last_refresh_utc: datetime | None,
    model_count: int,
    has_index: bool,
) -> None:
    """**Property 2: Cache Status Response Completeness**

    For any internal CacheState, the /api/status response (CacheMeta) SHALL
    always contain all three fields: cache_status, last_refresh_utc, and
    model_count, with appropriate zero values when no index exists.

    **Validates: Requirements 2.7**
    """
    # Build a CacheManager with injected state
    config = MagicMock()
    config.refresh_interval_hours = 6
    config.retry_interval_minutes = 5
    fetcher = MagicMock()
    index_builder = MagicMock()

    manager = CacheManager(config, fetcher, index_builder)

    # Inject state directly
    manager._state.status = status  # type: ignore[assignment]
    manager._state.last_refresh_utc = last_refresh_utc
    if has_index:
        manager._state.index = _make_minimal_index(model_count)
    else:
        manager._state.index = None

    # Get the meta (mirrors what /api/status returns)
    meta: CacheMeta = manager.get_meta()

    # --- Invariant 1: All three fields exist ---
    assert hasattr(meta, "cache_status"), "CacheMeta must have 'cache_status'"
    assert hasattr(meta, "last_refresh_utc"), "CacheMeta must have 'last_refresh_utc'"
    assert hasattr(meta, "model_count"), "CacheMeta must have 'model_count'"

    # --- Invariant 2: cache_status is one of the three valid values ---
    assert meta.cache_status in {"fresh", "stale", "unavailable"}, (
        f"cache_status must be 'fresh'/'stale'/'unavailable', got {meta.cache_status!r}"
    )

    # --- Invariant 3: cache_status matches the injected state ---
    assert meta.cache_status == status

    # --- Invariant 4: model_count is 0 when no index ---
    if not has_index:
        assert meta.model_count == 0, (
            f"model_count must be 0 when index is None, got {meta.model_count}"
        )

    # --- Invariant 5: model_count is non-negative ---
    assert meta.model_count >= 0, f"model_count must be >= 0, got {meta.model_count}"

    # --- Invariant 6: last_refresh_utc matches the injected value ---
    assert meta.last_refresh_utc == last_refresh_utc


# ---------------------------------------------------------------------------
# Unit tests — concrete examples
# ---------------------------------------------------------------------------


class TestCacheStatusExamples:
    """Concrete spot-checks for the most important CacheMeta states."""

    def _manager(self) -> CacheManager:
        config = MagicMock()
        config.refresh_interval_hours = 6
        config.retry_interval_minutes = 5
        return CacheManager(config, MagicMock(), MagicMock())

    def test_initial_state_is_unavailable(self):
        m = self._manager()
        meta = m.get_meta()
        assert meta.cache_status == "unavailable"
        assert meta.model_count == 0
        assert meta.last_refresh_utc is None

    def test_after_swap_status_is_fresh(self):
        m = self._manager()
        index = _make_minimal_index(42)
        m.swap_index(index)
        meta = m.get_meta()
        assert meta.cache_status == "fresh"
        assert meta.model_count == 42
        assert meta.last_refresh_utc is not None

    def test_status_response_never_missing_fields(self):
        """Ensure all three fields always exist in the returned CacheMeta."""
        m = self._manager()
        for s in ("fresh", "stale", "unavailable"):
            m._state.status = s  # type: ignore[assignment]
            meta = m.get_meta()
            assert meta.cache_status == s
            # model_count and last_refresh_utc must always be present (even if None/0)
            assert "model_count" in meta.__dataclass_fields__  # type: ignore[attr-defined]
            assert "last_refresh_utc" in meta.__dataclass_fields__  # type: ignore[attr-defined]
