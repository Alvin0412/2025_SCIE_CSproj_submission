"""
Keyword and context search for the retrieval pipeline.
Provides high-recall, metadata-aware component selection for LLM-based retrieval.
"""

from __future__ import annotations

import json
import logging
import operator
import re
from dataclasses import dataclass, field
from functools import reduce
from typing import List, Optional, Sequence, Tuple

from django.db import connection
from django.db.models import Q, QuerySet, F, Value, FloatField, ExpressionWrapper, Case, When
from django.db.models.functions import Length, Lower, Abs
from django.contrib.postgres.search import TrigramSimilarity

from backend.apps.pastpaper.models import (
    PastPaper,
    PastPaperComponent,
    PastPaperMetadata,
)


# =====================================================
# Data Models
# =====================================================

@dataclass(frozen=True, slots=True)
class KeywordQuery:
    """Input parameters for component keyword search."""
    query: str
    keywords: Tuple[str, ...] = field(default_factory=tuple)
    subject: Optional[str] = None
    syllabus_code: Optional[str] = None
    exam_board: Optional[str] = None
    year_from: Optional[int] = None
    year_to: Optional[int] = None
    paper_type: Optional[str] = None
    limit: int = 25


@dataclass(frozen=True, slots=True)
class ComponentContext:
    """Lightweight structural context for a component result."""
    parent_id: Optional[int]
    parent_path: Optional[str]
    sibling_paths: Tuple[str, ...] = field(default_factory=tuple)
    child_paths: Tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class KeywordResult:
    """Normalized keyword search payload suitable for the retrieval workspace."""
    candidate_id: str
    component_id: int
    paper_uuid: str
    paper_version: int
    paper_code: str
    subject: str
    syllabus_code: str
    exam_board: str
    year: int
    path: str
    score: float
    snippet: str
    match_terms: Tuple[str, ...]
    source: str = "pastpaper_keyword"


STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "exam",
        "for",
        "in",
        "markscheme",
        "markschemes",
        "of",
        "on",
        "paper",
        "papers",
        "question",
        "questions",
        "scheme",
        "the",
        "to",
        "with",
    }
)

HIGH_SIGNAL_PATTERNS = (
    re.compile(r"^\d{3,5}[a-z]?$"),  # syllabus or paper code, e.g., 9708 or 9489w
    re.compile(r"^(?:qp|ms|paper|variant|p)\d{0,2}$"),  # paper type tokens
    re.compile(r"^\d{4}$"),  # exact year
)

MAX_KEYWORD_ANNOTATIONS = 6


# =====================================================
# Search
# =====================================================

def search_components(
    query: KeywordQuery,
    *,
    ignore_filters: bool = False,
    fuzzy: bool = False,
) -> List[KeywordResult]:
    """
    Metadata-aware keyword search tuned for LLM tool usage.

    This version:
      * separates high-signal tokens (codes, years) from descriptive keywords
      * enforces metadata scopes when provided, with automatic relaxation
      * falls back to pg_trgm fuzzy search when strict matching fails
      * emits structured logs so we can run retrieval ablations with real data
    """

    logger = logging.getLogger(__name__)
    supports_trigram = _supports_trigram()
    if fuzzy and not supports_trigram:
        logger.warning("Fuzzy keyword search requested but backend lacks pg_trgm; using strict mode instead.")
        fuzzy = False
    keyword_blob = " ".join(t for t in (query.keywords or ()) if t)
    normalized_terms = _normalize_terms(" ".join(part for part in [query.query, keyword_blob] if part))
    if not normalized_terms:
        logger.debug("search_components skipped empty query=%s", query)
        return []

    hard_terms, soft_terms = _split_terms(normalized_terms)
    term_clause = None if fuzzy else _build_text_clause(hard_terms, soft_terms)
    if term_clause is None and not fuzzy:
        logger.debug("search_components no usable terms query=%s keywords=%s", query.query, query.keywords)
        return []

    limit = max(1, query.limit)
    base_qs = (
        PastPaperComponent.objects.select_related("paper", "paper__metadata")
        .exclude(content__isnull=True)
        .exclude(content__exact="")
    )

    scoped_qs = base_qs
    if term_clause is not None:
        scoped_qs = scoped_qs.filter(term_clause)

    scope_q = None if ignore_filters else _build_metadata_scope(query)
    metadata_applied = bool(scope_q)
    metadata_relaxed = False
    soft_terms_relaxed = False
    hard_terms_relaxed = False
    if scope_q is not None:
        scoped_qs = scoped_qs.filter(scope_q)

    search_qs = _annotate_rank(scoped_qs, query, normalized_terms, fuzzy=fuzzy)
    raw_components = list(search_qs[: limit * 4])

    if not raw_components and hard_terms:
        soft_terms_relaxed = True
        relaxed_clause = _build_text_clause(hard_terms, tuple())
        relaxed_qs = base_qs
        if relaxed_clause is not None:
            relaxed_qs = relaxed_qs.filter(relaxed_clause)
        if scope_q is not None:
            relaxed_qs = relaxed_qs.filter(scope_q)
        search_qs = _annotate_rank(relaxed_qs, query, normalized_terms, fuzzy=False)
        raw_components = list(search_qs[: limit * 4])

    if not raw_components and soft_terms:
        hard_terms_relaxed = True
        relaxed_clause = _build_text_clause(tuple(), soft_terms)
        relaxed_qs = base_qs
        if relaxed_clause is not None:
            relaxed_qs = relaxed_qs.filter(relaxed_clause)
        if scope_q is not None:
            relaxed_qs = relaxed_qs.filter(scope_q)
        search_qs = _annotate_rank(relaxed_qs, query, normalized_terms, fuzzy=False)
        raw_components = list(search_qs[: limit * 4])

    if not raw_components and metadata_applied:
        metadata_relaxed = True
        scoped_qs = base_qs.filter(term_clause) if term_clause is not None else base_qs
        search_qs = _annotate_rank(scoped_qs, query, normalized_terms, fuzzy=fuzzy)
        raw_components = list(search_qs[: limit * 4])

    fuzzy_triggered = fuzzy
    if not raw_components and not fuzzy and supports_trigram:
        fuzzy_triggered = True
        relaxed_qs = base_qs
        if scope_q is not None and not metadata_relaxed:
            relaxed_qs = relaxed_qs.filter(scope_q)
        search_qs = _annotate_rank(relaxed_qs, query, normalized_terms, fuzzy=True)
        raw_components = list(search_qs[: limit * 4])
    elif not raw_components and not fuzzy:
        logger.debug("Skipping fuzzy fallback because backend does not support pg_trgm similarity.")

    results: List[KeywordResult] = []
    seen: set[int] = set()
    for component in raw_components:
        if component.id in seen:
            continue
        seen.add(component.id)
        paper = component.paper
        metadata = paper.metadata
        snippet = _build_snippet(component.content or "", normalized_terms)
        results.append(
            KeywordResult(
                candidate_id=f"component:{component.id}",
                component_id=component.id,
                paper_uuid=str(paper.paper_id),
                paper_version=paper.version_no,
                paper_code=metadata.paper_code,
                subject=metadata.subject,
                syllabus_code=metadata.syllabus_code,
                exam_board=metadata.exam_board,
                year=metadata.year,
                path=component.path_normalized or component.num_display,
                score=float(getattr(component, "rank_score", 0.0)),
                snippet=snippet,
                match_terms=tuple(normalized_terms),
            )
        )
        if len(results) >= limit:
            break

    payload = {
        "query": query.query,
        "keywords": list(query.keywords or ()),
        "terms": list(normalized_terms),
        "hard_terms": list(hard_terms),
        "soft_terms": list(soft_terms),
        "limit": limit,
        "returned": len(results),
        "metadata_applied": metadata_applied,
        "metadata_relaxed": metadata_relaxed,
        "soft_terms_relaxed": soft_terms_relaxed,
        "hard_terms_relaxed": hard_terms_relaxed,
        "fuzzy": fuzzy_triggered,
        "scope_fields": _active_scope_fields(query) if metadata_applied else [],
    }
    logger.info("keyword_search.complete %s", json.dumps(payload, ensure_ascii=False))

    return sorted(results, key=lambda r: r.score, reverse=True)


# =====================================================
# Context & Utilities
# =====================================================

def fetch_component_context(component: PastPaperComponent) -> ComponentContext:
    """Return lightweight structural hints for a component."""
    parent = component.parent
    parent_path = parent.path_normalized if parent else None

    sibling_paths: Tuple[str, ...] = tuple()
    if parent:
        siblings = (
            parent.children.exclude(pk=component.pk)
            .order_by("path_normalized")
            .values_list("path_normalized", flat=True)
        )
        sibling_paths = tuple(siblings)

    child_paths = tuple(
        component.children.order_by("path_normalized").values_list("path_normalized", flat=True)
    )

    return ComponentContext(
        parent_id=parent.id if parent else None,
        parent_path=parent_path,
        sibling_paths=sibling_paths,
        child_paths=child_paths,
    )


def _active_scope_fields(query: KeywordQuery) -> List[str]:
    fields: List[str] = []
    if query.subject:
        fields.append("subject")
    if query.syllabus_code:
        fields.append("syllabus_code")
    if query.exam_board:
        fields.append("exam_board")
    if query.paper_type:
        fields.append("paper_type")
    if query.year_from is not None or query.year_to is not None:
        fields.append("year_range")
    return fields


def _split_terms(terms: Tuple[str, ...]) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    hard: List[str] = []
    soft: List[str] = []
    for term in terms:
        if _is_high_signal(term):
            hard.append(term)
        else:
            soft.append(term)
    return tuple(hard), tuple(soft)


def _is_high_signal(term: str) -> bool:
    if not term:
        return False
    if term in STOPWORDS:
        return False
    if term.isdigit():
        value = int(term)
        if 1900 <= value <= 2100:
            return True
        return len(term) >= 3
    for pattern in HIGH_SIGNAL_PATTERNS:
        if pattern.match(term):
            return True
    return False


def _build_text_clause(hard_terms: Tuple[str, ...], soft_terms: Tuple[str, ...]) -> Optional[Q]:
    def combine_or(clauses: Sequence[Q]) -> Optional[Q]:
        clauses = [c for c in clauses if c is not None]
        if not clauses:
            return None
        return reduce(operator.or_, clauses)

    def combine_and(clauses: Sequence[Q]) -> Optional[Q]:
        clauses = [c for c in clauses if c is not None]
        if not clauses:
            return None
        return reduce(operator.and_, clauses)

    hard_clauses = [_term_clause(term) for term in hard_terms]
    soft_clauses = [_term_clause(term) for term in soft_terms]

    if hard_clauses:
        combined = combine_and(hard_clauses)
        soft_clause = combine_or(soft_clauses)
        if combined is not None and soft_clause is not None:
            return combined & soft_clause
        return combined
    return combine_or(soft_clauses)


def _term_clause(term: str) -> Q:
    clause = (
        Q(content__icontains=term)
        | Q(path_normalized__icontains=term)
        | Q(num_display__icontains=term)
    )
    if term.isdigit() and len(term) == 4:
        clause = clause | Q(paper__metadata__year=int(term))
    if re.match(r"^\d{3,5}", term):
        clause = clause | Q(paper__metadata__paper_code__icontains=term)
        clause = clause | Q(paper__metadata__syllabus_code__icontains=term)
    return clause


def _build_metadata_scope(query: KeywordQuery) -> Optional[Q]:
    clauses: List[Q] = []
    if query.syllabus_code:
        clauses.append(Q(paper__metadata__syllabus_code__iexact=query.syllabus_code))
    if query.exam_board:
        clauses.append(Q(paper__metadata__exam_board__iexact=query.exam_board))
    if query.subject:
        clauses.append(Q(paper__metadata__subject__icontains=query.subject))
    if query.paper_type:
        clauses.append(Q(paper__metadata__paper_type__iexact=query.paper_type))
    if query.year_from is not None:
        clauses.append(Q(paper__metadata__year__gte=query.year_from))
    if query.year_to is not None:
        clauses.append(Q(paper__metadata__year__lte=query.year_to))
    if not clauses:
        return None
    return reduce(operator.and_, clauses)


def _annotate_rank(
    qs: QuerySet[PastPaperComponent],
    query: KeywordQuery,
    terms: Tuple[str, ...],
    *,
    fuzzy: bool,
) -> QuerySet[PastPaperComponent]:
    if fuzzy:
        qs = _annotate_fuzzy_similarity(qs, " ".join(terms))
    else:
        qs = _annotate_keyword_similarity(qs, terms)
    qs = _annotate_metadata_bias(qs, query)
    qs = qs.annotate(
        depth_bonus=Case(
            When(depth__lte=1, then=Value(0.06)),
            When(depth__lte=3, then=Value(0.03)),
            default=Value(0.0),
            output_field=FloatField(),
        )
    )
    qs = qs.annotate(
        rank_score=ExpressionWrapper(
            (F("similarity") + F("depth_bonus")) * (1 + F("meta_bias")),
            output_field=FloatField(),
        )
    )
    return qs.order_by("-rank_score", "-paper__metadata__year", "depth", "id")


def _annotate_keyword_similarity(
    qs: QuerySet[PastPaperComponent],
    terms: Tuple[str, ...],
) -> QuerySet[PastPaperComponent]:
    if not terms:
        return qs.annotate(similarity=Value(0.2, output_field=FloatField()))

    annotations: dict[str, Case] = {}
    fields: List[str] = []
    for idx, term in enumerate(terms[:MAX_KEYWORD_ANNOTATIONS]):
        alias = f"kw_match_{idx}"
        annotations[alias] = Case(
            When(content__icontains=term, then=Value(1.0)),
            When(path_normalized__icontains=term, then=Value(0.8)),
            When(num_display__icontains=term, then=Value(0.6)),
            default=Value(0.0),
            output_field=FloatField(),
        )
        fields.append(alias)

    if annotations:
        qs = qs.annotate(**annotations)
        match_expr = reduce(
            operator.add,
            (F(alias) for alias in fields),
            Value(0.0, output_field=FloatField()),
        )
        denom = Value(float(len(fields)), output_field=FloatField())
        qs = qs.annotate(
            similarity=ExpressionWrapper(
                (Value(0.2) + match_expr / denom)
                / (1 + Length("content") / Value(600.0, output_field=FloatField())),
                output_field=FloatField(),
            )
        )
    else:
        qs = qs.annotate(similarity=Value(0.2, output_field=FloatField()))

    return qs


def _annotate_fuzzy_similarity(
    qs: QuerySet[PastPaperComponent],
    full_query: str,
) -> QuerySet[PastPaperComponent]:
    lowered = full_query.lower()
    qs = qs.annotate(
        similarity=ExpressionWrapper(
            (
                TrigramSimilarity(Lower("content"), lowered) * Value(0.7, output_field=FloatField())
                + TrigramSimilarity(Lower("path_normalized"), lowered) * Value(0.2, output_field=FloatField())
                + TrigramSimilarity(Lower("num_display"), lowered) * Value(0.1, output_field=FloatField())
            )
            / (1 + Length("content") / Value(400.0, output_field=FloatField())),
            output_field=FloatField(),
        )
    )
    return qs.filter(similarity__gt=0.02)


def _annotate_metadata_bias(
    qs: QuerySet[PastPaperComponent],
    query: KeywordQuery,
) -> QuerySet[PastPaperComponent]:
    bias_fields: List[str] = []

    def add_bias(alias: str, condition: Q, weight: float) -> None:
        nonlocal qs, bias_fields
        qs = qs.annotate(
            **{
                alias: Case(
                    When(condition, then=Value(weight, output_field=FloatField())),
                    default=Value(0.0, output_field=FloatField()),
                    output_field=FloatField(),
                )
            }
        )
        bias_fields.append(alias)

    if query.subject:
        add_bias("subject_bias", Q(paper__metadata__subject__icontains=query.subject), 0.35)
    if query.syllabus_code:
        add_bias("syllabus_bias", Q(paper__metadata__syllabus_code__iexact=query.syllabus_code), 0.5)
    if query.exam_board:
        add_bias("exam_board_bias", Q(paper__metadata__exam_board__iexact=query.exam_board), 0.25)
    if query.paper_type:
        add_bias("paper_type_bias", Q(paper__metadata__paper_type__iexact=query.paper_type), 0.2)

    target_year = _target_year(query)
    if target_year is not None:
        qs = qs.annotate(
            year_diff=Abs(F("paper__metadata__year") - Value(target_year)),
        )
        qs = qs.annotate(
            year_bias=Case(
                When(year_diff__lte=0, then=Value(0.45, output_field=FloatField())),
                When(year_diff__lte=1, then=Value(0.3, output_field=FloatField())),
                When(year_diff__lte=2, then=Value(0.2, output_field=FloatField())),
                default=Value(0.05, output_field=FloatField()),
                output_field=FloatField(),
            )
        )
        bias_fields.append("year_bias")

    if bias_fields:
        total = reduce(
            operator.add,
            (F(alias) for alias in bias_fields),
            Value(0.0, output_field=FloatField()),
        )
        qs = qs.annotate(
            meta_bias=ExpressionWrapper(total, output_field=FloatField()),
        )
    else:
        qs = qs.annotate(meta_bias=Value(0.0, output_field=FloatField()))

    return qs


def _target_year(query: KeywordQuery) -> Optional[int]:
    candidates = [year for year in (query.year_from, query.year_to) if year is not None]
    if not candidates:
        return None
    if len(candidates) == 2:
        start, end = sorted(candidates)
        return (start + end) // 2
    return candidates[0]


def _supports_trigram() -> bool:
    try:
        return connection.vendor == "postgresql"
    except Exception:  # pragma: no cover - defensive guard for unusual connection states
        return False


def _normalize_terms(raw: str) -> Tuple[str, ...]:
    tokens = [t.strip().lower() for t in re.split(r"[\s,;_]+", raw) if t.strip()]
    seen: set[str] = set()
    uniq: List[str] = []
    for t in tokens:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return tuple(uniq)


def _build_snippet(content: str, terms: Sequence[str], window: int = 200) -> str:
    lowered = (content or "").lower()
    for term in terms:
        idx = lowered.find(term)
        if idx != -1:
            start = max(0, idx - window // 2)
            end = min(len(content), idx + window // 2)
            snippet = content[start:end].strip()
            if start > 0:
                snippet = "…" + snippet
            if end < len(content):
                snippet = snippet + "…"
            return snippet
    return (content or "")[:window].strip() + ("…" if len(content or "") > window else "")
