"""
Shared retrieval services and dataclasses that wrap domain helpers.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from backend.apps.indexing.tool import ChunkRecord, fetch_chunks_for_point_ids, list_active_indices
from backend.apps.pastpaper.tool import KeywordQuery, KeywordResult, search_components
from backend.apps.service.tasks import generate_embedding

from backend.apps.indexing.qdrant import get_client as get_qdrant_client


logger = logging.getLogger(__name__)

RESOURCE_TYPE_TO_PAPER_TYPE = {
    "mark_scheme": "ms",
    "markscheme": "ms",
    "marking_scheme": "ms",
    "marking scheme": "ms",
    "mark-scheme": "ms",
    "full_paper": "qp",
    "full paper": "qp",
    "question": "qp",
    "paper": "qp",
}


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class QueryBlueprint:
    """Normalized representation of user intent."""

    raw_query: str
    subject: Optional[str] = None
    syllabus_code: Optional[str] = None
    exam_board: Optional[str] = None
    resource_type: str = "question"
    year_range: Tuple[Optional[int], Optional[int]] = (None, None)
    keywords: Tuple[str, ...] = ()
    semantic_seed: str = ""
    provenance: Dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "raw_query": self.raw_query,
            "subject": self.subject,
            "syllabus_code": self.syllabus_code,
            "exam_board": self.exam_board,
            "resource_type": self.resource_type,
            "year_range": list(self.year_range),
            "keywords": list(self.keywords),
            "semantic_seed": self.semantic_seed,
            "provenance": self.provenance,
        }


@dataclass(slots=True)
class WorkspaceCandidate:
    candidate_id: str
    paper_uuid: str
    paper_code: str
    year: Optional[int]
    path: Optional[str]
    snippet: str
    score: float
    source: str
    subject: Optional[str] = None
    syllabus_code: Optional[str] = None
    exam_board: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_result(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "paper_uuid": self.paper_uuid,
            "paper_code": self.paper_code,
            "year": self.year,
            "path": self.path,
            "snippet": self.snippet,
            "score": self.score,
            "source": self.source,
            "subject": self.subject,
            "syllabus_code": self.syllabus_code,
            "exam_board": self.exam_board,
            "metadata": self.metadata,
        }


class SearchWorkspace:
    """In-memory workspace for the current retrieval interaction."""

    def __init__(self):
        self._candidates: dict[str, WorkspaceCandidate] = {}

    def add_candidates(self, candidates: Iterable[WorkspaceCandidate]) -> None:
        for candidate in candidates:
            existing = self._candidates.get(candidate.candidate_id)
            if existing is None or candidate.score > existing.score:
                self._candidates[candidate.candidate_id] = candidate

    def size(self) -> int:
        """Return the number of retained candidates."""
        return len(self._candidates)

    def summary(self) -> dict[str, Any]:
        if not self._candidates:
            return {"total": 0, "sources": {}}
        totals: dict[str, int] = defaultdict(int)
        for candidate in self._candidates.values():
            totals[candidate.source] += 1
        avg_score = sum(c.score for c in self._candidates.values()) / max(self.size(), 1)
        return {"total": len(self._candidates), "sources": dict(totals), "avg_score": round(avg_score, 4)}

    def topk(self, limit: int) -> list[WorkspaceCandidate]:
        ordered = sorted(self._candidates.values(), key=lambda c: c.score, reverse=True)
        return ordered[:limit]

    def snapshot(self, limit: int = 5) -> dict[str, Any]:
        """
        Lightweight diagnostic payload summarizing the workspace for LLM refinement steps.
        """
        limit = max(1, limit)
        top_candidates: list[dict[str, Any]] = []
        for candidate in self.topk(limit):
            snippet = candidate.snippet or ""
            if len(snippet) > 180:
                snippet = snippet[:177] + "…"
            top_candidates.append(
                {
                    "candidate_id": candidate.candidate_id,
                    "paper_code": candidate.paper_code,
                    "year": candidate.year,
                    "subject": candidate.subject,
                    "syllabus_code": candidate.syllabus_code,
                    "source": candidate.source,
                    "score": round(float(candidate.score), 4),
                    "path": candidate.path,
                    "snippet": snippet,
                }
            )
        return {"summary": self.summary(), "top_candidates": top_candidates}

    def clear(self) -> None:
        self._candidates.clear()


# ---------------------------------------------------------------------------
# Service layer
# ---------------------------------------------------------------------------


class RetrievalServices:
    """Facade that produces keyword + semantic candidates for the workspace."""

    def __init__(self):
        self._qdrant_client = None

    # Keyword ----------------------------------------------------------------

    async def keyword_search(
        self,
        blueprint: QueryBlueprint,
        *,
        limit: int = 25,
    ) -> list[WorkspaceCandidate]:
        if not blueprint.raw_query.strip():
            return []
        paper_type = self._paper_type_from_resource(blueprint.resource_type)
        query = KeywordQuery(
            query=blueprint.raw_query,
            keywords=blueprint.keywords,
            subject=blueprint.subject,
            syllabus_code=blueprint.syllabus_code,
            exam_board=blueprint.exam_board,
            paper_type=paper_type,
            year_from=blueprint.year_range[0],
            year_to=blueprint.year_range[1],
            limit=limit,
        )
        logger.debug(
            "keyword_search query=%s subject=%s syllabus=%s exam_board=%s limit=%s",
            query.query,
            query.subject,
            query.syllabus_code,
            query.exam_board,
            limit,
        )
        # Django ORM access must happen off the event loop.
        results: list[KeywordResult] = await asyncio.to_thread(search_components, query)
        candidates = [self._convert_keyword_result(res) for res in results]
        logger.info(
            "keyword_search.workspace subject=%s syllabus=%s exam_board=%s returned=%s limit=%s",
            blueprint.subject,
            blueprint.syllabus_code,
            blueprint.exam_board,
            len(candidates),
            limit,
        )
        return candidates

    # Semantic ----------------------------------------------------------------

    async def semantic_search(
        self,
        blueprint: QueryBlueprint,
        *,
        limit: int = 15,
        score_threshold: float | None = None,
    ) -> list[WorkspaceCandidate]:
        if not blueprint.semantic_seed.strip():
            return []

        paper_type = self._paper_type_from_resource(blueprint.resource_type)
        vector = await self.embed_text(blueprint.semantic_seed)
        indices = await asyncio.to_thread(
            list_active_indices,
            subject=blueprint.subject,
            exam_board=blueprint.exam_board,
            syllabus_code=blueprint.syllabus_code,
            paper_type=paper_type,
            year_from=blueprint.year_range[0],
            year_to=blueprint.year_range[1],
            limit=self._index_limit(limit),
        )
        if not indices:
            logger.info("semantic_search: no active indices found for blueprint %s", blueprint.as_dict())
            return []

        await asyncio.to_thread(self._ensure_qdrant_client)
        limit_per_index = max(1, limit // len(indices))

        hits_by_plan: dict[str, list[str]] = defaultdict(list)
        scores: dict[str, float] = {}

        for index in indices:
            try:
                hits = self._qdrant_client.search(
                    collection_name=index.qdrant_collection,
                    query_vector=vector,
                    limit=limit_per_index,
                    score_threshold=score_threshold,
                    with_payload=True,
                    with_vectors=False,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed semantic search on %s: %s", index.qdrant_collection, exc)
                continue

            for hit in hits:
                payload = getattr(hit, "payload", {}) or {}
                point_id = str(getattr(hit, "id", payload.get("point_id")))
                plan_id = str(payload.get("plan_id") or index.plan_id)
                candidate_id = f"chunk:{point_id}"
                hits_by_plan[plan_id].append(point_id)
                scores[candidate_id] = float(getattr(hit, "score", payload.get("score", 0.0)) or 0.0)

        candidates: list[WorkspaceCandidate] = []
        for plan_id, point_ids in hits_by_plan.items():
            chunks = await asyncio.to_thread(fetch_chunks_for_point_ids, plan_id, point_ids)
            for chunk in chunks:
                candidate_id = f"chunk:{chunk.qdrant_point_id or chunk.chunk_id}"
                score = scores.get(candidate_id, 0.0)
                candidates.append(self._convert_chunk_record(chunk, score))
        logger.info(
            "semantic_search.workspace subject=%s syllabus=%s exam_board=%s indices=%s hits=%s limit=%s",
            blueprint.subject,
            blueprint.syllabus_code,
            blueprint.exam_board,
            len(indices),
            len(candidates),
            limit,
        )
        return candidates

    # ------------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------------

    async def embed_text(self, text: str) -> list[float]:
        """Delegate to the shared embeddings actor."""
        sanitized = text.strip()
        if not sanitized:
            return []
        return await generate_embedding(sanitized)

    def _ensure_qdrant_client(self) -> None:
        if self._qdrant_client is None:
            self._qdrant_client = get_qdrant_client()

    @staticmethod
    def _convert_keyword_result(result: KeywordResult) -> WorkspaceCandidate:
        return WorkspaceCandidate(
            candidate_id=result.candidate_id,
            paper_uuid=result.paper_uuid,
            paper_code=result.paper_code,
            year=result.year,
            path=result.path,
            snippet=result.snippet,
            score=float(result.score),
            source=result.source,
            subject=result.subject,
            syllabus_code=result.syllabus_code,
            exam_board=result.exam_board,
            metadata={
                "component_id": result.component_id,
                "match_terms": list(result.match_terms),
            },
        )

    @staticmethod
    def _convert_chunk_record(record: ChunkRecord, score: float) -> WorkspaceCandidate:
        snippet = record.text.strip()
        if len(snippet) > 260:
            snippet = snippet[:257] + "…"
        return WorkspaceCandidate(
            candidate_id=f"chunk:{record.qdrant_point_id or record.chunk_id}",
            paper_uuid=record.paper_uuid,
            paper_code=record.paper_code,
            year=record.year,
            path=" / ".join(record.span_paths) if record.span_paths else None,
            snippet=snippet,
            score=float(score),
            source="qdrant_semantic",
            subject=record.subject,
            syllabus_code=record.syllabus_code,
            exam_board=record.exam_board,
            metadata={
                "chunk_id": record.chunk_id,
                "plan_id": str(record.plan_id),
                "bundle_sequence": record.bundle_sequence,
                "component_ids": list(record.component_ids),
            },
        )

    @staticmethod
    def _paper_type_from_resource(resource_type: str | None) -> str | None:
        if not resource_type:
            return None
        normalized = resource_type.strip().lower()
        return RESOURCE_TYPE_TO_PAPER_TYPE.get(normalized)

    @staticmethod
    def _index_limit(limit: int) -> int:
        safe_limit = max(limit, 1)
        return max(4, min(20, safe_limit))


# ---------------------------------------------------------------------------
# Runtime helper
# ---------------------------------------------------------------------------


class RetrievalRuntime:
    """
    Ensures the Dramatiq ResultOrchestrator is live when running LangGraph
    outside of ASGI (e.g., CLI tooling or standalone scripts).
    """

    _lock = asyncio.Lock()
    _started = False
    _orchestrator = None

    async def ensure_started(self):
        from backend.apps.service.orchestrators.registry import start_cleanup
        from backend.apps.service.orchestrators.service import ResultOrchestrator

        if RetrievalRuntime._started:
            return
        async with RetrievalRuntime._lock:
            if RetrievalRuntime._started:
                return
            start_cleanup()
            orchestrator = ResultOrchestrator()
            await orchestrator.start()
            RetrievalRuntime._orchestrator = orchestrator
            RetrievalRuntime._started = True
            logger.info("Retrieval runtime orchestrator started (pid=%s)", orchestrator.consumer_name)

    async def shutdown(self):
        from backend.apps.service.orchestrators.registry import stop_cleanup

        if not RetrievalRuntime._started:
            return
        async with RetrievalRuntime._lock:
            orchestrator = RetrievalRuntime._orchestrator
            RetrievalRuntime._orchestrator = None
            RetrievalRuntime._started = False

            if orchestrator:
                await orchestrator.stop()
            stop_cleanup()
            logger.info("Retrieval runtime orchestrator stopped")
