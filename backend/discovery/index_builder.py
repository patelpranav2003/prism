"""IndexBuilder — orchestrates artifact parsing into a SchemaIndex.

Orchestration pipeline:
  1. ManifestParser.parse()        → list[ModelMeta]  (may raise ParseError)
  2. CatalogParser.merge()         → list[ModelMeta]  (may raise ParseError)
  3. GraphParser.parse()           → dict[str, LineageNode]  (may raise ParseError)
  4. _infer_layer()                applied to every model (mutates a copy)
  5. _infer_grain()                applied to every model whose grain is empty

On a ParseError from *any* of the three parsers:
  - Log the file name and the parse error message.
  - Return the *previous* SchemaIndex unchanged (if one was provided).
  - If no previous index exists the caller is responsible for marking the
    cache status as "unavailable".

The ``IndexBuilder`` intentionally does **not** generate embeddings.  The
``embeddings`` field of the returned ``SchemaIndex`` is an empty numpy array
(shape ``(0,)``).  The Embedder component is responsible for filling that in
after ``build()`` returns.

Requirement references:
  - 3.1  Build SchemaIndex within 30 s for ≤500 models
  - 3.3  Layer inference priority: tag → folder path → default "bronze"
  - 3.6  Grain inference when meta.grain absent
  - 3.7  Missing fields → zero value; never skip a model
  - 3.8  Parse failure → preserve previous index; no prior index → "unavailable"
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

import numpy as np

from backend.exceptions import ParseError
from backend.models import ArtifactBundle, ModelMeta, SchemaIndex
from backend.discovery.manifest_parser import ManifestParser
from backend.discovery.catalog_parser import CatalogParser
from backend.discovery.graph_parser import GraphParser

logger = logging.getLogger(__name__)

# Medallion layer keywords (lowercase for case-insensitive matching).
_LAYER_KEYWORDS = ("gold", "silver", "bronze")

# Regex that matches the _by_<dimension> suffix pattern in a model name.
# Anchored at the end so "sales_by_region" matches but "by_product_sales" does not.
_BY_SUFFIX_RE = re.compile(r"_by_[a-zA-Z0-9_]+$")


def _infer_layer(model: ModelMeta) -> str:
    """Infer the medallion layer for *model* (Requirement 3.3).

    Priority order:
      1. Any tag contains "gold", "silver", or "bronze" (case-insensitive).
         The first matching tag wins; "gold" > "silver" > "bronze" in the event
         of multiple layer tags — matching order follows ``_LAYER_KEYWORDS``.
      2. Any segment of ``folder_path`` contains one of those keywords.
      3. Default → ``"bronze"``.

    Returns
    -------
    str
        One of ``"gold"``, ``"silver"``, or ``"bronze"``.
    """
    # --- Priority 1: tag match ---
    for keyword in _LAYER_KEYWORDS:
        for tag in model.tags:
            if keyword in tag.lower():
                return keyword

    # --- Priority 2: folder path match ---
    # Split on both forward- and back-slashes so Windows and POSIX paths work.
    segments = re.split(r"[/\\]", model.folder_path)
    for keyword in _LAYER_KEYWORDS:
        for segment in segments:
            if keyword in segment.lower():
                return keyword

    # --- Priority 3: default ---
    return "bronze"


def _infer_grain(model: ModelMeta) -> str:
    """Infer the grain for *model* when ``meta.grain`` is absent (Requirement 3.6).

    Scans the compiled SQL of ``compiled_sql_excerpt``:

    Priority order:
      1. GROUP BY clause present → return the GROUP BY columns as a
         comma-separated string (trimmed, lowercased).
      2. DISTINCT keyword present → return ``"distinct"``.
      3. Model name suffix matches ``_by_{dimension}`` → return the dimension.
      4. None of the above → return ``"unknown"``.

    Returns
    -------
    str
        The inferred grain string, or ``"unknown"`` when nothing matches.
    """
    sql = model.compiled_sql_excerpt[:500]  # only need the header for grain inference

    # --- Priority 1: GROUP BY ---
    # Match "GROUP BY <columns>" — capture everything up to the next SQL clause
    # keyword or end of the excerpt.  Case-insensitive.
    group_by_match = re.search(
        r"\bGROUP\s+BY\s+(.+?)(?:\bHAVING\b|\bORDER\b|\bLIMIT\b|\bUNION\b|$)",
        sql,
        re.IGNORECASE | re.DOTALL,
    )
    if group_by_match:
        cols = group_by_match.group(1).strip()
        # Collapse whitespace and return the raw columns string.
        cols = re.sub(r"\s+", " ", cols).strip()
        if cols:
            return cols

    # --- Priority 2: DISTINCT ---
    if re.search(r"\bDISTINCT\b", sql, re.IGNORECASE):
        return "distinct"

    # --- Priority 3: _by_{dimension} suffix ---
    by_match = _BY_SUFFIX_RE.search(model.name)
    if by_match:
        # Extract the dimension part (everything after "_by_").
        suffix = by_match.group(0)          # e.g. "_by_day"
        dimension = suffix[len("_by_"):]     # e.g. "day"
        return dimension

    return "unknown"


class IndexBuilder:
    """Orchestrates artifact parsing and constructs a :class:`SchemaIndex`.

    Usage::

        builder = IndexBuilder()
        index = builder.build(bundle, previous_index=cache.index)

    The returned ``SchemaIndex`` has an empty ``embeddings`` array — the
    :class:`~backend.search.embedder.Embedder` fills those in after this call.
    """

    def __init__(self) -> None:
        self._manifest_parser = ManifestParser()
        self._catalog_parser = CatalogParser()
        self._graph_parser = GraphParser()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build(
        self,
        bundle: ArtifactBundle,
        previous_index: SchemaIndex | None = None,
    ) -> SchemaIndex | None:
        """Build a :class:`SchemaIndex` from *bundle*.

        Parameters
        ----------
        bundle:
            Raw artifact bytes fetched from GitLab CI.
        previous_index:
            The last valid :class:`SchemaIndex`, used as a fallback when any
            artifact file fails to parse.  Pass ``None`` if no prior index
            exists.

        Returns
        -------
        SchemaIndex | None
            A fresh :class:`SchemaIndex` on success.
            The ``previous_index`` (possibly ``None``) on any parse failure,
            preserving the caller's existing state.

        Notes
        -----
        *Embeddings are not generated here.*  The returned index has
        ``embeddings = np.empty((0,), dtype=np.float32)`` — the Embedder
        component populates the real array after this method returns.
        """

        # --- Step 1: ManifestParser ---
        try:
            models = self._manifest_parser.parse(bundle.manifest)
        except ParseError as exc:
            logger.error(
                "IndexBuilder: failed to parse manifest.json — %s; "
                "preserving previous SchemaIndex",
                exc,
            )
            return previous_index

        # --- Step 2: CatalogParser (merge) ---
        try:
            models = self._catalog_parser.merge(models, bundle.catalog)
        except ParseError as exc:
            logger.error(
                "IndexBuilder: failed to parse catalog.json — %s; "
                "preserving previous SchemaIndex",
                exc,
            )
            return previous_index

        # --- Step 3: GraphParser ---
        try:
            lineage = self._graph_parser.parse(bundle.graph)
        except ParseError as exc:
            logger.error(
                "IndexBuilder: failed to parse graph_summary.json — %s; "
                "preserving previous SchemaIndex",
                exc,
            )
            return previous_index

        # --- Step 4: Layer inference ---
        # _infer_layer() is pure and never raises; iterate all models.
        enriched: list[ModelMeta] = []
        for model in models:
            layer = _infer_layer(model)
            # Re-assign layer: ModelMeta is a plain dataclass (not frozen).
            model.layer = layer  # type: ignore[assignment]
            enriched.append(model)

        # --- Step 5: Grain inference ---
        # Only infer when manifest didn't supply a grain (empty string).
        for model in enriched:
            if not model.grain:
                model.grain = _infer_grain(model)

        # --- Assemble SchemaIndex ---
        # Embeddings are intentionally left empty — the Embedder fills them.
        empty_embeddings = np.empty((0,), dtype=np.float32)

        index = SchemaIndex(
            models=enriched,
            embeddings=empty_embeddings,
            lineage=lineage,
            built_at=datetime.now(tz=timezone.utc),
            model_count=len(enriched),
        )

        logger.info(
            "IndexBuilder: built SchemaIndex with %d model(s)",
            index.model_count,
        )
        return index
