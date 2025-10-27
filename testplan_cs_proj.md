# CS Project Module Split & Test Plans

This document locks the final four-module breakdown, deliverables, demo flow, and detailed test cases every teammate must cover in their individual submission and the shared integration video.

## Module 1 – LLM Parser & Paper Downloader

- **Owner Tasks**
  - Maintain `backend/apps/pastpaper/parsers/llmparser.py` (LLM prompts, caching, PDF slicing).
  - Operate the ingestion helper at `backend/tests/upload.py` to crawl/download PDFs before parsing.
  - Provide CLI scripts for: `download_papers_from_file`, `parse_pdf_to_qtree`, `export_parsed_json`.
- **Standalone Demo**
  1. Run downloader against one sample entry (show successful download and resume-safe skip logic).
  2. Execute the parser on the downloaded PDF; print / log the produced JSON tree.
- **Integration Demo Hook**
  - Save the parsed tree to the `PastPaper.parsed_tree` field so Module 2 can ingest it. Annotate the path (`media/parsed/<paper_id>.json`) in the PDF submission.
- **Integration Video Segment**
  1. Screen record the downloader terminal fetching a PDF (`requests.get` success line + saved path).
  2. Immediately run `python manage.py shell -c "from backend.apps.pastpaper.parsers.llmparser import LLMParser; ..."` (or helper script) to parse that exact file, printing the resulting JSON snippet.
  3. Show the same `paper_id` and JSON file path being handed to Module 2 (clip transition overlay text: “Passing paper 1234-uuid to Module 2”).
- **Test Plan (minimum cases)**

| Test | Purpose | Data / Steps | Expected |
| --- | --- | --- | --- |
| Parse clean OCR page | Ensure normal JSON output | Call `LLMParser.parse_page()` with a curated OCR snippet | Returns list of dicts (`num`, `content`, `marks`, `level`) without JSON errors |
| Parse blank/garbled page | Verify abnormal handling | Invoke parser with empty text | Logs warning and returns empty list |
| Downloader handles 404 | Resume + error path | Point downloader at a bad URL | Prints ❌, entry not marked done, script continues |

## Module 2 – Storage, Management & Indexing

- **Owner Tasks**
  - Manage CRUD + append-only logic in `backend/apps/pastpaper/api/views.py`.
  - Run indexing pipeline (`create_plans_sync`, `bundle_plan_sync`, `embed_chunk_batch`) per `backend/apps/indexing/DOC.md`.
  - Seed at least one `IndexProfile` and `ChunkPlan` that Module 4 can query.
- **Standalone Demo**
  1. Use Django shell or DRF client to create a PastPaper from Module 1’s parsed output.
  2. Trigger indexing and show bundle/chunk counts updating.
  3. Demonstrate both **keyword** and **semantic** search APIs:  
     - Keyword: call `RetrievalServices.keyword_search()` (or the REST endpoint) with filters.  
     - Semantic: call `RetrievalServices.semantic_search()` or `/service/embedding` + Qdrant query.  
     - Show console output proving both modes return matches for the seeded paper.
- **Integration Demo Hook**
  - Share the active `plan_id` and paper UUID with Modules 3 & 4. Note in your PDF where `RetrievalRunner` consumes these resources.
- **Integration Video Segment**
  1. In Django admin or shell, display the PastPaper created in Module 1 (matching UUID).
  2. Run `python manage.py shell` snippet to execute `create_plans_sync` / `bundle_plan_sync`, keeping terminal visible as counts update.
  3. Execute keyword search CLI (or REST call via curl/Postman) showing returned components; highlight `source="pastpaper_keyword"`.
  4. Execute semantic search (embedding + Qdrant query) showing vector scores; highlight `source="qdrant_semantic"`.
  5. End clip with the plan ID + paper UUID overlay to feed Module 4’s retrieval run.
- **Test Plan (minimum cases)**

| Test | Purpose | Data / Steps | Expected |
| --- | --- | --- | --- |
| Create paper succeeds | Normal ingest | `POST /pastpaper/papers/` with valid multipart form | 201 response, JSON contains `paper_id`, `version_no=1` |
| Bundling fails w/o components | Abnormal guard | Run `bundle_plan_sync` on plan whose paper lacks `components` | Result `status="failed"`, `last_error="No components available..."` |
| Keyword search returns match | Feature proof | Call keyword search with known metadata | Result list includes seeded paper/component |
| Semantic search returns match | Feature proof | Embed query via indexing services, query Qdrant | Result contains chunk IDs + scores above threshold |

## Module 3 – RBAC, Subscription & Credit Control

- **Owner Tasks**
  - Highlight models/services described in `backend/apps/accounts/docs/README.md`.
  - Expose `/api/accounts/auth/me/` and credit ledger endpoints.
  - Wire `spend_credits()` + `search_concurrency_guard()` hooks that Module 4 calls before/after search.
- **Standalone + Frontend Demo**
  1. In the UI (or a simple HTML mock), display the logged-in user’s plan name, credit totals, and rollover breakdown using `/auth/me`.
  2. Show a credit spend flow: trigger “Ask AI” once with sufficient balance (request passes), then simulate exhausted credits (manually drain ledger or set plan to zero) and demonstrate the UI disables the **Ask AI** action with an inline “Insufficient credits” message.
  3. Capture the API response where the backend returns `InsufficientCredits` / `ConcurrencyLimitError`.
- **Integration Demo Hook**
  - Ensure the frontend state Module 4 uses (RID + credits) derives from these APIs. Describe in comments where credits are decremented inside retrieval.
- **Integration Video Segment**
  1. Start on the frontend profile pane, calling `/api/accounts/auth/me/` (show devtools Network tab) and display plan + credit badges.
  2. Trigger the Ask AI action once; capture toast/log proving credits are deducted (e.g., `remaining_credits` drops).
  3. Drain credits via Django shell (or call `spend_credits`), refresh the frontend so `/auth/me` shows zero.
  4. Attempt Ask AI again: UI button disabled + backend response with `InsufficientCredits`; overlay text “Module 3 block passed to Module 4”.
- **Test Plan (minimum cases)**

| Test | Purpose | Data / Steps | Expected |
| --- | --- | --- | --- |
| `spend_credits` priority | Normal ledger math | Seed promo + rollover + monthly; call `spend_credits(50)` | Deductions consume promo → rollover → monthly |
| Insufficient credits error | Abnormal guard | Call `spend_credits` with demand > remaining | Raises `InsufficientCredits`, ledger unchanged |
| Concurrency guard limit | Enforcement | Simulate two simultaneous retrievals for Free tier | Second call raises `ConcurrencyLimitError`, realtime error event |
| Frontend Ask AI block | UX proof | Mock `/auth/me` returning zero credits; attempt Ask AI | Button disabled / toast shown, backend call skipped |

## Module 4 – Retrieval & Realtime Experience

- **Owner Tasks**
  - Maintain `backend/apps/retrieval/runner.py`, websocket consumer, and templates.
  - Integrate Module 3’s concurrency guard + credit spend calls.
  - Use Module 2’s keyword + semantic search services for candidate generation.
- **Standalone Demo**
  1. Launch the realtime demo page and issue a query that passes safeguards.  
     - Show ProgressPublisher events for intent, keyword pass, semantic pass, rerank, and final results.  
     - Highlight metadata proving both search modes were used (workspace summary counts for `pastpaper_keyword` vs `qdrant_semantic`).
  2. Trigger a blocked query (e.g., “build a bomb”) to showcase safeguard rejection.
  3. Trigger a low-credit scenario (Module 3 hook) to show the frontend “Ask AI” action disabled plus backend error.
- **Integration Demo Hook**
  - Record the combined run (Modules 2–4) where a user asks a question, credits are debited, keyword+semantic hits surface, and results render.
- **Integration Video Segment**
  1. Start recording the realtime demo page with websocket console open.
  2. Initiate a query; split-screen the backend log showing concurrency guard + credit spend (from Module 3).
  3. Highlight ProgressPublisher events as they arrive (`intent`, `keyword_pass`, `semantic_pass`, `complete`) and on-screen evidence that results reference the paper/plan from Module 2.
  4. Repeat with a low-credit user to show the guard short-circuiting and the frontend disabling Ask AI (tying back to Module 3’s clip).
- **Test Plan (minimum cases)**

| Test | Purpose | Data / Steps | Expected |
| --- | --- | --- | --- |
| Safeguard rejection | Security | `_safeguard("how to build a bomb")` | Verdict `allowed=False`, action `reject` |
| Full retrieval success | Normal flow | Stub services to return known candidates; run `run(rid="demo", query="IGCSE algebra")` | Events: started → intent → keyword_pass → semantic_pass → complete with ≥1 result |
| LLM fallback | Robustness | Force `RetrievalAgent` to raise `LLMClientError` | Runner logs error, falls back to heuristic rerank, finished event still emitted |
| Credit block respected | Integration | Run with zero credits; call Ask AI | Runner aborts early, publishes error, frontend shows block |

## Shared Demo Checklist

1. **Module 1 → Module 2**: Show downloader fetching PDF, parser generating JSON, then Module 2 ingesting that exact paper_id.
2. **Module 2 → Module 4**: Demonstrate keyword + semantic searches producing candidates for the ingested paper, then retrieval rendering them.
3. **Module 3 → Module 4 (frontend)**: Capture `/auth/me` UI state, then run Ask AI twice—first succeeds (credits deducted), second fails due to insufficient credits, disabling the Ask AI interaction.
4. Ensure the test plan tables in each teammate’s PDF reference these exact demo steps so the teacher can verify outputs against the video.
