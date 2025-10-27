# PastPaper API

The PastPaper API manages exam paper versions, parsed states, and component trees. All routes are mounted under `/pastpaper/`.

## Core Resources
- `GET /pastpaper/papers/` – Query by `paper_id` or `paper_code`. Optional `type=asset|pdf|parsed`, `version_no`, `active_only=true`, or `redirect=true` (for PDFs).
- `GET /pastpaper/papers/{paper_id}/` – Fetch a specific paper version (defaults to active/latest). Supports the same query switches as the collection route.
- `POST /pastpaper/papers/` – Create the first version for a paper. Send multipart form data with metadata fields plus `file`.
- `PUT /pastpaper/papers/append/` – Append a new version. Requires `paper_id` and optionally a replacement `file` or metadata snapshot.
- `PATCH /pastpaper/papers/state/` – Update internal parsing state, error text, or parsed tree for a version.
- `DELETE /pastpaper/papers/delete/` – Remove a version (`version_no` optional). The most recent surviving version is reactivated automatically.
- `GET /pastpaper/papers/components/` – Return the parsed component tree for a paper. Query params: `paper_id` (required), `version_no`, `flat=true`, `path_prefix`, `page` filters.

## Typical Flows
1. **Ingest a paper**
   ```bash
   curl -X POST http://localhost:8000/pastpaper/papers/ \
     -F paper_code=9489_w22_ms_32 -F exam_board=CAIE -F subject="Mathematics" \
     -F year=2022 -F file=@paper.pdf
   ```
2. **Append a new scan**
   ```bash
   curl -X PUT http://localhost:8000/pastpaper/papers/append/ \
     -F paper_id=<existing-uuid> -F file=@paper_v2.pdf
   ```
3. **Track parsing progress**
   ```bash
   curl -X PATCH http://localhost:8000/pastpaper/papers/state/ \
     -H "Content-Type: application/json" \
     -d '{"paper_id": "<uuid>", "parsed_state": "READY"}'
   ```
4. **Serve PDFs directly**
   - `GET /pastpaper/papers/{paper_id}/?type=pdf&redirect=true` returns a 302 to cloud storage when the asset has a hosted URL.

## Notes
- All UUIDs refer to `PastPaper.paper_id` values; the API always returns a normalized representation via `PastPaperSerializer`.
- Multipart endpoints compute SHA-256 checksums and re-use metadata snapshots; metadata is append-only per version.
- Background parsing is powered by Dramatiq. Updating state does not trigger new jobs; use the service orchestrators for reprocessing.
- Component responses include score hints and positional data when available for downstream retrieval and highlighting tasks.
