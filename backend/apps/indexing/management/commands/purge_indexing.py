from __future__ import annotations

import uuid

from django.core.management.base import BaseCommand, CommandError

from backend.apps.indexing.models import ChunkPlan


class Command(BaseCommand):
    help = "Purge chunk plans (and optionally Qdrant vectors) for a paper/profile"

    def add_arguments(self, parser) -> None:  # pragma: no cover - framework method
        parser.add_argument(
            "--paper-pk",
            type=int,
            help="Internal PastPaper primary key",
        )
        parser.add_argument(
            "--paper-id",
            type=str,
            help="PastPaper public paper_id (UUID)",
        )
        parser.add_argument(
            "--profile",
            type=str,
            help="IndexProfile slug to filter plans",
        )
        parser.add_argument(
            "--plan-id",
            type=str,
            help="Specific ChunkPlan UUID (plan_id) to remove",
        )
        parser.add_argument(
            "--preserve-vectors",
            action="store_true",
            help="Do not delete Qdrant vectors (default is to delete)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show the plans that would be removed without deleting",
        )

    def handle(self, *args, **options):  # pragma: no cover - CLI entrypoint
        qs = ChunkPlan.objects.all().select_related("paper", "profile")

        paper_pk = options.get("paper_pk")
        paper_id = options.get("paper_id")
        profile_slug = options.get("profile")
        plan_uuid = options.get("plan_id")

        if plan_uuid:
            try:
                qs = qs.filter(plan_id=uuid.UUID(plan_uuid))
            except ValueError as exc:  # noqa: BLE001
                raise CommandError(f"Invalid plan_id UUID: {plan_uuid}") from exc

        if paper_pk is not None:
            qs = qs.filter(paper_id=paper_pk)

        if paper_id:
            qs = qs.filter(paper__paper_id=paper_id)

        if profile_slug:
            qs = qs.filter(profile__slug=profile_slug)

        if not plan_uuid and paper_pk is None and not paper_id and not profile_slug:
            raise CommandError("Provide at least one filter (--paper-pk/--paper-id/--profile/--plan-id)")

        plans = list(qs)
        if not plans:
            self.stdout.write(self.style.WARNING("No matching chunk plans found."))
            return

        drop_vectors = not options.get("preserve_vectors", False)
        if options.get("dry_run", False):
            self.stdout.write("Dry run: the following plans would be removed:")
            for plan in plans:
                self.stdout.write(
                    f" - plan_id={plan.plan_id} paper_pk={plan.paper_id} profile={plan.profile.slug}"
                )
            return

        deleted = 0
        for plan in plans:
            self.stdout.write(
                f"Deleting plan {plan.plan_id} (paper_pk={plan.paper_id} profile={plan.profile.slug})"
            )
            if not drop_vectors:
                plan._skip_vector_cleanup = True  # type: ignore[attr-defined]
            plan.delete()
            deleted += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Deleted {deleted} chunk plan(s). Qdrant vectors {'kept' if not drop_vectors else 'removed'}"
            )
        )
