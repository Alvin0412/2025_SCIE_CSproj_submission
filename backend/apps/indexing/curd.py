"""Service helpers for coordinating indexing operations."""

from __future__ import annotations

import logging
from contextlib import suppress

from django.db import transaction

from .models import ChunkPlan, ChunkPlanStatus
from .qdrant import delete_plan
from .tasks import bundle_plan, create_plans_for_paper, enqueue_embedding_plan


logger = logging.getLogger(__name__)


def enqueue_indexing(paper_pk: str) -> None:
    """Kick off indexing for a parsed PastPaper by primary key."""

    logger.info("Enqueuing indexing for paper_pk=%s", paper_pk)
    create_plans_for_paper.send(paper_pk)


def _reset_plan_fields(plan: ChunkPlan) -> ChunkPlan:
    """Return plan to the pre-bundling state without dispatching work."""

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
    return plan


def reset_plan_state(plan_pk: int) -> ChunkPlan | None:
    """Public helper to reset a plan without queueing follow-up tasks."""

    try:
        plan = ChunkPlan.objects.get(pk=plan_pk)
    except ChunkPlan.DoesNotExist:
        logger.warning("Cannot reset missing plan %s", plan_pk)
        return None

    return _reset_plan_fields(plan)


def rerun_plan(plan_pk: int, *, requeue_embedding: bool = True) -> None:
    """Force a plan back through bundling and optional embedding."""

    try:
        plan = ChunkPlan.objects.get(pk=plan_pk)
    except ChunkPlan.DoesNotExist:
        logger.warning("Cannot rerun missing plan %s", plan_pk)
        return

    logger.info("Rerunning plan %s (requeue_embedding=%s)", plan.plan_id, requeue_embedding)

    plan = _reset_plan_fields(plan)

    bundle_plan.send(plan.id)
    if requeue_embedding:
        with suppress(Exception):
            enqueue_embedding_plan.send(plan.id)


def activate_plan(plan_pk: int) -> None:
    """Mark a plan as active for its (paper, profile) pair."""

    with transaction.atomic():
        plan = ChunkPlan.objects.select_for_update().get(pk=plan_pk)
        ChunkPlan.objects.filter(
            paper=plan.paper,
            profile=plan.profile,
        ).update(is_active=False)
        plan.is_active = True
        plan.save(update_fields=["is_active", "updated_at"])

    logger.info(
        "Activated plan %s for paper=%s profile=%s",
        plan.plan_id,
        plan.paper_id,
        plan.profile.slug,
    )


def deactivate_plans_for_paper(
    paper_pk: int,
    *,
    drop_vectors: bool = True,
    mark_failed: bool = True,
) -> int:
    """Disable all plans for a paper and optionally remove vector data."""

    plans = list(
        ChunkPlan.objects.filter(paper_id=paper_pk).select_related("profile")
    )
    if not plans:
        logger.debug("No chunk plans found to deactivate for paper %s", paper_pk)
        return 0

    updated_count = 0

    for plan in plans:
        if drop_vectors:
            try:
                delete_plan(plan.profile, plan)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Failed to delete Qdrant vectors for plan %s during deactivation: %s",
                    plan.plan_id,
                    exc,
                )

        plan.is_active = False
        fields = ["is_active"]

        if mark_failed and plan.status != ChunkPlanStatus.FAILED:
            plan.status = ChunkPlanStatus.FAILED
            plan.last_error = plan.last_error or "Paper deactivated"
            fields.extend(["status", "last_error"])

        plan.save(update_fields=fields)
        updated_count += 1

    logger.info(
        "Deactivated %s chunk plan(s) for paper %s (drop_vectors=%s, mark_failed=%s)",
        updated_count,
        paper_pk,
        drop_vectors,
        mark_failed,
    )
    return updated_count
