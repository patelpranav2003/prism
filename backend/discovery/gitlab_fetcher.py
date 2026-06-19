"""
backend/discovery/gitlab_fetcher.py

Artifact_Fetcher — downloads the three dbt CI artifacts from GitLab.

Files fetched:
    - manifest.json
    - catalog.json
    - graph_summary.json

URL pattern:
    {GITLAB_BASE_URL}/projects/{GITLAB_PROJECT_ID}/jobs/artifacts/main/raw/public/{filename}?job=pages

Auth:
    PRIVATE-TOKEN header, value from AppConfig.gitlab_token (retrieved from
    the Databricks secret scope at startup).

Error handling:
    - Missing token / HTTP 401,403  → log masked info, return FetchError
    - Individual file failure        → log per-file, mark file failed
    - Partial failure (1-2 files)    → return partial ArtifactBundle + set "stale"
    - Total failure (all 3 files)    → return FetchError + set "unavailable"
    - Success                        → return complete ArtifactBundle

Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 11.1
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import httpx

from backend.config import AppConfig, mask_secret
from backend.exceptions import FetchError
from backend.models import ArtifactBundle

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# The three artifact filenames expected in every successful fetch
_ARTIFACT_FILES = ("manifest.json", "catalog.json", "graph_summary.json")

# Per-request timeout in seconds (Requirement 1.5)
_TIMEOUT_SECONDS = 30


class ArtifactFetcher:
    """Downloads dbt CI artifacts from GitLab concurrently.

    Constructed with an :class:`~backend.config.AppConfig` instance; all
    configuration (base URL, project ID, token) is read from there so that
    runtime updates propagated by the Settings page take effect immediately on
    the next refresh cycle.

    Args:
        config: Application configuration holding GitLab credentials and URLs.
    """

    def __init__(self, config: AppConfig) -> None:
        self._config = config

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def fetch_all(self) -> ArtifactBundle | FetchError:
        """Fetch all three dbt artifacts concurrently.

        Returns:
            An :class:`~backend.models.ArtifactBundle` when all three files
            are downloaded successfully, or a :class:`~backend.exceptions.FetchError`
            when all three fetches fail.  Partial failures (1-2 files) are
            handled internally: missing bytes are replaced with ``b""`` so the
            caller can detect them and set cache status to ``"stale"``.

        Note:
            This method does **not** raise — all error states are returned as
            values so the caller (CacheManager) controls status transitions.
        """
        token = self._config.gitlab_token

        # Requirement 11.1 / 1.4: missing token must never appear in logs
        if not token:
            logger.error(
                "GITLAB_TOKEN is absent from the Databricks secret scope; "
                "setting cache status to unavailable. "
                "(scope=databricks-secrets, key=GITLAB_TOKEN)"
            )
            return FetchError(
                "GITLAB_TOKEN is missing — cannot fetch GitLab artifacts."
            )

        timeout = httpx.Timeout(timeout=_TIMEOUT_SECONDS)
        headers = {"PRIVATE-TOKEN": token}

        async with httpx.AsyncClient(headers=headers, timeout=timeout) as client:
            results: list[bytes | FetchError] = list(
                await asyncio.gather(
                    *[self._fetch_one(filename, client) for filename in _ARTIFACT_FILES],
                    return_exceptions=False,
                )
            )

        manifest_result, catalog_result, graph_result = results

        # Count failures
        failures = [r for r in results if isinstance(r, FetchError)]

        if len(failures) == len(_ARTIFACT_FILES):
            # Total failure — all three files failed (Requirement 1.6 / 6 last AC)
            logger.error(
                "All three artifact fetches failed; setting cache status to unavailable."
            )
            return FetchError("All artifact fetches failed.")

        # At least one succeeded — build a bundle (partial bytes are b"" for failed files)
        return ArtifactBundle(
            manifest=manifest_result if isinstance(manifest_result, bytes) else b"",
            catalog=catalog_result if isinstance(catalog_result, bytes) else b"",
            graph=graph_result if isinstance(graph_result, bytes) else b"",
            fetched_at=datetime.now(tz=timezone.utc),
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _fetch_one(
        self, filename: str, client: httpx.AsyncClient
    ) -> bytes | FetchError:
        """Fetch a single artifact file from GitLab.

        Constructs the full URL per Requirement 1.2, issues the request, and
        handles HTTP errors per Requirements 1.4 and 1.6.

        Args:
            filename: One of ``manifest.json``, ``catalog.json``, or
                ``graph_summary.json``.
            client: Shared async HTTP client (carries auth headers and timeout).

        Returns:
            Raw bytes on success, or a :class:`~backend.exceptions.FetchError`
            on any non-200 response.  Never raises.
        """
        url = self._build_url(filename)

        try:
            response = await client.get(url)
        except httpx.TimeoutException as exc:
            logger.error(
                "Timeout fetching artifact %s from %s after %ss: %s",
                filename,
                url,
                _TIMEOUT_SECONDS,
                exc,
            )
            return FetchError(f"Timeout fetching {filename}: {exc}")
        except httpx.RequestError as exc:
            logger.error(
                "Network error fetching artifact %s from %s: %s",
                filename,
                url,
                exc,
            )
            return FetchError(f"Network error fetching {filename}: {exc}")

        # Requirement 1.4: 401/403 → log masked token info
        if response.status_code in (401, 403):
            masked = mask_secret(self._config.gitlab_token, "GITLAB_TOKEN")
            logger.error(
                "GitLab returned HTTP %d for artifact %s. "
                "Token (masked): %s. "
                "Check GITLAB_TOKEN secret scope and project permissions.",
                response.status_code,
                filename,
                masked,
            )
            return FetchError(
                f"HTTP {response.status_code} fetching {filename} — "
                "check GITLAB_TOKEN permissions."
            )

        # Requirement 1.6: any non-200 → log file, status, body[:500]
        if response.status_code != 200:
            body_preview = response.text[:500]
            logger.error(
                "Non-200 response fetching artifact %s: HTTP %d. "
                "Response body (truncated to 500 chars): %s",
                filename,
                response.status_code,
                body_preview,
            )
            return FetchError(
                f"HTTP {response.status_code} fetching {filename}."
            )

        # Success — log outcome per Requirement 12.4
        logger.info(
            "Fetched artifact %s: HTTP 200, %d bytes.",
            filename,
            len(response.content),
        )
        return response.content

    def _build_url(self, filename: str) -> str:
        """Construct the GitLab Artifacts API URL for *filename*.

        Pattern (Requirement 1.2):
            {base_url}/projects/{project_id}/jobs/artifacts/main/raw/public/{filename}?job=pages

        Args:
            filename: Artifact filename (e.g. ``manifest.json``).

        Returns:
            Fully constructed URL string.
        """
        base = self._config.gitlab_base_url.rstrip("/")
        project_id = self._config.gitlab_project_id
        return (
            f"{base}/projects/{project_id}/jobs/artifacts/main/raw/public"
            f"/{filename}?job=pages"
        )
