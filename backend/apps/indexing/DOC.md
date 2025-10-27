# Indexing Module Technical Documentation

This document provides an in-depth guide to the indexing app housed in
`backend/apps/indexing`. It explains the data schema, semantic bundling and
chunking logic, embedding workflow, Qdrant integration, dramatiq task
orchestration, service helpers, and configuration knobs.

---

## 1. Data Model (`models.py`)

- **IndexProfile**
  - Describes one embedding/indexing strategy.
  - Fields capture encoder + tokenizer identifiers, embedding dimension, max
    token limits, chunk sizing (`chunk_size`, `chunk_overlap`), coarse bundle
    target (`target_bundle_tokens`), and Qdrant HNSW parameters.
  - `is_active` indicates whether new plans should be spawned for newly parsed
    papers. Index profiles are intended to be append-only once created.

- **ChunkPlan**
  - Represents applying an `IndexProfile` to a specific `PastPaper` version.
  - Tracks lifecycle via `ChunkPlanStatus` (`PENDING`, `BUNDLING`, `READY`,
    `EMBEDDING`, `EMBEDDED`, `FAILED`).
  - Maintains diagnostic fields: `last_error`, counts of bundles/chunks, and
    timestamps (`bundled_at`, `embedded_at`). `is_active` denotes the plan to
    use by default for a `(paper, profile)` pair.

- **Bundle**
  - Coarse semantic unit produced by `build_bundles`. Stores the ordered list
    of component ids, normalized paths, concatenated text, and estimated token
    count for each bundle.

- **Chunk**
  - Final retrieval units derived from bundles. Records sequence ordering,
    token counts, character spans within bundle text, embedding status via
    `ChunkEmbeddingStatus`, the Qdrant point id, and timestamps when vectors
    are written.

Each model includes indexes to support plan lookups, status queries, and active
plan selection.

---

## 2. Semantic Bundling (`bundler.py`)

`build_bundles(paper, tokenizer, target_tokens)` generates a `list[BundleSpec]`
while maintaining semantic cohesion:

1. **Tree Preparation** – loads `PastPaperComponent`s, groups them by parent
   id, and precomputes per-node text, normalized paths, and token counts using
   shared tokenizer utilities.

2. **Subtree Caching** – `compute_subtree()` recursively aggregates the ids,
   paths, text fragments, and token totals for each component subtree, storing
   the result in an LRU dictionary to avoid recomputation when siblings share
   descendants.

3. **Recursive Bundling** – `bundle_node()` attempts to keep a node and its
   entire subtree within `target_tokens`. If the subtree is too large, it
   greedily groups siblings together, carrying the parent's own text ahead of
   children while ensuring the sum stays within the token budget. Oversized
   child subtrees recurse individually.

4. **Bundle Emission** – Aggregated payloads are converted into `BundleSpec`
   objects preserving preorder traversal. Titles reuse the first text line or
   path to offer human-readable context.

This algorithm guarantees that bundles align with semantic subtrees (questions
and subparts) rather than arbitrary token windows.

---

## 3. Chunking (`chunker.py`)

`split_bundle(bundle, tokenizer, chunk_size, overlap)` converts bundle text
into overlapping token slices:

- Tokenizes the bundle text (excluding special tokens).
- Iterates with stride `chunk_size - overlap`, decoding each slice back to
  text, trimming whitespace, and calculating character offsets within the
  bundle. Pointer tracking minimizes misalignment for repeated phrases.
- Emits `ChunkSpec` objects that feed directly into `Chunk` persistence.

---

## 4. Tokenization Utilities (`tokenization.py`)

- `get_tokenizer(name)` – LRU-cached Hugging Face tokenizer loader (fast
  variant) guaranteeing consistent tokenization across all workers.
- `count_tokens(text, tokenizer)` – helper to compute token counts without
  adding special tokens, used heavily in bundling decisions.

---

## 5. Embedding Helpers (`embedding.py`)

- `load_encoder(model_name)` – cached loader returning `(tokenizer, model)` for
  the configured encoder. Models are set to evaluation mode.
- `embed_texts(model_name, texts)` – runs batch inference (GPU if available)
  and returns mean pooled embeddings suitable for cosine similarity. Encoding
  is padded and truncated safely to stay within model limits.

This module abstracts Hugging Face usage so dramatiq actors can call into a
simplified API.

---

## 6. Qdrant Integration (`qdrant.py`)

- `get_client()` – cached `QdrantClient` configured via settings
  (`QDRANT_URL`, `QDRANT_API_KEY`, `QDRANT_TIMEOUT`).
- `ensure_collection(profile)` – creates a collection when absent, using the
  profile’s dimension, distance metric mapping, and HNSW parameters.
- `upsert_vectors(profile, records)` – converts internal `VectorRecord`
  objects to `PointStruct`s and performs a blocking upsert.
- `delete_plan(profile, plan)` – removes all points for a plan using a
  payload filter on `plan_id`.

Payload schema encodes `plan_id`, chunk primary key, paper metadata, bundle
sequence, component ids, and token counts to support rich filtering during
retrieval.

---

## 7. Dramatiq Task Pipeline (`tasks.py`)

Actors and workflow:

- `create_plans_for_paper(paper_id)` – ensures a `ChunkPlan` exists for every
  active profile, resets previous runs, and dispatches `bundle_plan` messages.
- `bundle_plan(plan_id)` – executes the semantic bundler, persists `Bundle`
  rows in bulk, creates `Chunk` rows from `split_bundle`, updates plan counts,
  and queues embedding via `enqueue_embedding_plan`.
- `enqueue_embedding_plan(plan_id)` – batches pending / failed chunks using
  `INDEXING_EMBED_BATCH_SIZE`, marks them `QUEUED`, and dispatches
  `embed_chunk_batch` on the embedding queue.
- `embed_chunk_batch(plan_id, chunk_ids)` – embeds chunk text, ensures the
  target Qdrant collection exists, upserts vectors, marks chunks as
  `EMBEDDED`, and records point ids. Exceptions mark chunks failed and surface
  errors on the plan.
- `_check_plan_completion(plan_id)` – after each embedding batch, determines if
  all chunks are complete, promoting the plan to `EMBEDDED` or `FAILED`.

Queues and retries are governed by settings (`INDEXING_PLAN_QUEUE`,
`INDEXING_EMBED_QUEUE`, `INDEXING_MAX_EMBED_RETRIES`).

---

## 8. Service Facade (`curd.py`)

- `enqueue_indexing(paper_pk)` – entrypoint invoked from the parsing pipeline
  (`backend.apps.pastpaper.curd`) right after a paper reaches `READY`.
- `rerun_plan(plan_pk, requeue_embedding=True)` – resets plan state to
  `PENDING`, clears counters/timestamps, and re-enqueues bundling (and
  embedding if requested).
- `activate_plan(plan_pk)` – transactionally flips `is_active` for a plan while
  deactivating siblings sharing the same `(paper, profile)` pair.
- `deactivate_plans_for_paper(paper_pk, drop_vectors=True, mark_failed=True)` –
  disables every plan for a paper, optionally removing Qdrant vectors and
  marking the plans as failed. Used by lifecycle signals and manual ops.

Use these helpers from admin commands or management APIs instead of calling
actors directly.

---

## 9. App Configuration (`apps.py` & `config/settings.py`)

- `IndexingConfig.ready()` imports `tasks` to register dramatiq actors during
  Django startup.
- Settings register the indexing app and expose environment-driven configuration
  for Qdrant (`QDRANT_URL`, `QDRANT_API_KEY`, `QDRANT_TIMEOUT`) and dramatiq
  queues (`INDEXING_PLAN_QUEUE`, `INDEXING_EMBED_QUEUE`, `INDEXING_EMBED_BATCH_SIZE`,
  `INDEXING_MAX_EMBED_RETRIES`). Ensure these environment variables are defined
  in deployment manifests.

---

## 10. End-to-End Processing Pipeline

1. Parsing finishes → `enqueue_indexing` dispatches `create_plans_for_paper`.
2. Plans are created/reset, then `bundle_plan` runs semantic bundling/chunking.
3. Ready chunks are batched and sent to `embed_chunk_batch` for embeddings.
4. Embeddings are written to Qdrant; plan status becomes `EMBEDDED` upon
   success.

The default dramatiq worker command registers all actors:

```bash
python manage.py rundramatiq backend.apps.indexing.tasks -Q indexing-plan,indexing-embed
```

---

## 11. Operational Guidance

- **Migrations**

  ```bash
  python manage.py makemigrations indexing
  python manage.py migrate indexing
  ```

- **Profile Seeding** – create `IndexProfile` rows via Django admin, fixtures,
  or data migrations. Profiles are immutable once in use; clone to make
  changes.

- **Plan Management** – use `rerun_plan` for troubleshooting and
  `activate_plan` to swap default plans for a profile. When a paper must be
  hidden, call `deactivate_plans_for_paper` (or rely on the automatic hook when
  `PastPaper.is_active` becomes `False`).

- **Monitoring** – watch `ChunkPlan.status`, `last_error`, `bundle_count`, and
  `chunk_count`. Unexpected `FAILED` statuses usually indicate embedding or
  Qdrant connectivity issues. Dramatiq logs also surface batch-level failures.

- **Deployment Notes** – restart dramatiq workers whenever encoder/tokenizer
  versions change to invalidate in-memory caches. Qdrant schema changes (vector
  size or distance) require new collections and plan re-embedding.

- **Remediation Tooling** – the management command `python manage.py
  purge_indexing` removes plans (and optionally Qdrant vectors) for a specified
  paper/profile combination. Use `--dry-run` to inspect targets and
  `--preserve-vectors` to leave Qdrant untouched.

---

## 12. Extensibility

- Add new `IndexProfile`s for alternative encoders; the pipeline is profile-
  aware and will generate separate Qdrant collections.
- Retrieval services can join Qdrant payload metadata with `Chunk`/`Bundle`
  records to rebuild the hierarchical context.
- Additional post-processing (summarization, metadata enrichment) can hook into
  the plan lifecycle by watching for status transitions to `EMBEDDED` and
  enqueueing follow-up tasks.

---

## 13. Lifecycle Safeguards (`signals.py`)

- `pre_delete` on `ChunkPlan` automatically deletes Qdrant vectors unless the
  plan carries `_skip_vector_cleanup=True` (used by tooling when vectors should
  be preserved).
- `pre_save`/`post_save` on `PastPaper` detect `is_active` transitions. When a
  paper is deactivated, the handler uses `transaction.on_commit` to call
  `deactivate_plans_for_paper`, disabling all plans and removing vectors to
  keep search results in sync with paper visibility.
- Signals are registered in `IndexingConfig.ready()` alongside dramatiq actor
  imports to ensure lifecycle hooks are active as soon as Django loads.
