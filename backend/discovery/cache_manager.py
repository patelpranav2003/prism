"""
backend/discovery/cache_manager.py

Cache_Manager — thread-safe in-memory store for dbt artifacts and the
active SchemaIndex.

Responsibilities
----------------
- Hold the last successfully fetched ArtifactBundle and built SchemaIndex
  in memory for the process lifetime.
- Expose ``get_index()``, ``get_status()``, and ``get_meta()`` for reads;
  all reads always reflect the **previous good state** while a refresh is
  in progress.
- Perform atomic index swaps via ``asyncio.Lock`` (``swap_index()``).
- Run a background refresh loop on a 6-hour cycle (``asyncio.create_task``);
  on failure, retry every 5 minutes indefinitely.
- Trigger an immediate out-of-cycle refresh on demand via ``refresh()``.

Design decisions
----------------
- A single ``asyncio.Lock`` serialises concurrent refresh attempts so the
  background loop and a manual admin refresh never race.
- All reads (``get_index``, ``get_status``, ``get_meta``) are lock-free and
  therefore zero-latency — they always reflect the last committed state.
- The background loop is started via ``start_background_refresh()`` which
  must be called once at application startup (e.g. in the FastAPI lifespan
  hook).

Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.7
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Literal

from backend.models import ArtifactBundle, CacheState, SchemaIndex

if TYPE_CHECKING:
    from backend.config import AppConfig
    from backend.discovery.gitlab_fetcher import ArtifactFetcher
    from backend.discovery.index_builder import IndexBuilder
    from backend.exceptions import FetchError
    from backend.search.embedder import Embedder

logger = logging.getLogger(__name__)

# Type aliases to match the design doc
CacheStatus = Literal["fresh", "stale", "unavailable"]


@dataclass
class CacheMeta:
    """Snapshot of cache state metadata exposed to the ``/api/status`` endpoint.

    Attributes:
        last_refresh_utc: UTC timestamp of the last successful refresh, or
            ``None`` if no refresh has ever succeeded.
        model_count: Number of models in the current SchemaIndex, or ``0``
            if no index is available.
        status: Current cache health.
    """

    last_refresh_utc: datetime | None
    model_count: int
    status: CacheStatus


@dataclass
class RefreshResult:
    """Result object returned by :meth:`CacheManager.refresh`.

    Attributes:
        success: ``True`` when the refresh completed without error.
        model_count: Number of models in the newly built index, or ``None``
            on failure.
        error: Human-readable failure reason, or ``None`` on success.
    """

    success: bool
    model_count: int | None
    error: str | None


class CacheManager:
    """In-memory cache for dbt artifacts and the active SchemaIndex.

    The cache holds a single :class:`~backend.models.CacheState` which is
    updated atomically.  All public read methods (``get_index``,
    ``get_status``, ``get_meta``) are lock-free and always return the last
    committed good state.

    Parameters
    ----------
    config:
        Application configuration (used to read refresh interval settings).
    fetcher:
        :class:`~backend.discovery.gitlab_fetcher.ArtifactFetcher` used to
        download the three dbt artifacts.
    index_builder:
        :class:`~backend.discovery.index_builder.IndexBuilder` used to
        construct a :class:`~backend.models.SchemaIndex` from raw artifact
        bytes.
    """

    def __init__(
        self,
        config: "AppConfig",
        fetcher: "ArtifactFetcher",
        index_builder: "IndexBuilder",
        embedder: "Embedder",
    ) -> None:
        self._config = config
        self._fetcher = fetcher
        self._index_builder = index_builder
        self._embedder = embedder

        # Initialise to a clean unavailable state.  The lock is created here
        # so it is bound to the correct event loop when the object is
        # constructed inside an async context (or via ``asyncio.run``).
        self._state = CacheState(
            bundle=None,
            index=None,
            status="unavailable",
            last_refresh_utc=None,
        )

        # Background task handle — kept so it can be inspected in tests.
        self._background_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Read API (lock-free, zero-latency) — Requirement 2.3
    # ------------------------------------------------------------------

    def get_index(self) -> SchemaIndex | None:
        """Return the current :class:`~backend.models.SchemaIndex`, or ``None``.

        Always returns the last *committed* (good) index.  Callers receive
        no interruption or added latency while a refresh is in progress.

        Returns
        -------
        SchemaIndex | None
            The active index, or ``None`` if no successful fetch has
            completed yet (Requirement 2.1).
        """
        return self._state.index

    def get_status(self) -> CacheStatus:
        """Return the current cache status string.

        Returns
        -------
        CacheStatus
            One of ``"fresh"``, ``"stale"``, or ``"unavailable"``
            (Requirement 2.7).
        """
        return self._state.status

    def get_meta(self) -> CacheMeta:
        """Return a snapshot of cache metadata for the ``/api/status`` endpoint.

        Returns
        -------
        CacheMeta
            Contains ``last_refresh_utc``, ``model_count``, and ``status``.
            ``model_count`` is ``0`` when no index is available (Requirement 2.7).
        """
        index = self._state.index
        return CacheMeta(
            last_refresh_utc=self._state.last_refresh_utc,
            model_count=index.model_count if index is not None else 0,
            status=self._state.status,
        )

    # ------------------------------------------------------------------
    # Write API
    # ------------------------------------------------------------------

    def swap_index(self, new_index: SchemaIndex) -> None:
        """Atomically replace the active index with *new_index*.

        This method is called *within* a held ``refresh_lock`` so the swap
        itself is serialised.  Reads (``get_index`` etc.) observe either the
        old or the new index — never a partial state — because Python's GIL
        ensures that attribute assignment is atomic for simple object
        references.

        Parameters
        ----------
        new_index:
            The freshly built :class:`~backend.models.SchemaIndex` that
            has passed validation (Requirement 2.5).
        """
        self._state.index = new_index
        self._state.last_refresh_utc = datetime.now(tz=timezone.utc)
        self._state.status = "fresh"
        logger.info(
            "CacheManager: index swapped — %d model(s), status=fresh",
            new_index.model_count,
        )

    # ------------------------------------------------------------------
    # Refresh logic
    # ------------------------------------------------------------------

    async def refresh(self) -> RefreshResult:
        """Perform a full artifact fetch → index build → atomic swap cycle.

        This method can be called directly (e.g. by the admin "Refresh Schema
        Now" button) or by the background loop.  Concurrent calls are
        serialised by ``refresh_lock``: if a refresh is already in progress
        the second caller waits.

        While this coroutine runs, all read methods continue serving the
        **previous** good state without interruption (Requirement 2.3, 2.5).

        Returns
        -------
        RefreshResult
            ``success=True`` with updated ``model_count`` on success;
            ``success=False`` with an ``error`` message on failure.
        """
        async with self._state.refresh_lock:
            logger.info("CacheManager: starting refresh cycle")

            # --- Step 1: Fetch artifacts ---
            from backend.exceptions import FetchError  # local import to avoid cycle

            result = await self._fetcher.fetch_all()

            if isinstance(result, FetchError):
                # Total fetch failure — all three files failed.
                error_msg = str(result)
                logger.error(
                    "CacheManager: artifact fetch failed — %s; "
                    "preserving previous cache, setting status=unavailable "
                    "if no prior index exists",
                    error_msg,
                )
                if self._state.index is None:
                    # No prior index exists; mark unavailable (Requirement 2.1)
                    self._state.status = "unavailable"
                else:
                    # Preserve last good index; mark stale (Requirement 2.4)
                    self._state.status = "stale"
                return RefreshResult(success=False, model_count=None, error=error_msg)

            bundle: ArtifactBundle = result

            # Detect partial bundle (any empty bytes payload = failed file).
            # The ArtifactFetcher logs per-file failures before returning.
            if not bundle.manifest or not bundle.catalog or not bundle.graph:
                logger.warning(
                    "CacheManager: partial artifact bundle received — "
                    "one or more files missing; setting status=stale"
                )
                self._state.status = "stale"
                # Keep previous index but store the partial bundle for inspection.
                self._state.bundle = bundle
                return RefreshResult(
                    success=False,
                    model_count=None,
                    error="Partial artifact fetch — one or more files unavailable.",
                )

            # --- Step 2: Build index ---
            # While the builder runs, existing reads still see the old index.
            previous_index = self._state.index
            new_index = self._index_builder.build(bundle, previous_index=previous_index)

            if new_index is None:
                # IndexBuilder returns None when all three parsers failed AND
                # there is no previous index (caller should set "unavailable").
                error_msg = "Index build failed and no previous index exists."
                logger.error("CacheManager: %s", error_msg)
                self._state.status = "unavailable"
                return RefreshResult(success=False, model_count=None, error=error_msg)

            if new_index is previous_index:
                # IndexBuilder returned the *same* previous_index object —
                # meaning a parse error occurred but a fallback was available.
                error_msg = "Index build failed — preserved previous SchemaIndex."
                logger.warning("CacheManager: %s", error_msg)
                self._state.status = "stale"
                return RefreshResult(success=False, model_count=None, error=error_msg)

            # --- Step 3: Generate embeddings ---
            new_index.embeddings = self._embedder.embed_models(new_index.models)

            # --- Step 4: Atomic swap (Requirement 2.5) ---
            self._state.bundle = bundle
            self.swap_index(new_index)

            logger.info(
                "CacheManager: refresh complete — %d model(s) loaded",
                new_index.model_count,
            )
            return RefreshResult(
                success=True,
                model_count=new_index.model_count,
                error=None,
            )

    # ------------------------------------------------------------------
    # Background loop
    # ------------------------------------------------------------------

    def start_background_refresh(self) -> None:
        """Schedule the 6-hour background refresh loop as an asyncio task.

        Must be called **once** from within a running event loop (e.g. inside
        a FastAPI ``lifespan`` startup handler).  Calling it a second time
        creates a duplicate task, so guard with a flag if needed.

        The loop runs indefinitely:
        - On success: sleep 6 hours, then refresh again.
        - On failure: sleep 5 minutes, retry indefinitely until a refresh
          succeeds (Requirement 2.4).
        """
        self._background_task = asyncio.create_task(
            self._refresh_loop(),
            name="cache_manager_refresh_loop",
        )
        logger.info(
            "CacheManager: background refresh loop started "
            "(interval=%dh, retry=%dmin)",
            self._config.refresh_interval_hours,
            self._config.retry_interval_minutes,
        )

    async def _refresh_loop(self) -> None:
        """Internal background coroutine — runs the periodic refresh schedule.

        Cycle:
        1. Attempt a full refresh.
        2a. Success → sleep ``refresh_interval_hours`` × 3600 seconds.
        2b. Failure → sleep ``retry_interval_minutes`` × 60 seconds, retry.
        """
        refresh_sleep_seconds = self._config.refresh_interval_hours * 3600
        retry_sleep_seconds = self._config.retry_interval_minutes * 60

        while True:
            result = await self.refresh()

            if result.success:
                logger.info(
                    "CacheManager: next scheduled refresh in %d hour(s)",
                    self._config.refresh_interval_hours,
                )
                await asyncio.sleep(refresh_sleep_seconds)
            else:
                logger.warning(
                    "CacheManager: refresh failed (%s); "
                    "retrying in %d minute(s)",
                    result.error,
                    self._config.retry_interval_minutes,
                )
                await asyncio.sleep(retry_sleep_seconds)
