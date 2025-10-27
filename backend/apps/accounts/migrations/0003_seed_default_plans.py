from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import migrations
from django.utils import timezone


def seed_plans(apps, schema_editor):
    PlanTier = apps.get_model("accounts", "PlanTier")
    UserAccountMeta = apps.get_model("accounts", "UserAccountMeta")
    BillingCycle = apps.get_model("accounts", "BillingCycle")
    CreditLedgerEntry = apps.get_model("accounts", "CreditLedgerEntry")

    plans = [
        {
            "slug": "free",
            "name": "Free",
            "description": "Basic AI search with limited credits.",
            "monthly_price": Decimal("0"),
            "monthly_credits": 100,
            "concurrency_limit": 1,
            "is_default": True,
            "features": [
                "Basic AI search",
                "Limited generation",
                "Favorites",
                "Standard queue",
            ],
        },
        {
            "slug": "plus",
            "name": "Plus",
            "description": "Full AI search with extended limits.",
            "monthly_price": Decimal("9.9"),
            "monthly_credits": 2000,
            "concurrency_limit": 2,
            "is_default": False,
            "features": [
                "Full AI search + generation",
                "Faster queue",
                "Usage analytics",
                "Cross-device sync",
            ],
        },
    ]

    plan_cache = {}
    for attrs in plans:
        plan, _ = PlanTier.objects.update_or_create(slug=attrs["slug"], defaults=attrs)
        plan_cache[plan.slug] = plan

    default_plan = plan_cache.get("free") or PlanTier.objects.order_by("monthly_price").first()
    if not default_plan:
        return

    now = timezone.now()
    period = timedelta(days=int(getattr(settings, "ACCOUNTS_BILLING_PERIOD_DAYS", 30)))

    for meta in UserAccountMeta.objects.select_related("user").all():
        needs_save = False
        if not meta.plan_id:
            meta.plan = default_plan
            needs_save = True
        if not meta.plan_started_at:
            meta.plan_started_at = now
            needs_save = True
        if not meta.current_cycle_started_at:
            meta.current_cycle_started_at = now
            needs_save = True
        if not meta.next_billing_at:
            meta.next_billing_at = now + period
            needs_save = True
        if needs_save:
            meta.save(
                update_fields=[
                    "plan",
                    "plan_started_at",
                    "current_cycle_started_at",
                    "next_billing_at",
                    "updated_at",
                ]
            )

        cycle, created = BillingCycle.objects.get_or_create(
            user=meta.user,
            cycle_start=meta.current_cycle_started_at,
            defaults={
                "plan": meta.plan,
                "cycle_end": meta.next_billing_at or (meta.current_cycle_started_at + period),
                "monthly_allocation": meta.plan.monthly_credits,
                "rollover_allocation": 0,
            },
        )
        if created and meta.plan.monthly_credits > 0:
            CreditLedgerEntry.objects.create(
                user=meta.user,
                cycle=cycle,
                source_type="monthly",
                amount=meta.plan.monthly_credits,
                remaining_amount=meta.plan.monthly_credits,
                metadata={"cycle_id": str(cycle.id)},
            )


def remove_plans(apps, schema_editor):
    PlanTier = apps.get_model("accounts", "PlanTier")
    PlanTier.objects.filter(slug__in=["free", "plus"]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0002_plantier_useraccountmeta_current_cycle_started_at_and_more"),
    ]

    operations = [
        migrations.RunPython(seed_plans, reverse_code=remove_plans),
    ]
