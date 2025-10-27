"""
Service helpers that expose chunk plan metadata for the retrieval layer.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence
from uuid import UUID

from backend.apps.indexing.models import (
    Bundle,
    Chunk,
    ChunkPlan,
    ChunkPlanStatus,
    IndexProfile,
)

logger = logging.getLogger(__name__)

DEFAULT_INDEX_LIMIT = 40


@dataclass(frozen=True, slots=True)
class SemanticIndex:
    """Description of an embedded chunk plan backed by a Qdrant collection."""

    plan_id: UUID
    profile_slug: str
    qdrant_collection: str
    vector_dimension: int
    paper_uuid: str
    paper_version: int
    active: bool
    subject: str
    exam_board: str
    syllabus_code: str
    year: int


@dataclass(frozen=True, slots=True)
class ChunkRecord:
    """Hydrated chunk metadata used when turning Qdrant hits into workspace candidates."""

    chunk_id: int
    plan_id: UUID
    qdrant_point_id: str
    sequence: int
    text: str
    token_count: int
    paper_uuid: str
    paper_version: int
    paper_code: str
    subject: str
    exam_board: str
    syllabus_code: str
    year: int
    bundle_sequence: int
    span_paths: tuple[str, ...]
    component_ids: tuple[int, ...]


def list_active_indices(
    *,
    subject: Optional[str] = None,
    exam_board: Optional[str] = None,
    syllabus_code: Optional[str] = None,
    paper_type: Optional[str] = None,
    year_from: Optional[int] = None,
    year_to: Optional[int] = None,
    limit: Optional[int] = None,
) -> List[SemanticIndex]:
    """
    Return embedded chunk plans that are ready for semantic retrieval.

    Filters are optional and allow the caller to scope indices by curriculum metadata.
    If no filters are provided we cap the list to a small window so semantic search does not
    spray every Qdrant collection.
    """

    def _base_queryset(include_inactive: bool):
        qs = ChunkPlan.objects.filter(status=ChunkPlanStatus.EMBEDDED, profile__is_active=True)
        if not include_inactive:
            qs = qs.filter(is_active=True, paper__is_active=True)
        return qs.select_related("profile", "paper", "paper__metadata").order_by(
            "-paper__metadata__year", "-embedded_at", "paper__metadata__subject"
        )

    def _apply_metadata_filters(qs):
        if subject:
            qs = qs.filter(paper__metadata__subject__iexact=subject)
        if exam_board:
            qs = qs.filter(paper__metadata__exam_board__iexact=exam_board)
        if syllabus_code:
            qs = qs.filter(paper__metadata__syllabus_code__iexact=syllabus_code)
        if paper_type:
            qs = qs.filter(paper__metadata__paper_type__iexact=paper_type)
        if year_from is not None:
            qs = qs.filter(paper__metadata__year__gte=year_from)
        if year_to is not None:
            qs = qs.filter(paper__metadata__year__lte=year_to)
        return qs

    effective_limit = limit
    if effective_limit is None and not any([subject, exam_board, syllabus_code, paper_type, year_from, year_to]):
        effective_limit = DEFAULT_INDEX_LIMIT
    slice_size = max(1, effective_limit) if effective_limit else None

    plans_qs = _apply_metadata_filters(_base_queryset(include_inactive=False))
    if slice_size:
        plans_qs = plans_qs[:slice_size]
    plans = list(plans_qs)
    active_relaxed = False

    if not plans:
        fallback_qs = _apply_metadata_filters(_base_queryset(include_inactive=True))
        if slice_size:
            fallback_qs = fallback_qs[:slice_size]
        plans = list(fallback_qs)
        active_relaxed = True

    indices: List[SemanticIndex] = []
    for plan in plans:
        profile: IndexProfile = plan.profile
        paper = plan.paper
        metadata = paper.metadata
        indices.append(
            SemanticIndex(
                plan_id=plan.plan_id,
                profile_slug=profile.slug,
                qdrant_collection=profile.qdrant_collection,
                vector_dimension=profile.dimension,
                paper_uuid=str(paper.paper_id),
                paper_version=paper.version_no,
                active=plan.is_active,
                subject=metadata.subject,
                exam_board=metadata.exam_board,
                syllabus_code=metadata.syllabus_code,
                year=metadata.year,
            )
        )

    payload = {
        "subject": subject,
        "exam_board": exam_board,
        "syllabus_code": syllabus_code,
        "paper_type": paper_type,
        "year_from": year_from,
        "year_to": year_to,
        "limit": effective_limit,
        "returned": len(indices),
        "active_relaxed": active_relaxed,
    }
    logger.info("semantic_indices.selected %s", json.dumps(payload, ensure_ascii=False))
    return indices


def fetch_chunks_for_point_ids(plan_id: UUID | str, point_ids: Sequence[str]) -> List[ChunkRecord]:
    """
    Hydrate a list of chunks using their stored Qdrant point identifiers.
    """
    if not point_ids:
        return []

    normalized_plan = _normalize_plan_id(plan_id)
    unique_point_ids = list(dict.fromkeys(point_ids))
    chunks = (
        Chunk.objects.filter(plan__plan_id=normalized_plan, qdrant_point_id__in=unique_point_ids)
        .select_related(
            "plan",
            "plan__paper",
            "plan__paper__metadata",
            "bundle",
        )
        .order_by("sequence")
    )
    records = _render_chunk_records(chunks)
    payload = {
        "plan_id": str(normalized_plan),
        "requested": len(point_ids),
        "unique_requested": len(unique_point_ids),
        "returned": len(records),
    }
    logger.info("semantic_chunks.hydrate_by_point %s", json.dumps(payload, ensure_ascii=False))
    return records


def fetch_chunks_by_ids(chunk_ids: Sequence[int]) -> List[ChunkRecord]:
    """Hydrate chunk records by database primary key."""
    if not chunk_ids:
        return []

    unique_ids = list(dict.fromkeys(chunk_ids))
    chunks = (
        Chunk.objects.filter(pk__in=unique_ids)
        .select_related(
            "plan",
            "plan__paper",
            "plan__paper__metadata",
            "bundle",
        )
        .order_by("sequence")
    )
    records = _render_chunk_records(chunks)
    payload = {
        "chunk_ids": len(chunk_ids),
        "unique_ids": len(unique_ids),
        "returned": len(records),
    }
    logger.info("semantic_chunks.hydrate_by_id %s", json.dumps(payload, ensure_ascii=False))
    return records


def _render_chunk_records(chunks: Iterable[Chunk]) -> List[ChunkRecord]:
    records: List[ChunkRecord] = []
    for chunk in chunks:
        plan: ChunkPlan = chunk.plan
        bundle: Bundle = chunk.bundle
        metadata = plan.paper.metadata
        records.append(
            ChunkRecord(
                chunk_id=chunk.id,
                plan_id=plan.plan_id,
                qdrant_point_id=chunk.qdrant_point_id or "",
                sequence=chunk.sequence,
                text=chunk.text,
                token_count=chunk.token_count,
                paper_uuid=str(plan.paper.paper_id),
                paper_version=plan.paper.version_no,
                paper_code=metadata.paper_code,
                subject=metadata.subject,
                exam_board=metadata.exam_board,
                syllabus_code=metadata.syllabus_code,
                year=metadata.year,
                bundle_sequence=bundle.sequence,
                span_paths=tuple(bundle.span_paths or []),
                component_ids=tuple(bundle.component_ids or []),
            )
        )
    return records


def _normalize_plan_id(plan_id: UUID | str) -> UUID:
    if isinstance(plan_id, UUID):
        return plan_id
    return UUID(str(plan_id))
