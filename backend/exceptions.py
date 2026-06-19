"""Custom exception types for Prism backend components."""


class FetchError(Exception):
    """Raised when an artifact fetch from GitLab CI fails.

    This covers HTTP errors (non-200 responses), network timeouts,
    missing authentication tokens, and unauthorised (401/403) responses.
    """


class ParseError(Exception):
    """Raised when an artifact file cannot be parsed.

    Thrown by ManifestParser, CatalogParser, or GraphParser when a file
    contains invalid JSON or an unexpected top-level structure.
    """


class GenerationError(Exception):
    """Raised when the SQL_Generator fails to produce a valid SQLResult.

    Covers Claude API non-200 responses, network timeouts, invalid JSON
    responses, and missing/incorrectly-typed required fields in the response.
    """


class SecurityError(Exception):
    """Raised when the Query_Runner detects a prohibited DDL/DML statement.

    Triggered before any SQL is forwarded to the Databricks connector when
    the SQL contains any of: CREATE, INSERT, UPDATE, DELETE, DROP, ALTER,
    TRUNCATE, MERGE, REPLACE (case-insensitive, word-boundary matched).
    """
