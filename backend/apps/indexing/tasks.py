"""Dramatiq actors powering the indexing pipeline."""

from __future__ import annotations

import logging
import uuid
from typing import Iterable, Sequence

import dramatiq
from django.conf import settings
from django.utils import timezone

from backend.apps.pastpaper.models import PastPaper

from .bundler import build_bundles
from .chunker import split_bundle
from .embedding import embed_texts
from .models import (
    Bundle,
    Chunk,
    ChunkEmbeddingStatus,
    ChunkPlan,
    ChunkPlanStatus,
    IndexProfile,
)
from .qdrant import VectorRecord, ensure_collection, upsert_vectors
from .tokenization import get_tokenizer


logger = logging.getLogger(__name__)

PLAN_QUEUE = settings.INDEXING_PLAN_QUEUE
EMBED_QUEUE = settings.INDEXING_EMBED_QUEUE
BATCH_SIZE = settings.INDEXING_EMBED_BATCH_SIZE
MAX_EMBED_RETRIES = settings.INDEXING_MAX_EMBED_RETRIES


def _batched(iterable: Sequence[int], size: int) -> Iterable[list[int]]:
    if size <= 0:
        size = 1
    for idx in range(0, len(iterable), size):
        yield list(iterable[idx : idx + size])


def create_plans_sync(
    paper_id: str,
    *,
    profile_ids: Sequence[int] | None = None,
    enqueue_bundles: bool = True,
) -> list[ChunkPlan]:
    """Create or reset plans for a paper without requiring Dramatiq."""

    try:
        paper = PastPaper.objects.get(paper_id=paper_id)
    except PastPaper.DoesNotExist:
        logger.warning("Paper %s not found for indexing", paper_id)
        return []

    profiles_qs = IndexProfile.objects.filter(is_active=True)
    if profile_ids:
        profiles_qs = profiles_qs.filter(id__in=profile_ids)
    profiles = list(profiles_qs)

    plans: list[ChunkPlan] = []
    for profile in profiles:
        plan, created = ChunkPlan.objects.get_or_create(
            paper=paper,
            profile=profile,
            defaults={"status": ChunkPlanStatus.PENDING},
        )
        if not created:
            plan.status = ChunkPlanStatus.PENDING
            plan.last_error = ""
            plan.bundle_count = 0
            plan.chunk_count = 0
            plan.bundled_at = None
            plan.embedded_at = None
            plan.save(
                update_fields=[
                    "status",
                    "last_error",
                    "bundle_count",
                    "chunk_count",
                    "bundled_at",
                    "embedded_at",
                    "updated_at",
                ]
            )
        plans.append(plan)
        if enqueue_bundles:
            bundle_plan.send(plan.id)

    return plans


def bundle_plan_sync(
    plan_db_id: int,
    *,
    enqueue_embedding: bool = True,
) -> dict[str, object]:
    """Execute the bundling pipeline synchronously."""

    try:
        plan = ChunkPlan.objects.select_related("paper", "profile").get(id=plan_db_id)
    except ChunkPlan.DoesNotExist:
        logger.warning("ChunkPlan %s missing during bundling", plan_db_id)
        return {"status": "missing", "detail": "plan not found"}

    logger.info(
        "Bundling started for plan %s (paper=%s, profile=%s)",
        plan.plan_id,
        plan.paper_id,
        plan.profile.slug,
    )

    max_tokens = plan.profile.max_input_tokens or 0
    if max_tokens and plan.profile.chunk_size > max_tokens:
        logger.warning(
            "ChunkPlan %s (profile=%s) has chunk_size %s greater than encoder window %s; "
            "chunks may exceed the embedder limit.",
            plan.plan_id,
            plan.profile.slug,
            plan.profile.chunk_size,
            max_tokens,
        )

    if not plan.paper.components.exists():
        logger.warning(
            "ChunkPlan %s aborted: paper %s has no components to bundle",
            plan.plan_id,
            plan.paper_id,
        )
        plan.status = ChunkPlanStatus.FAILED
        plan.last_error = "No components available for bundling"
        plan.save(update_fields=["status", "last_error", "updated_at"])
        return {
            "status": ChunkPlanStatus.FAILED,
            "detail": plan.last_error,
            "bundle_count": 0,
            "chunk_count": 0,
        }

    plan.status = ChunkPlanStatus.BUNDLING
    plan.last_error = ""
    plan.save(update_fields=["status", "last_error", "updated_at"])
    plan.bundles.all().delete()
    plan.bundle_count = 0
    plan.chunk_count = 0
    plan.save(update_fields=["bundle_count", "chunk_count", "updated_at"])

    tokenizer = get_tokenizer(plan.profile.tokenizer)

    try:
        bundle_specs = build_bundles(
            plan.paper,
            tokenizer=tokenizer,
            target_tokens=plan.profile.target_bundle_tokens,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Bundling failed for plan %s", plan.plan_id)
        plan.status = ChunkPlanStatus.FAILED
        plan.last_error = str(exc)[:2000]
        plan.save(update_fields=["status", "last_error", "updated_at"])
        return {
            "status": ChunkPlanStatus.FAILED,
            "detail": plan.last_error,
            "bundle_count": 0,
            "chunk_count": 0,
        }

    if not bundle_specs:
        logger.warning(
            "ChunkPlan %s produced no bundles despite available components",
            plan.plan_id,
        )
        plan.status = ChunkPlanStatus.FAILED
        plan.last_error = "Bundling produced no bundles"
        plan.save(update_fields=["status", "last_error", "updated_at"])
        return {
            "status": ChunkPlanStatus.FAILED,
            "detail": plan.last_error,
            "bundle_count": 0,
            "chunk_count": 0,
        }

    bundle_models = [
        Bundle(
            plan=plan,
            sequence=spec.sequence,
            root_component_id=spec.component_ids[0] if spec.component_ids else None,
            title=spec.title,
            component_ids=spec.component_ids,
            span_paths=spec.span_paths,
            text=spec.text,
            token_count=spec.token_count,
        )
        for spec in bundle_specs
    ]
    Bundle.objects.bulk_create(bundle_models, batch_size=64)
    saved_bundles = {
        bundle.sequence: bundle
        for bundle in plan.bundles.order_by("sequence").all()
    }

    chunk_models: list[Chunk] = []
    global_sequence = 1
    for spec in bundle_specs:
        bundle = saved_bundles.get(spec.sequence)
        if bundle is None:
            continue
        chunk_specs = split_bundle(
            spec,
            tokenizer=tokenizer,
            chunk_size=plan.profile.chunk_size,
            overlap=plan.profile.chunk_overlap,
            max_tokens=max_tokens or None,
        )
        over_limit_chunks: list[dict[str, int]] = []
        for c_spec in chunk_specs:
            current_sequence = global_sequence
            chunk_models.append(
                Chunk(
                    plan=plan,
                    bundle=bundle,
                    sequence=current_sequence,
                    text=c_spec.text,
                    token_count=c_spec.token_count,
                    char_start=c_spec.char_start,
                    char_end=c_spec.char_end,
                )
            )
            if max_tokens and c_spec.token_count > max_tokens:
                over_limit_chunks.append(
                    {
                        "chunk_sequence": current_sequence,
                        "bundle_sequence": bundle.sequence,
                        "tokens": c_spec.token_count,
                    }
                )
            global_sequence += 1

        if over_limit_chunks:
            logger.error(
                "Chunking produced chunks exceeding encoder window for plan %s "
                "(profile=%s, max_tokens=%s, chunk_size=%s, target_bundle_tokens=%s): %s",
                plan.plan_id,
                plan.profile.slug,
                max_tokens,
                plan.profile.chunk_size,
                plan.profile.target_bundle_tokens,
                over_limit_chunks,
            )

    Chunk.objects.bulk_create(chunk_models, batch_size=128)

    if not chunk_models:
        logger.error(
            "ChunkPlan %s produced bundles but no chunks; marking as failed",
            plan.plan_id,
        )
        plan.status = ChunkPlanStatus.FAILED
        plan.last_error = "Chunking produced no chunks"
        plan.bundle_count = len(bundle_models)
        plan.chunk_count = 0
        plan.save(
            update_fields=[
                "status",
                "last_error",
                "bundle_count",
                "chunk_count",
                "updated_at",
            ]
        )
        return {
            "status": ChunkPlanStatus.FAILED,
            "detail": plan.last_error,
            "bundle_count": len(bundle_models),
            "chunk_count": 0,
        }

    plan.bundle_count = len(bundle_models)
    plan.chunk_count = len(chunk_models)
    plan.status = ChunkPlanStatus.READY_FOR_EMBEDDING
    plan.bundled_at = timezone.now()
    plan.save(
        update_fields=[
            "bundle_count",
            "chunk_count",
            "status",
            "bundled_at",
            "updated_at",
        ]
    )

    logger.info(
        "Bundling complete for plan %s: bundles=%s chunks=%s",
        plan.plan_id,
        plan.bundle_count,
        plan.chunk_count,
    )

    if enqueue_embedding:
        enqueue_embedding_plan.send(plan.id)

    return {
        "status": plan.status,
        "detail": "",
        "bundle_count": plan.bundle_count,
        "chunk_count": plan.chunk_count,
    }


def enqueue_embedding_plan_sync(
    plan_db_id: int,
    *,
    dispatch_batches: bool = True,
) -> dict[str, object]:
    """Collect candidate chunks for embedding, optionally dispatching workers."""

    try:
        plan = ChunkPlan.objects.select_related("profile", "paper").get(id=plan_db_id)
    except ChunkPlan.DoesNotExist:
        logger.warning("ChunkPlan %s missing during enqueue", plan_db_id)
        return {"status": "missing", "detail": "plan not found", "chunk_ids": []}

    chunk_ids = list(
        plan.chunks.filter(
            embedding_status__in=[
                ChunkEmbeddingStatus.PENDING,
                ChunkEmbeddingStatus.FAILED,
            ]
        )
        .order_by("sequence")
        .values_list("id", flat=True)
    )

    if not chunk_ids:
        _check_plan_completion(plan.id)
        return {
            "status": plan.status,
            "detail": "no chunks eligible",
            "chunk_ids": [],
        }

    plan.status = ChunkPlanStatus.EMBEDDING
    plan.last_error = ""
    plan.save(update_fields=["status", "last_error", "updated_at"])

    logger.info(
        "Queueing %s chunks for embedding for plan %s",
        len(chunk_ids),
        plan.plan_id,
    )

    batches: list[list[int]] = []
    for batch in _batched(chunk_ids, BATCH_SIZE):
        batches.append(batch)
        Chunk.objects.filter(id__in=batch).update(
            embedding_status=ChunkEmbeddingStatus.QUEUED,
            last_error="",
        )
        if dispatch_batches:
            embed_chunk_batch.send(plan.id, batch)

    return {
        "status": ChunkPlanStatus.EMBEDDING,
        "chunk_ids": chunk_ids,
        "batches": batches,
    }


def embed_chunk_batch_sync(
    plan_db_id: int,
    chunk_ids: list[int],
    *,
    persist: bool = True,
    check_completion: bool = True,
) -> dict[str, object]:
    """Embed a batch of chunks, optionally skipping persistence."""

    try:
        plan = ChunkPlan.objects.select_related("profile", "paper").get(id=plan_db_id)
    except ChunkPlan.DoesNotExist:
        logger.warning("ChunkPlan %s missing for embedding", plan_db_id)
        return {"status": "missing", "detail": "plan not found", "embedded": 0}

    logger.debug(
        "Embedding batch for plan %s: chunk_ids=%s",
        plan.plan_id,
        chunk_ids,
    )

    chunks = list(
        plan.chunks.filter(id__in=chunk_ids)
        .select_related("bundle")
        .order_by("sequence")
    )
    if not chunks:
        if check_completion:
            _check_plan_completion(plan.id)
        return {
            "status": plan.status,
            "detail": "no matching chunks",
            "embedded": 0,
        }

    max_tokens = plan.profile.max_input_tokens or 0
    over_limit_chunks: list[Chunk] = []
    for chunk in chunks:
        if max_tokens and chunk.token_count > max_tokens:
            over_limit_chunks.append(chunk)

    if over_limit_chunks:
        tokenizer = get_tokenizer(plan.profile.tokenizer)
        diagnostics: list[dict[str, object]] = []
        for chunk in over_limit_chunks:
            recalculated_tokens = len(
                tokenizer.encode(chunk.text, add_special_tokens=False)
            )
            diagnostics.append(
                {
                    "chunk_id": chunk.id,
                    "chunk_sequence": chunk.sequence,
                    "bundle_sequence": chunk.bundle.sequence,
                    "stored_tokens": chunk.token_count,
                    "recalculated_tokens": recalculated_tokens,
                }
            )
        logger.error(
            "Embedding batch contains chunks exceeding max tokens for plan %s "
            "(profile=%s, max_tokens=%s, profile_chunk_size=%s): %s",
            plan.plan_id,
            plan.profile.slug,
            max_tokens,
            plan.profile.chunk_size,
            diagnostics,
        )

    if persist:
        Chunk.objects.filter(id__in=chunk_ids).update(
            embedding_status=ChunkEmbeddingStatus.EMBEDDING,
            last_error="",
        )

    try:
        if persist:
            ensure_collection(plan.profile)
        vectors = embed_texts(plan.profile.encoder, [c.text for c in chunks])
        now = timezone.now()

        records: list[VectorRecord] = []
        preview_vectors: list[list[float]] = []
        for chunk, vector in zip(chunks, vectors):
            point_uuid = uuid.uuid5(plan.plan_id, f"{chunk.sequence:06d}")
            point_id = str(point_uuid)
            payload = {
                "plan_id": str(plan.plan_id),
                "chunk_pk": chunk.id,
                "paper_id": str(plan.paper.paper_id),
                "paper_version": plan.paper.version_no,
                "bundle_sequence": chunk.bundle.sequence,
                "chunk_sequence": chunk.sequence,
                "paths": chunk.bundle.span_paths,
                "component_ids": chunk.bundle.component_ids,
                "token_count": chunk.token_count,
            }
            if persist:
                records.append(VectorRecord(point_id=point_id, vector=vector, payload=payload))
            else:
                preview_vectors.append(vector)

        if persist and records:
            upsert_vectors(plan.profile, records)

            for chunk, record in zip(chunks, records):
                chunk.embedding_status = ChunkEmbeddingStatus.EMBEDDED
                chunk.embedded_at = now
                chunk.qdrant_point_id = record.point_id
                chunk.last_error = ""
                chunk.updated_at = now

            Chunk.objects.bulk_update(
                chunks,
                [
                    "embedding_status",
                    "embedded_at",
                    "qdrant_point_id",
                    "last_error",
                    "updated_at",
                ],
            )
    except Exception as exc:  # noqa: BLE001
        err = str(exc)[:2000]
        logger.exception("Embedding batch failed for plan %s", plan.plan_id)
        if persist:
            Chunk.objects.filter(id__in=chunk_ids).update(
                embedding_status=ChunkEmbeddingStatus.FAILED,
                last_error=err,
            )
            ChunkPlan.objects.filter(id=plan.id).update(
                status=ChunkPlanStatus.FAILED,
                last_error=err,
                updated_at=timezone.now(),
            )
        if check_completion:
            _check_plan_completion(plan.id)
        return {"status": "failed", "detail": err, "embedded": 0}
    else:
        if check_completion:
            _check_plan_completion(plan.id)
        if persist:
            return {
                "status": "embedded",
                "detail": "",
                "embedded": len(chunks),
            }
        return {
            "status": "preview",
            "detail": "",
            "embedded": len(chunks),
            "vectors": preview_vectors,
        }


@dramatiq.actor(queue_name=PLAN_QUEUE, max_retries=0)
def create_plans_for_paper(paper_id: str) -> None:
    create_plans_sync(paper_id, enqueue_bundles=True)


@dramatiq.actor(queue_name=PLAN_QUEUE, max_retries=0)
def bundle_plan(plan_db_id: int) -> None:
    bundle_plan_sync(plan_db_id, enqueue_embedding=True)


@dramatiq.actor(queue_name=PLAN_QUEUE, max_retries=0)
def enqueue_embedding_plan(plan_db_id: int) -> None:
    enqueue_embedding_plan_sync(plan_db_id, dispatch_batches=True)


@dramatiq.actor(queue_name=EMBED_QUEUE, max_retries=MAX_EMBED_RETRIES)
def embed_chunk_batch(plan_db_id: int, chunk_ids: list[int]) -> None:
    embed_chunk_batch_sync(plan_db_id, chunk_ids, persist=True, check_completion=True)


def _check_plan_completion(plan_db_id: int) -> None:
    try:
        plan = ChunkPlan.objects.get(id=plan_db_id)
    except ChunkPlan.DoesNotExist:
        return

    if plan.status == ChunkPlanStatus.FAILED:
        return

    outstanding = plan.chunks.exclude(embedding_status=ChunkEmbeddingStatus.EMBEDDED)
    if outstanding.filter(embedding_status=ChunkEmbeddingStatus.FAILED).exists():
        plan.status = ChunkPlanStatus.FAILED
        plan.last_error = plan.last_error or "Embedding failures detected"
        plan.save(update_fields=["status", "last_error", "updated_at"])
        return

    if outstanding.exists():
        return

    plan.status = ChunkPlanStatus.EMBEDDED
    plan.last_error = ""
    plan.embedded_at = timezone.now()
    plan.save(update_fields=["status", "last_error", "embedded_at", "updated_at"])

    logger.info(
        "Embedding complete for plan %s (paper=%s, profile=%s)",
        plan.plan_id,
        plan.paper_id,
        plan.profile.slug,
    )
