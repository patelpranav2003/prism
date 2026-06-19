"""
tests/unit/test_artifact_url_construction.py

Property-based test for ArtifactFetcher URL construction.

Property 1: Artifact URL Construction
  For any GitLab base URL, project ID, and artifact filename, the
  ArtifactFetcher SHALL always produce a URL that exactly matches the pattern:
      {base_url}/projects/{project_id}/jobs/artifacts/main/raw/public/{filename}?job=pages
  with no deviation.

Validates: Requirements 1.2
"""

from hypothesis import given, settings
from hypothesis import strategies as st

from backend.config import AppConfig
from backend.discovery.gitlab_fetcher import ArtifactFetcher


# ---------------------------------------------------------------------------
# Helper — build a minimal AppConfig without env vars
# ---------------------------------------------------------------------------

def _make_config(base_url: str, project_id: str) -> AppConfig:
    """Construct an AppConfig directly (bypassing from_env) for testing."""
    return AppConfig(
        gitlab_base_url=base_url,
        gitlab_project_id=project_id,
        gitlab_token="test-token",
        databricks_sql_warehouse="wh-test",
        anthropic_api_key="sk-ant-test",
        admin_password_hash="$2b$12$testhash",
    )


# ---------------------------------------------------------------------------
# Strategies — constrained to produce valid URL-like inputs
# ---------------------------------------------------------------------------

# Base URLs: realistic http/https origins with optional path segments.
# We avoid trailing slashes here to test that _build_url strips them.
_base_url_strategy = st.one_of(
    # plain http / https origins
    st.builds(
        lambda host, port: f"https://{host}:{port}",
        host=st.from_regex(r"[a-z][a-z0-9\-]{1,20}\.[a-z]{2,6}", fullmatch=True),
        port=st.integers(min_value=1, max_value=65535),
    ),
    st.builds(
        lambda host: f"https://{host}",
        host=st.from_regex(r"[a-z][a-z0-9\-]{1,20}\.[a-z]{2,6}", fullmatch=True),
    ),
    # origins with a path prefix (common for self-hosted GitLab)
    st.builds(
        lambda host, prefix: f"https://{host}/{prefix}",
        host=st.from_regex(r"[a-z][a-z0-9\-]{1,20}\.[a-z]{2,6}", fullmatch=True),
        prefix=st.from_regex(r"[a-z][a-z0-9/_\-]{0,30}", fullmatch=True),
    ),
    # with trailing slash (to ensure _build_url strips it)
    st.builds(
        lambda host: f"https://{host}/",
        host=st.from_regex(r"[a-z][a-z0-9\-]{1,20}\.[a-z]{2,6}", fullmatch=True),
    ),
)

# Project IDs: integers (most common) or arbitrary non-empty strings
_project_id_strategy = st.one_of(
    st.integers(min_value=1, max_value=10_000_000).map(str),
    st.from_regex(r"[a-zA-Z0-9_\-]{1,40}", fullmatch=True),
)

# Filenames: the three real artifact filenames plus arbitrary safe names
_filename_strategy = st.one_of(
    st.sampled_from(["manifest.json", "catalog.json", "graph_summary.json"]),
    st.from_regex(r"[a-zA-Z0-9_\-]{1,30}\.[a-z]{2,5}", fullmatch=True),
)


# ---------------------------------------------------------------------------
# Property 1: Artifact URL Construction
# Validates: Requirements 1.2
# ---------------------------------------------------------------------------

@given(
    base_url=_base_url_strategy,
    project_id=_project_id_strategy,
    filename=_filename_strategy,
)
@settings(max_examples=100)
def test_property_1_artifact_url_construction(
    base_url: str, project_id: str, filename: str
) -> None:
    """**Property 1: Artifact URL Construction**

    For any GitLab base URL, project ID, and artifact filename, the
    ArtifactFetcher SHALL always produce a URL that exactly matches the
    pattern:
        {base_url}/projects/{project_id}/jobs/artifacts/main/raw/public/{filename}?job=pages
    with no deviation.

    **Validates: Requirements 1.2**
    """
    config = _make_config(base_url, project_id)
    fetcher = ArtifactFetcher(config)

    actual_url = fetcher._build_url(filename)

    # The base URL with any trailing slash stripped
    expected_base = base_url.rstrip("/")
    expected_url = (
        f"{expected_base}/projects/{project_id}"
        f"/jobs/artifacts/main/raw/public/{filename}?job=pages"
    )

    assert actual_url == expected_url, (
        f"URL mismatch for base_url={base_url!r}, "
        f"project_id={project_id!r}, filename={filename!r}.\n"
        f"  Expected: {expected_url!r}\n"
        f"  Got:      {actual_url!r}"
    )


# ---------------------------------------------------------------------------
# Unit tests — concrete examples to anchor the property
# ---------------------------------------------------------------------------

class TestArtifactUrlConstructionExamples:
    """Concrete spot-checks complementing the property test above."""

    def _fetcher(self, base_url: str, project_id: str) -> ArtifactFetcher:
        return ArtifactFetcher(_make_config(base_url, project_id))

    def test_standard_cloud_gitlab(self):
        fetcher = self._fetcher("https://gitlab.com/api/v4", "12345")
        url = fetcher._build_url("manifest.json")
        assert url == (
            "https://gitlab.com/api/v4/projects/12345"
            "/jobs/artifacts/main/raw/public/manifest.json?job=pages"
        )

    def test_self_hosted_gitlab_with_path_prefix(self):
        fetcher = self._fetcher("https://git.example.com/api/v4", "99")
        url = fetcher._build_url("catalog.json")
        assert url == (
            "https://git.example.com/api/v4/projects/99"
            "/jobs/artifacts/main/raw/public/catalog.json?job=pages"
        )

    def test_trailing_slash_on_base_url_is_stripped(self):
        fetcher = self._fetcher("https://gitlab.com/api/v4/", "42")
        url = fetcher._build_url("graph_summary.json")
        # Must not produce a double-slash
        assert "//" not in url.split("://", 1)[1], (
            f"Double-slash found in URL path: {url!r}"
        )
        assert url == (
            "https://gitlab.com/api/v4/projects/42"
            "/jobs/artifacts/main/raw/public/graph_summary.json?job=pages"
        )

    def test_all_three_artifact_filenames(self):
        fetcher = self._fetcher("https://gitlab.example.com", "7")
        for filename in ("manifest.json", "catalog.json", "graph_summary.json"):
            url = fetcher._build_url(filename)
            assert url.endswith(f"/{filename}?job=pages"), (
                f"Unexpected URL tail for {filename!r}: {url!r}"
            )
            assert "/jobs/artifacts/main/raw/public/" in url

    def test_query_string_is_exactly_job_equals_pages(self):
        fetcher = self._fetcher("https://gitlab.com", "1")
        url = fetcher._build_url("manifest.json")
        assert url.endswith("?job=pages"), (
            f"Expected URL to end with '?job=pages', got: {url!r}"
        )
        # Only one query parameter; no extra & appended
        assert url.count("?") == 1

    def test_projects_segment_is_present(self):
        fetcher = self._fetcher("https://gitlab.com", "5678")
        url = fetcher._build_url("manifest.json")
        assert "/projects/5678/" in url
