# LangGraph Retrieval Implementation Plan

This document captures the concrete plan for delivering the LangGraph-powered retrieval experience inside Django (ASGI views, management commands, Dramatiq workers).

---

## 1. Align Foundations (Stage 0)

| Goal | Tasks |
| --- | --- |
| Reuse realtime + orchestration plumbing | Audit `SubscriptionConsumer`, `ProgressPublisher`, `awaitable_actor`, and `ResultOrchestrator` contracts to ensure retrieval can stream progress without bespoke glue (`backend/apps/service/realtime` and `backend/apps/service/orchestrators`). |
| Bootstrap helper | Ship a context manager/management-command mixin that calls `django.setup()`, starts the `ResultOrchestrator` + FutureRegistry cleanup loop, and tears them down for CLI/worker runs (required before LangGraph runs outside ASGI). |
| Shared service layer | Introduce `backend/apps/retrieval/services.py` exposing agent-ready Python functions (keyword search, semantic search, result hydration, embeddings/Qdrant access) so LangGraph nodes, HTTP views, and tests share identical code paths. |

Deliverable: foundational module + helper so later stages can run in any process.

---

## 2. Shared Tooling (Dataclasses + Services)

1. Define neutral dataclasses (`QueryBlueprint`, `WorkspaceCandidate`, `HydratedResult`) in `retrieval/services.py`.
2. Wrap existing helpers:
   - `pastpaper.tool.search_components` + `fetch_component_context` for deterministic keyword search.
   - `indexing.tool.list_active_indices` and chunk hydration for Qdrant hits.
   - Embedding generation via `service.tasks.generate_embedding`; encapsulate Qdrant client calls.
3. Provide a `SearchWorkspace` abstraction (in-memory dict or Redis) with CRUD, deduping, and scoring snapshots that every retrieval node can mutate.

Deliverable: single module every node imports; no DRF view calls inside LangGraph.

---

## 3. LangGraph Definition (Stage 1)

| Node | Responsibilities |
| --- | --- |
| `safeguard_gate` | Deterministic heuristics + optional LLM classifier; returns `allow`, `clarify`, or `reject`, logging decisions for observability. |
| `intent_parser` | One LLM call that emits a `QueryBlueprint` (subject, syllabus code, resource type, year range, semantic seed) plus a `needs_clarification` flag. |
| `clarify_loop` | When clarification is required, publish `ProgressPublisher.message` events describing missing info, wait for client input on the same RID, then resume the graph. |

Implementation details:
- Capture provenance metadata (node name, model) inside the blueprint.
- Provide unit tests with mocked LLM outputs for the parser/gate logic.

---

## 4. Multi-Index Retrieval Loop (Stage 2)

### Workspace Engine
- Build `SearchWorkspace` methods: `add_candidate`, `update_score`, `topk`, `stats`, `serialize`, `clear`.
- Store dedup keys (`paper_uuid`, `path`) and track source (`pastpaper_keyword`, `qdrant_semantic`, `expansion`).

### Retrieval Nodes
1. `keyword_pass`
   - Expand synonyms per subject (static dictionaries or heuristics).
   - Call keyword service with filters inferred from blueprint (subject, year range, paper type).
   - Emit progress events summarizing hits and top snippets.
2. `semantic_pass`
   - Generate a deterministic prompt from blueprint (`semantic_seed`).
   - Batch `generate_embedding` calls, query Qdrant, hydrate `ChunkRecord`s, and add them to the workspace with cosine similarity metadata.
3. `merge_and_expand`
   - Drop low-signal candidates, prefer active chunk plans, and optionally expand by fetching sibling/parent components via `fetch_component_context`.
   - Track iteration count to avoid infinite loops; decide whether to re-run keyword/semantic passes based on workspace size/score distribution.
4. `blueprint_refiner`
   - Feed the current blueprint plus workspace snapshot (candidate counts, sources, top hits) into an LLM tool.
   - Tool decides whether to stop or spin another retrieval round and can update blueprint filters (keywords, subject, year range) before looping.
   - Emit telemetry for each decision (action, reason, provenance) so we can audit the agentic chain during ablations.

Deliverable: ≤30 vetted candidates stored in the workspace, ready for reranking.

---

## 5. Rerank & Output (Stage 3)

1. `llm_rerank`
   - Prompt structure: blueprint summary + top N workspace candidates (snippet, metadata).
   - Enforce diversity (e.g., limit repeated years) and require JSON output `{candidate_id, score, justification}`.
2. `result_formatter`
   - Hydrate final payloads (paper metadata, component paths, URLs, source labels, reason).
   - Compose `{ "results": [...], "workspace_summary": {...} }`.
   - Emit `ProgressPublisher.finished` and clear workspace state.

Lifecycle integration:
- WebSocket calls stream `started` → `message` updates describing each node.
- CLI/management command prints concise logs and writes JSON output for batch use.

---

## 6. Execution Surfaces

- Replace `simulate_work` with an async LangGraph runner invoked by `EchoConsumer.start`.
- Structure:
  1. WebSocket action receives `{rid, text, ...}` and launches `asyncio.create_task(run_graph(rid, payload))`.
  2. Runner publishes progress via `ProgressPublisher(topic="retrieval")`.
  3. A Django management command (e.g., `python manage.py retrieval_run --query "...")` reuses the same runner inside the bootstrap helper.
  4. Plan for an HTTP endpoint that enqueues a Dramatiq job which still streams progress over the RID the client subscribed to.
- Update `realtime_demo.html` to show real stage transitions (intent summary, candidate counts, partial results) instead of simulated characters.

---

## 7. Observability & QA

- Metrics/logs:
  - Safeguard rejects, clarify loops invoked, time per stage, total turnaround, success rate (results returned vs. requests).
  - Track Dramatiq task failures via orchestrator logs.
- Testing:
  - Unit tests for keyword wrapper, semantic hydration, workspace merge logic, rerank formatter.
  - Mocked LangGraph integration test covering safeguard → rerank with fake LLM/Qdrant responses.
  - Realtime smoke test to ensure `ProgressPublisher` events reach `SubscriptionConsumer`.
- Documentation:
  - Extend `AGENTS.md` with new commands, environment variables, and the RID/token protocol.
  - Keep `plan.md` up to date with delivery status checkboxes.

---

## 8. Delivery Checklist

- [ ] Foundation helper + services module merged.
- [ ] LangGraph nodes (gate, parser, clarify, keyword, semantic, merge, rerank, formatter) implemented with typed contracts.
- [ ] WebSocket + CLI surfaces run the same runner; demo page reflects real output.
- [ ] Observability (metrics/logs) and tests in place; CI covers unit + integration paths.
- [ ] Documentation updated (README snippet, AGENTS guidelines, this plan).

---

## 9. LLM Agent Integration Plan

### Goals
- Bring LLM participation into Stage 1 (intent/clarify) and Stage 3 (rerank) while keeping the pipeline deterministic for downstream consumers.

### Components
1. **Async LLM Client**
   - Add `backend/apps/retrieval/llm_client.py` patterned after `pastpaper.parsers.agentic_parser.JSONChatClient`.
   - Responsibilities: apply `response_format={"type": "json_object"}`, parse/validate JSON (with regex fallback), log token usage, surface typed exceptions.
   - Inject settings (`RETRIEVAL_LLM_MODEL`, `RETRIEVAL_LLM_TIMEOUT`, retry counts) so deployments can swap providers.

2. **RetrievalAgent API**
   - New `backend/apps/retrieval/agent.py` exposing:
     ```python
     class RetrievalAgent:
         async def parse_intent(self, query: str, history: list[dict[str, str]]) -> IntentResponse
         async def rerank(self, blueprint: QueryBlueprint, candidates: list[WorkspaceCandidate]) -> list[RerankDecision]
     ```
   - `IntentResponse`: `blueprint`, `needs_clarification`, `clarification_prompt`, `provenance`.
   - `RerankDecision`: `candidate_id`, `score`, `reason`, `constraints`.
   - Both methods return dataclasses so the runner keeps typed contracts.

3. **Prompt Templates**
   - Store prompts as dedicated strings (e.g., `INTENT_SYSTEM_PROMPT`, `INTENT_USER_TEMPLATE`) to allow snapshot tests. Include explicit JSON schema descriptions and example outputs to reduce hallucinations.
   - Rerank prompt includes blueprint summary + truncated candidate payloads to stay within token limits.

4. **Clarification Loop Wiring**
   - Runner flow:
     1. `IntentResponse.needs_clarification` → emit `ProgressPublisher.message` with `event="clarify"` and the agent’s prompt.
     2. Wait for a follow-up payload (via WebSocket or HTTP) carrying user clarification; append it to the `history` list before calling `parse_intent` again.
     3. Abandon after configurable timeout, emitting `ProgressPublisher.error` so the UI can notify the user.

5. **Fallback Strategy**
   - Wrap agent calls with try/except; on failure log the raw response, increment metrics, and fall back to the existing heuristic blueprint/rerank so requests still complete.
   - Attach provenance in the blueprint/rerank payloads (`{"model": "...", "version": "v1"}`) for debugging.

6. **Testing & Observability**
   - Unit tests with mocked client responses to ensure JSON parsing, clarify handling, and rerank ordering work.
   - Prompt snapshot tests to catch accidental format changes.
   - Metrics/logs: `intent_llm_latency_ms`, `rerank_llm_failures`, `clarify_requests`, `clarify_timeouts`.
   - Optional sampling of prompts/responses into a secure store for offline tuning (respecting privacy constraints).

7. **Rollout Controls**
   - Feature flags (`settings.RETRIEVAL_USE_LLM_INTENT`, `..._RERANK`) to enable gradual rollout and easy rollback.
   - Update `realtime_demo.html` to display clarify prompts and LLM provenance fields so QA can visualize the agent’s behavior.

Deliverable: production-ready LLM agent nodes with robust JSON handling, clarification support, telemetry, and fallbacks so the retrieval pipeline can safely depend on model outputs.

### Current Status (LLM-in-the-loop Prototype)

- ✅ `RetrievalAgent` + `LLMClient` now live; enable with `RETRIEVAL_USE_LLM_INTENT` / `RETRIEVAL_USE_LLM_RERANK` and set `RETRIEVAL_LLM_MODEL`, `RETRIEVAL_LLM_BASE_URL`, plus export the API key via `RETRIEVAL_LLM_API_KEY_ENV` (defaults to `OPENROUTER_APIKEY`).
- ✅ `RetrievalRunner` streams `intent`, `clarify`, `rerank_provenance` events and falls back to heuristics on failures. Clients can send conversation history via `options.conversation`.
- ✅ The realtime demo includes a clarification prompt card so QA can exercise the loop end-to-end. Further polish (caching model responses, offline tests) remains on the backlog.
