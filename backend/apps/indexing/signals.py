"""Model signal handlers for indexing lifecycle events."""

from __future__ import annotations

import logging

from django.db import transaction
from django.db.models.signals import post_save, pre_delete, pre_save
from django.dispatch import receiver

from backend.apps.pastpaper.models import PastPaper

from .curd import deactivate_plans_for_paper
from .models import ChunkPlan
from .qdrant import delete_plan


logger = logging.getLogger(__name__)


@receiver(pre_delete, sender=ChunkPlan)
def cleanup_vectors_on_plan_delete(sender, instance: ChunkPlan, **kwargs) -> None:
    """Remove Qdrant points when a plan is deleted unless explicitly skipped."""
    logger.debug("Received pre_delete signal for ChunkPlan %s", instance.plan_id)

    if getattr(instance, "_skip_vector_cleanup", False):
        logger.debug(
            "Skipping vector cleanup for plan %s due to explicit flag", instance.plan_id
        )
        return

    try:
        delete_plan(instance.profile, instance)
        logger.info(
            "Deleted Qdrant vectors for plan %s during plan deletion", instance.plan_id
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Failed to delete Qdrant vectors for plan %s: %s", instance.plan_id, exc
        )


@receiver(pre_save, sender=PastPaper)
def remember_previous_is_active(sender, instance: PastPaper, **kwargs) -> None:
    """Capture the previous is_active flag for change detection."""
    logger.debug("Received pre_save signal for PastPaper %s", instance.pk)
    if not instance.pk:
        return
    try:
        previous = sender.objects.only("is_active").get(pk=instance.pk)
    except sender.DoesNotExist:
        return
    instance._previous_is_active = previous.is_active  # type: ignore[attr-defined]


@receiver(post_save, sender=PastPaper)
def handle_paper_deactivation(
    sender, instance: PastPaper, created: bool, **kwargs
) -> None:
    """Deactivate plans (and their vectors) when a paper is disabled."""
    logger.debug("Received post_save signal for PastPaper %s", instance.pk)
    if created:
        return

    previous_active = getattr(instance, "_previous_is_active", None)
    if previous_active is None or previous_active is instance.is_active:
        return

    if instance.is_active:
        return

    paper_pk = instance.pk

    def _deactivate() -> None:
        logger.info(
            "PastPaper %s deactivated; disabling associated chunk plans", paper_pk
        )
        deactivate_plans_for_paper(paper_pk, drop_vectors=True, mark_failed=True)

    transaction.on_commit(_deactivate)
