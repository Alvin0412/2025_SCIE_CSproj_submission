from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Sequence

from django.conf import settings

from .llm_client import LLMClient, LLMClientError
from .services import QueryBlueprint, WorkspaceCandidate

INTENT_SYSTEM_PROMPT = """You are an expert study-guide librarian helping students find PastPaper questions.
Given a user query (plus optional prior conversation), you must:
- decide whether to allow, clarify, or reject the request
- when allowed, produce a normalized retrieval blueprint
- when clarification is required, provide one concise question that would unblock the search
Return a strict JSON object that matches the documented schema."""

INTENT_USER_TEMPLATE = """
User query:
\"\"\"{query}\"\"\"

Conversation history (oldest first, may be empty):
{history}

Respond with JSON containing:
- action: "allow" | "clarify" | "reject"
- reason: short explanation for your decision
- needs_clarification: boolean (true only if the action is "clarify")
- clarification_prompt: empty unless clarification required
- blueprint: object with fields
  {{
    "subject": string|null,
    "syllabus_code": string|null,
    "exam_board": string|null,
    "resource_type": string ("question" default, or mark_scheme/full_paper/notes/etc),
    "year_range": [startYear|null, endYear|null],
    "keywords": array of <=6 lowercase keywords,
    "semantic_seed": short natural language summary (<=200 chars),
    "provenance": {{}}
  }}
- provenance: object describing the model (include model name and prompt version).
"""

RERANK_SYSTEM_PROMPT = """You are a careful exam coach reranking candidate study materials.
Given a structured blueprint and the candidate list, return JSON with a 'decisions' array ordered from best to worst.
Each decision entry must include candidate_id, a numeric score between 0 and 1, and a short reason highlighting subject/year/resource fit."""

RERANK_USER_TEMPLATE = """
Blueprint context:
{blueprint}

Candidates (JSON array):
{candidates}

Return JSON with:
{{
  "decisions": [{{"candidate_id": "...", "score": 0.92, "reason": "..."}}],
  "provenance": {{}}
}}
Limit the list to the most relevant items only.
"""

REFINER_SYSTEM_PROMPT = """You are an iterative retrieval planner refining search blueprints.
You inspect the current blueprint plus a snapshot of the workspace (candidate counts, sources, top hits) and decide
whether another retrieval round should run. When you continue, update the blueprint with concrete adjustments like
adding/removing keywords, narrowing subjects, or adjusting year ranges. Keep changes minimal and grounded in the snapshot."""

REFINER_USER_TEMPLATE = """
Current blueprint JSON:
{blueprint}

Workspace snapshot:
{workspace_snapshot}

Return JSON:
{{
  "action": "continue" | "stop",
  "reason": "short explanation of your decision",
  "blueprint": {{
     "subject": string|null,
     "syllabus_code": string|null,
     "exam_board": string|null,
     "resource_type": string|null,
     "year_range": [start|null, end|null],
     "keywords": array<string>,
     "semantic_seed": string|null
  }},
  "provenance": {{}}
}}
- Use "stop" only when the snapshot already covers the needed material or further changes would not help.
- Unspecified fields must retain their previous values; never reset useful metadata to null without reason.
"""


@dataclass(slots=True)
class IntentResponse:
    action: str
    reason: str
    needs_clarification: bool
    clarification_prompt: str
    blueprint: QueryBlueprint | None
    provenance: dict[str, Any]


@dataclass(slots=True)
class RerankDecision:
    candidate_id: str
    score: float
    reason: str


@dataclass(slots=True)
class RerankResponse:
    decisions: list[RerankDecision]
    provenance: dict[str, Any]


@dataclass(slots=True)
class BlueprintRevision:
    action: str
    reason: str
    blueprint: QueryBlueprint
    provenance: dict[str, Any]


class RetrievalAgent:
    """LLM-backed intent parser and reranker for the retrieval runner."""

    def __init__(self, client: LLMClient | None = None):
        self.client = client or LLMClient()
        self.intent_prompt_version = "intent_v1"
        self.rerank_prompt_version = "rerank_v1"
        self.refine_prompt_version = "refine_v1"
        self.max_rerank_candidates = int(getattr(settings, "RETRIEVAL_LLM_MAX_CANDIDATES", 12))

    async def parse_intent(
        self,
        query: str,
        *,
        history: Sequence[dict[str, str]] | None = None,
    ) -> IntentResponse:
        rendered_history = self._render_history(history or [])
        messages = [
            {"role": "system", "content": INTENT_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": INTENT_USER_TEMPLATE.format(query=query.strip(), history=rendered_history),
            },
        ]
        payload = await self.client.complete_json(messages)
        action = (payload.get("action") or "allow").lower()
        needs_clarification = bool(payload.get("needs_clarification"))
        clarification_prompt = (payload.get("clarification_prompt") or "").strip()
        blueprint_data = payload.get("blueprint") or {}
        blueprint = self._convert_blueprint(blueprint_data, fallback_query=query)
        provenance = payload.get("provenance") or {}
        provenance.setdefault("model", getattr(self.client, "model", ""))
        provenance.setdefault("prompt_version", self.intent_prompt_version)
        reason = (payload.get("reason") or "").strip()
        if action != "clarify":
            needs_clarification = False
            clarification_prompt = ""
        return IntentResponse(
            action=action,
            reason=reason,
            needs_clarification=needs_clarification,
            clarification_prompt=clarification_prompt,
            blueprint=blueprint,
            provenance=provenance,
        )

    async def rerank(
        self,
        blueprint: QueryBlueprint,
        candidates: Sequence[WorkspaceCandidate],
    ) -> RerankResponse:
        limited = list(candidates)[: self.max_rerank_candidates]
        if not limited:
            return RerankResponse(decisions=[], provenance={})
        blueprint_summary = json.dumps(blueprint.as_dict(), ensure_ascii=False)
        candidates_payload = [
            {
                "candidate_id": c.candidate_id,
                "paper_code": c.paper_code,
                "year": c.year,
                "path": c.path,
                "subject": c.subject,
                "source": c.source,
                "score": round(float(c.score), 4),
                "snippet": (c.snippet[:260] + "...") if len(c.snippet or "") > 260 else c.snippet,
                "metadata": c.metadata,
            }
            for c in limited
        ]
        messages = [
            {"role": "system", "content": RERANK_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": RERANK_USER_TEMPLATE.format(
                    blueprint=blueprint_summary,
                    candidates=json.dumps(candidates_payload, ensure_ascii=False),
                ),
            },
        ]
        payload = await self.client.complete_json(messages)
        provenance = payload.get("provenance") or {}
        provenance.setdefault("model", getattr(self.client, "model", ""))
        provenance.setdefault("prompt_version", self.rerank_prompt_version)
        decisions = []
        for item in payload.get("decisions") or []:
            cid = str(item.get("candidate_id") or "").strip()
            if not cid:
                continue
            try:
                score = float(item.get("score"))
            except (TypeError, ValueError):
                score = 0.0
            reason = (item.get("reason") or "").strip() or "LLM-selected"
            decisions.append(RerankDecision(candidate_id=cid, score=score, reason=reason))
        return RerankResponse(decisions=decisions, provenance=provenance)

    async def refine_blueprint(
        self,
        blueprint: QueryBlueprint,
        workspace_snapshot: dict[str, Any],
    ) -> BlueprintRevision:
        messages = [
            {"role": "system", "content": REFINER_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": REFINER_USER_TEMPLATE.format(
                    blueprint=json.dumps(blueprint.as_dict(), ensure_ascii=False),
                    workspace_snapshot=json.dumps(workspace_snapshot, ensure_ascii=False),
                ),
            },
        ]
        payload = await self.client.complete_json(messages)
        action = (payload.get("action") or "continue").strip().lower()
        if action not in {"continue", "stop"}:
            action = "continue"
        reason = (payload.get("reason") or "").strip()
        provenance = payload.get("provenance") or {}
        provenance.setdefault("model", getattr(self.client, "model", ""))
        provenance.setdefault("prompt_version", self.refine_prompt_version)
        updated_data = payload.get("blueprint") or {}
        updated_blueprint = self._convert_blueprint(updated_data, fallback_query=blueprint.raw_query)
        merged = self._merge_blueprints(blueprint, updated_blueprint)
        merged.provenance = {**(blueprint.provenance or {}), **(merged.provenance or {}), "refiner": provenance}
        return BlueprintRevision(action=action, reason=reason, blueprint=merged, provenance=provenance)

    def _render_history(self, history: Sequence[dict[str, str]]) -> str:
        if not history:
            return "(empty)"
        formatted = []
        for turn in history:
            role = turn.get("role") or "user"
            content = (turn.get("content") or "").strip()
            formatted.append(f"- {role}: {content}")
        return "\n".join(formatted)

    def _convert_blueprint(self, data: Dict[str, Any], fallback_query: str) -> QueryBlueprint:
        keywords = tuple(str(token).strip().lower() for token in (data.get("keywords") or []) if token)
        yr = data.get("year_range") or (None, None)
        year_from = yr[0] if isinstance(yr, (list, tuple)) and yr else None
        year_to = yr[1] if isinstance(yr, (list, tuple)) and len(yr) > 1 else None
        return QueryBlueprint(
            raw_query=fallback_query,
            subject=self._clean_str(data.get("subject")),
            syllabus_code=self._clean_str(data.get("syllabus_code")),
            exam_board=self._clean_str(data.get("exam_board")),
            resource_type=self._clean_str(data.get("resource_type")) or "question",
            year_range=(self._coerce_int(year_from), self._coerce_int(year_to)),
            keywords=keywords,
            semantic_seed=self._clean_str(data.get("semantic_seed")) or fallback_query.strip(),
            provenance=data.get("provenance") or {},
        )

    def _merge_blueprints(self, base: QueryBlueprint, updated: QueryBlueprint) -> QueryBlueprint:
        year_range = updated.year_range
        if not year_range or not any(year_range):
            year_range = base.year_range
        keywords = updated.keywords or base.keywords
        provenance = {**(base.provenance or {}), **(updated.provenance or {})}
        merged = QueryBlueprint(
            raw_query=updated.raw_query or base.raw_query,
            subject=updated.subject or base.subject,
            syllabus_code=updated.syllabus_code or base.syllabus_code,
            exam_board=updated.exam_board or base.exam_board,
            resource_type=updated.resource_type or base.resource_type,
            year_range=year_range,
            keywords=keywords,
            semantic_seed=updated.semantic_seed or base.semantic_seed,
            provenance=provenance,
        )
        return merged

    @staticmethod
    def _clean_str(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @staticmethod
    def _coerce_int(value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None


__all__ = [
    "IntentResponse",
    "RerankDecision",
    "RerankResponse",
    "BlueprintRevision",
    "RetrievalAgent",
]
