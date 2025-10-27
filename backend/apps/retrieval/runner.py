"""
LangGraph-inspired retrieval runner that orchestrates safeguard checks,
intent parsing, multi-index retrieval, and reranking.
"""

from __future__ import annotations

import logging
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Dict, Optional

from django.conf import settings

from backend.apps.accounts.concurrency import ConcurrencyLimitError, search_concurrency_guard
from backend.apps.retrieval.agent import BlueprintRevision, RetrievalAgent
from backend.apps.retrieval.llm_client import LLMClientError
from backend.apps.retrieval.services import (
    QueryBlueprint,
    RetrievalRuntime,
    RetrievalServices,
    SearchWorkspace,
    WorkspaceCandidate,
)
from backend.apps.service.realtime.publisher import ProgressPublisher


logger = logging.getLogger(__name__)


BLOCKED_KEYWORDS = {"jailbreak", "hack", "exploit", "bomb", "weapon"}
SUBJECT_HINTS = {
    "physics": "Physics",
    "chemistry": "Chemistry",
    "biology": "Biology",
    "math": "Mathematics",
    "mathematics": "Mathematics",
    "english": "English",
    "history": "History",
    "geography": "Geography",
    "economics": "Economics",
}
RESOURCE_HINTS = {
    "mark scheme": "mark_scheme",
    "markscheme": "mark_scheme",
    "paper": "full_paper",
    "question": "question",
}


@dataclass(slots=True)
class SafeguardVerdict:
    allowed: bool
    action: str = "allow"  # allow | clarify | reject
    reason: str = ""


class RetrievalRunner:
    """High-level coordinator for the LangGraph pipeline."""

    def __init__(
        self,
        *,
        services: Optional[RetrievalServices] = None,
    ):
        self.services = services or RetrievalServices()
        self.runtime = RetrievalRuntime()
        self.use_llm_intent = bool(getattr(settings, "RETRIEVAL_USE_LLM_INTENT", False))
        self.use_llm_rerank = bool(getattr(settings, "RETRIEVAL_USE_LLM_RERANK", False))
        self.use_llm_refiner = bool(getattr(settings, "RETRIEVAL_USE_LLM_REFINER", False))
        self.default_round_limit = max(1, int(getattr(settings, "RETRIEVAL_AGENT_MAX_ROUNDS", 1)))
        if self.use_llm_intent or self.use_llm_rerank or self.use_llm_refiner:
            self.agent = RetrievalAgent()
        else:
            self.agent = None

    async def run(
        self,
        *,
        rid: str,
        query: str,
        user_id: Optional[int] = None,
        options: Optional[dict[str, Any]] = None,
    ):
        await self.runtime.ensure_started()
        pub = ProgressPublisher(rid, topic="retrieval")
        workspace = SearchWorkspace()
        opts = options or {}
        conversation = opts.get("conversation")
        logger.info(
            "Retrieval run invoked rid=%s user_id=%s opts=%s",
            rid,
            user_id,
            {k: v for k, v in opts.items() if k not in {"conversation"}},
        )
        guard = search_concurrency_guard(user_id) if user_id else _noop_guard()
        try:
            async with guard:
                await pub.started("Retrieval run started", data={"event": "started", "rid": rid})

                verdict = self._safeguard(query)
                if not verdict.allowed:
                    await pub.error(
                        "Request rejected",
                        data={"event": "safeguard", "reason": verdict.reason, "action": verdict.action},
                    )
                    return

                intent_resolution = await self._resolve_intent(
                    query,
                    pub=pub,
                    conversation=conversation if isinstance(conversation, list) else None,
                )
                if not intent_resolution:
                    return
                blueprint, intent_meta = intent_resolution
                await pub.message(
                    "Intent parsed",
                    data={
                        "event": "intent",
                        "blueprint": blueprint.as_dict(),
                        "provenance": intent_meta,
                    },
                )

                max_rounds = self._resolve_round_limit(opts)
                round_index = 0
                last_round_stats: dict[str, Any] | None = None
                while round_index < max_rounds:
                    round_index += 1
                    last_round_stats = await self._run_retrieval_round(
                        round_index=round_index,
                        blueprint=blueprint,
                        workspace=workspace,
                        pub=pub,
                        opts=opts,
                    )
                    if not self._should_attempt_refinement(round_index, max_rounds):
                        break
                    revision = await self._maybe_refine_blueprint(
                        blueprint=blueprint,
                        workspace=workspace,
                        pub=pub,
                        round_index=round_index,
                        round_stats=last_round_stats,
                    )
                    if not revision:
                        break
                    blueprint = revision.blueprint
                    if revision.action == "stop":
                        break

                reranked, rerank_meta = await self._select_results(
                    workspace,
                    blueprint,
                    limit=int(opts.get("limit", 10)),
                    pub=pub,
                )
                await pub.finished(
                    "Retrieval complete",
                    data={
                        "event": "complete",
                        "results": [item for item in reranked],
                        "workspace_summary": workspace.summary(),
                        "rounds_completed": round_index or 1,
                        "rerank_provenance": rerank_meta,
                    },
                )
        except ConcurrencyLimitError:
            await pub.error(
                "Concurrent limit reached",
                data={"event": "concurrency_limit", "detail": "Active AI search limit reached"},
            )
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("Retrieval run failed: %s", exc)
            await pub.error("Retrieval failed", data={"event": "error", "error": str(exc)})
        finally:
            workspace.clear()

    def _safeguard(self, query: str) -> SafeguardVerdict:
        lowered = query.lower()
        for keyword in BLOCKED_KEYWORDS:
            if keyword in lowered:
                return SafeguardVerdict(
                    allowed=False,
                    action="reject",
                    reason=f"contains blocked keyword '{keyword}'",
                )
        if len(query.strip()) < 8:
            return SafeguardVerdict(
                allowed=False,
                action="clarify",
                reason="query too short; please provide more context",
            )
        return SafeguardVerdict(allowed=True)

    async def _resolve_intent(
        self,
        query: str,
        *,
        pub: ProgressPublisher,
        conversation: Optional[list[dict[str, str]]] = None,
    ) -> tuple[QueryBlueprint, dict[str, Any]] | None:
        fallback = self._heuristic_blueprint(query)
        fallback_meta = {**fallback.provenance, "provider": "heuristic", "stage": "intent"}
        if not (self.use_llm_intent and self.agent):
            return fallback, fallback_meta
        try:
            logger.info(
                "Invoking LLM intent parser rid=%s conversation_turns=%s",
                self.runtime,
                len(conversation or []),
            )
            intent = await self.agent.parse_intent(query, history=conversation or [])
        except LLMClientError as exc:
            logger.warning("LLM intent parsing failed, falling back to heuristics: %s", exc)
            await pub.message(
                "Intent fallback applied",
                data={"event": "intent_fallback", "reason": str(exc)},
            )
            return fallback, {**fallback_meta, "error": str(exc)}
        provenance = {**intent.provenance, "provider": "llm", "stage": "intent"}
        if intent.action == "reject":
            await pub.error(
                "Request rejected",
                data={
                    "event": "safeguard",
                    "reason": intent.reason or "Policy violation detected",
                    "action": "reject",
                    "provenance": provenance,
                },
            )
            return None
        if intent.needs_clarification:
            await pub.message(
                "Clarification required",
                data={
                    "event": "clarify",
                    "prompt": intent.clarification_prompt or intent.reason or "Please provide more detail.",
                    "provenance": provenance,
                },
            )
            return None
        blueprint = intent.blueprint or fallback
        provenance_payload = {**(blueprint.provenance or {}), **provenance}
        if blueprint is fallback and intent.blueprint is None:
            provenance_payload["fallback"] = "heuristic_blueprint"
        blueprint.provenance = provenance_payload
        return blueprint, blueprint.provenance

    def _heuristic_blueprint(self, query: str) -> QueryBlueprint:
        lowered = query.lower()
        subject = self._guess_subject(lowered)
        resource_type = self._guess_resource_type(lowered)
        year_range = self._extract_year_range(lowered)
        keywords = tuple(token for token in re.split(r"[^\w]+", lowered) if len(token) > 3)[:6]

        return QueryBlueprint(
            raw_query=query,
            subject=subject,
            resource_type=resource_type,
            year_range=year_range,
            keywords=keywords,
            semantic_seed=query.strip(),
            provenance={"intent_parser": "rule_based_v1", "provider": "heuristic", "stage": "intent"},
        )

    def _guess_subject(self, lowered: str) -> Optional[str]:
        for token, subject in SUBJECT_HINTS.items():
            if token in lowered:
                return subject
        return None

    def _guess_resource_type(self, lowered: str) -> str:
        for token, resource in RESOURCE_HINTS.items():
            if token in lowered:
                return resource
        return "question"

    def _extract_year_range(self, lowered: str) -> tuple[Optional[int], Optional[int]]:
        years = [int(match) for match in re.findall(r"(20\d{2}|19\d{2})", lowered)]
        if not years:
            return (None, None)
        years.sort()
        return (years[0], years[-1])

    async def _select_results(
        self,
        workspace: SearchWorkspace,
        blueprint: QueryBlueprint,
        *,
        limit: int,
        pub: ProgressPublisher,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        fallback_meta = {"provider": "heuristic", "stage": "rerank", "version": "rule_based_v1"}
        if not (self.use_llm_rerank and self.agent):
            return self._heuristic_rerank(workspace, limit=limit), fallback_meta
        max_candidates = max(limit * 2, getattr(settings, "RETRIEVAL_LLM_MAX_CANDIDATES", 12))
        candidates = workspace.topk(max_candidates)
        try:
            logger.info(
                "Invoking LLM rerank rid=%s candidate_count=%s",
                self.runtime,
                len(candidates),
            )
            rerank_response = await self.agent.rerank(blueprint, candidates)
        except LLMClientError as exc:
            logger.warning("LLM rerank failed, using heuristic ordering: %s", exc)
            await pub.message("LLM rerank fallback", data={"event": "rerank_fallback", "reason": str(exc)})
            return self._heuristic_rerank(workspace, limit=limit), {**fallback_meta, "error": str(exc)}
        if not rerank_response.decisions:
            await pub.message(
                "LLM rerank empty, using heuristics",
                data={"event": "rerank_fallback", "reason": "empty_decisions"},
            )
            return self._heuristic_rerank(workspace, limit=limit), {**fallback_meta, "error": "empty_decisions"}
        candidate_map = {candidate.candidate_id: candidate for candidate in candidates}
        ordered: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for decision in rerank_response.decisions:
            candidate = candidate_map.get(decision.candidate_id)
            if not candidate or candidate.candidate_id in seen_ids:
                continue
            seen_ids.add(candidate.candidate_id)
            payload = {
                **candidate.to_result(),
                "reason": decision.reason,
                "rerank_score": decision.score,
            }
            ordered.append(payload)
            if len(ordered) >= limit:
                break
        if len(ordered) < limit:
            for fallback in self._heuristic_rerank(workspace, limit=limit):
                cid = fallback.get("candidate_id")
                if cid and cid in seen_ids:
                    continue
                ordered.append(fallback)
                if cid:
                    seen_ids.add(cid)
                if len(ordered) >= limit:
                    break
        provenance = {**rerank_response.provenance, "provider": "llm", "stage": "rerank"}
        return ordered[:limit], provenance

    def _resolve_round_limit(self, opts: dict[str, Any]) -> int:
        fallback = self.default_round_limit
        try:
            requested = int(opts.get("rounds", fallback))
        except (TypeError, ValueError):
            requested = fallback
        return max(1, requested)

    def _can_refine_blueprint(self) -> bool:
        return bool(self.use_llm_refiner and self.agent)

    def _should_attempt_refinement(self, round_index: int, max_rounds: int) -> bool:
        if round_index >= max_rounds:
            return False
        return self._can_refine_blueprint()

    async def _run_retrieval_round(
        self,
        *,
        round_index: int,
        blueprint: QueryBlueprint,
        workspace: SearchWorkspace,
        pub: ProgressPublisher,
        opts: dict[str, Any],
    ) -> dict[str, Any]:
        logger.info(
            "Starting retrieval round %s rid=%s subject=%s syllabus=%s board=%s",
            round_index,
            pub.rid,
            blueprint.subject,
            blueprint.syllabus_code,
            blueprint.exam_board,
        )
        await pub.message(
            f"Round {round_index} started",
            data={"event": "round_start", "round": round_index, "blueprint": blueprint.as_dict()},
        )

        round_stats: dict[str, Any] = {"round": round_index}
        keyword_limit = int(opts.get("keyword_limit", 25))
        before_keyword = workspace.size()
        keyword_candidates = await self.services.keyword_search(blueprint, limit=keyword_limit)
        workspace.add_candidates(keyword_candidates)
        after_keyword = workspace.size()
        keyword_added = max(0, after_keyword - before_keyword)
        round_stats["keyword_candidates"] = len(keyword_candidates)
        round_stats["keyword_added"] = keyword_added
        await pub.message(
            "Keyword pass complete",
            data={
                "event": "keyword_pass",
                "round": round_index,
                "returned": len(keyword_candidates),
                "new": keyword_added,
                "workspace": workspace.summary(),
            },
        )

        semantic_limit = int(opts.get("semantic_limit", 15))
        before_semantic = workspace.size()
        semantic_candidates = await self.services.semantic_search(
            blueprint,
            limit=semantic_limit,
            score_threshold=opts.get("semantic_score_threshold"),
        )
        workspace.add_candidates(semantic_candidates)
        after_semantic = workspace.size()
        semantic_added = max(0, after_semantic - before_semantic)
        round_stats["semantic_candidates"] = len(semantic_candidates)
        round_stats["semantic_added"] = semantic_added
        await pub.message(
            "Semantic pass complete",
            data={
                "event": "semantic_pass",
                "round": round_index,
                "returned": len(semantic_candidates),
                "new": semantic_added,
                "workspace": workspace.summary(),
            },
        )

        summary = workspace.summary()
        round_stats["workspace_summary"] = summary
        await pub.message(
            "Round summary",
            data={"event": "round_summary", "round": round_index, "workspace": summary},
        )
        return round_stats

    async def _maybe_refine_blueprint(
        self,
        *,
        blueprint: QueryBlueprint,
        workspace: SearchWorkspace,
        pub: ProgressPublisher,
        round_index: int,
        round_stats: dict[str, Any] | None,
    ) -> BlueprintRevision | None:
        if not self._can_refine_blueprint() or not self.agent:
            return None
        snapshot = self._build_workspace_snapshot(workspace, round_stats)
        try:
            revision = await self.agent.refine_blueprint(blueprint, snapshot)
        except LLMClientError as exc:
            logger.warning("Blueprint refinement failed, skipping additional rounds: %s", exc)
            await pub.message(
                "Blueprint refinement skipped",
                data={"event": "refine_fallback", "round": round_index, "reason": str(exc)},
            )
            return None

        logger.info(
            "Blueprint refinement action=%s round=%s reason=%s",
            revision.action,
            round_index,
            revision.reason,
        )
        await pub.message(
            "Blueprint refined",
            data={
                "event": "blueprint_refined",
                "round": round_index,
                "action": revision.action,
                "reason": revision.reason,
                "blueprint": revision.blueprint.as_dict(),
                "provenance": revision.provenance,
            },
        )
        return revision

    def _build_workspace_snapshot(
        self,
        workspace: SearchWorkspace,
        round_stats: dict[str, Any] | None,
    ) -> dict[str, Any]:
        snapshot = workspace.snapshot(limit=5)
        stats = round_stats or {}
        snapshot["round"] = stats.get("round")
        snapshot["last_round"] = {
            "keyword_candidates": stats.get("keyword_candidates", 0),
            "keyword_added": stats.get("keyword_added", 0),
            "semantic_candidates": stats.get("semantic_candidates", 0),
            "semantic_added": stats.get("semantic_added", 0),
        }
        return snapshot

    def _heuristic_rerank(self, workspace: SearchWorkspace, *, limit: int) -> list[dict[str, Any]]:
        candidates = workspace.topk(limit * 2 if limit else 10)
        results: list[dict[str, Any]] = []
        seen_years: set[int] = set()

        for candidate in candidates:
            if len(results) >= limit:
                break
            year = candidate.year or -1
            if year in seen_years and year != -1:
                continue
            seen_years.add(year)
            results.append(
                {
                    **candidate.to_result(),
                    "reason": self._build_reason(candidate),
                }
            )

        if len(results) < limit:
            for candidate in candidates:
                if len(results) >= limit:
                    break
                payload = {
                    **candidate.to_result(),
                    "reason": self._build_reason(candidate, diversity=False),
                }
                if payload not in results:
                    results.append(payload)
        return results[:limit]

    def _build_reason(self, candidate: WorkspaceCandidate, diversity: bool = True) -> str:
        bits = []
        if candidate.subject:
            bits.append(candidate.subject)
        if candidate.year:
            bits.append(str(candidate.year))
        if candidate.source:
            bits.append(candidate.source.replace("_", " "))
        if diversity:
            bits.append("diversity rule satisfied")
        return ", ".join(bits) or "candidate selected"


@asynccontextmanager
async def _noop_guard():
    yield None
