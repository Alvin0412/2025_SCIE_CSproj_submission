from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Iterable

from django.conf import settings
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from .models import (
    BillingCycle,
    CreditLedgerEntry,
    CreditUsageLog,
    PlanTier,
    User,
    UserAccountMeta,
)


class InsufficientCredits(Exception):
    """Raised when a user attempts to consume more credits than remain."""


BILLING_PERIOD_DAYS = int(getattr(settings, "ACCOUNTS_BILLING_PERIOD_DAYS", 30))


@dataclass
class CreditSnapshot:
    total_allocated: int
    total_remaining: int
    total_used: int
    promo_remaining: int
    rollover_remaining: int
    monthly_remaining: int
    add_on_remaining: int
    next_reset_at: datetime | None
    cycle_start: datetime | None
    cycle_end: datetime | None


def billing_period() -> timedelta:
    """Return the standard billing period duration."""

    return timedelta(days=BILLING_PERIOD_DAYS)


def get_default_plan() -> PlanTier:
    plan = PlanTier.objects.filter(is_default=True, is_active=True).order_by("monthly_price").first()
    if plan:
        return plan
    return (
        PlanTier.objects.filter(is_active=True).order_by("monthly_price").first()
        or PlanTier(slug="free", name="Free", monthly_price=Decimal("0.00"), monthly_credits=0)
    )


def ensure_account_meta(user: User) -> UserAccountMeta:
    meta, _ = UserAccountMeta.objects.get_or_create(user=user)
    return meta


def ensure_subscription_state(user: User, *, now=None) -> BillingCycle:
    """Ensure the user has an active plan, cycle, and monthly ledger entry."""

    now = now or timezone.now()
    with transaction.atomic():
        meta = ensure_account_meta(user)
        if not meta.plan:
            plan = get_default_plan()
            meta.plan = plan
            meta.plan_started_at = now
            meta.current_cycle_started_at = now
            meta.next_billing_at = now + billing_period()
            meta.save(
                update_fields=["plan", "plan_started_at", "current_cycle_started_at", "next_billing_at", "updated_at"]
            )
        plan = meta.plan
        cycle = (
            BillingCycle.objects.select_for_update()
            .filter(user=user, status=BillingCycle.STATUS_CURRENT, cycle_start__lte=now, cycle_end__gt=now)
            .select_related("plan")
            .first()
        )
        if not cycle:
            cycle_start = meta.current_cycle_started_at or now
            cycle_end = meta.next_billing_at or (cycle_start + billing_period())
            rollover_amount = (
                CreditLedgerEntry.objects.filter(
                    user=user,
                    remaining_amount__gt=0,
                    source_type__in=[
                        CreditLedgerEntry.SOURCE_MONTHLY,
                        CreditLedgerEntry.SOURCE_ROLLOVER,
                    ],
                ).aggregate(total=Sum("remaining_amount"))["total"]
                or 0
            )
            cycle = BillingCycle.objects.create(
                user=user,
                plan=plan,
                cycle_start=cycle_start,
                cycle_end=cycle_end,
                monthly_allocation=plan.monthly_credits,
                rollover_allocation=rollover_amount,
            )
            meta.current_cycle_started_at = cycle_start
            meta.next_billing_at = cycle_end
            if not meta.plan_started_at:
                meta.plan_started_at = cycle_start
            meta.save(update_fields=["current_cycle_started_at", "next_billing_at", "plan_started_at", "updated_at"])
        _ensure_monthly_entry(user, cycle)
        return cycle


def _ensure_monthly_entry(user: User, cycle: BillingCycle):
    exists = CreditLedgerEntry.objects.filter(
        user=user,
        cycle=cycle,
        source_type=CreditLedgerEntry.SOURCE_MONTHLY,
    ).exists()
    if not exists and cycle.monthly_allocation > 0:
        CreditLedgerEntry.objects.create(
            user=user,
            cycle=cycle,
            source_type=CreditLedgerEntry.SOURCE_MONTHLY,
            amount=cycle.monthly_allocation,
            remaining_amount=cycle.monthly_allocation,
            metadata={"cycle_id": str(cycle.id)},
        )


def credit_snapshot(user: User, *, now=None) -> CreditSnapshot:
    """Return a snapshot of the user's plan and credit balances."""

    cycle = ensure_subscription_state(user, now=now)
    entries = CreditLedgerEntry.objects.filter(user=user)

    total_allocated = entries.aggregate(total=Sum("amount"))["total"] or 0
    total_remaining = entries.aggregate(total=Sum("remaining_amount"))["total"] or 0
    promo_remaining = _sum_entries(entries, {CreditLedgerEntry.SOURCE_PROMO})
    add_on_remaining = _sum_entries(entries, {CreditLedgerEntry.SOURCE_TOP_UP, CreditLedgerEntry.SOURCE_ADJUSTMENT})
    rollover_remaining = _sum_rolled_over(entries, cycle)
    monthly_remaining = _sum_current_monthly(entries, cycle)
    total_used = total_allocated - total_remaining

    return CreditSnapshot(
        total_allocated=total_allocated,
        total_remaining=total_remaining,
        total_used=max(total_used, 0),
        promo_remaining=promo_remaining,
        rollover_remaining=rollover_remaining,
        monthly_remaining=monthly_remaining,
        add_on_remaining=add_on_remaining,
        next_reset_at=cycle.cycle_end,
        cycle_start=cycle.cycle_start,
        cycle_end=cycle.cycle_end,
    )


def _sum_entries(entries_queryset, source_types: Iterable[str]) -> int:
    return (
        entries_queryset.filter(source_type__in=list(source_types)).aggregate(total=Sum("remaining_amount"))["total"]
        or 0
    )


def _sum_rolled_over(entries_queryset, current_cycle: BillingCycle) -> int:
    prev_monthly = entries_queryset.filter(
        source_type=CreditLedgerEntry.SOURCE_MONTHLY,
    )
    if current_cycle:
        prev_monthly = prev_monthly.exclude(cycle=current_cycle)
    total_prev_monthly = prev_monthly.aggregate(total=Sum("remaining_amount"))["total"] or 0
    rollover_entries = entries_queryset.filter(source_type=CreditLedgerEntry.SOURCE_ROLLOVER)
    from_rollover = rollover_entries.aggregate(total=Sum("remaining_amount"))["total"] or 0
    return total_prev_monthly + from_rollover


def _sum_current_monthly(entries_queryset, current_cycle: BillingCycle) -> int:
    if not current_cycle:
        return 0
    return (
        entries_queryset.filter(
            source_type=CreditLedgerEntry.SOURCE_MONTHLY,
            cycle=current_cycle,
        ).aggregate(total=Sum("remaining_amount"))["total"]
        or 0
    )


def spend_credits(
    user: User,
    *,
    credits: int,
    reason: str,
    reference_type: str = "",
    reference_id: str = "",
    metadata: dict | None = None,
):
    """Consume credits following the established priority order."""

    if credits <= 0:
        return
    now = timezone.now()
    with transaction.atomic():
        cycle = ensure_subscription_state(user, now=now)
        entries = (
            CreditLedgerEntry.objects.select_for_update()
            .filter(user=user, remaining_amount__gt=0)
            .select_related("cycle")
        )
        available = sum(entry.remaining_amount for entry in entries)
        if available < credits:
            raise InsufficientCredits("Insufficient credits for this operation.")
        ordered_entries = sorted(entries, key=lambda entry: _entry_priority(entry, cycle.id if cycle else None))
        remaining = credits
        consumption_summary = []
        for entry in ordered_entries:
            if remaining <= 0:
                break
            take = min(entry.remaining_amount, remaining)
            if take <= 0:
                continue
            entry.remaining_amount -= take
            entry.save(update_fields=["remaining_amount", "updated_at"])
            consumption_summary.append({"entry_id": entry.id, "source": entry.source_type, "credits": take})
            remaining -= take
        if remaining > 0:
            raise InsufficientCredits("Failed to consume requested credits due to a race condition.")
        CreditUsageLog.objects.create(
            user=user,
            cycle=cycle,
            credits_used=credits,
            reason=reason,
            source_summary=consumption_summary,
            reference_type=reference_type,
            reference_id=reference_id,
            metadata=metadata or {},
        )


def _entry_priority(entry: CreditLedgerEntry, current_cycle_id: int | None) -> tuple[int, datetime]:
    if entry.source_type == CreditLedgerEntry.SOURCE_PROMO:
        return 0, entry.created_at
    if entry.source_type == CreditLedgerEntry.SOURCE_ROLLOVER:
        return 1, entry.created_at
    if entry.source_type == CreditLedgerEntry.SOURCE_MONTHLY:
        if current_cycle_id and entry.cycle_id == current_cycle_id:
            return 2, entry.created_at
        return 1, entry.created_at
    if entry.source_type == CreditLedgerEntry.SOURCE_TOP_UP:
        return 3, entry.created_at
    return 4, entry.created_at


def grant_top_up(
    user: User,
    *,
    credits: int,
    source_identifier: str,
    metadata: dict | None = None,
    source_type: str = CreditLedgerEntry.SOURCE_TOP_UP,
):
    if credits <= 0:
        return
    with transaction.atomic():
        CreditLedgerEntry.objects.create(
            user=user,
            cycle=None,
            source_type=source_type,
            source_identifier=source_identifier,
            amount=credits,
            remaining_amount=credits,
            metadata=metadata or {},
        )


def apply_plan_upgrade(user: User, new_plan: PlanTier, *, now=None) -> int:
    """Switch the user's plan immediately and grant prorated credits."""

    now = now or timezone.now()
    with transaction.atomic():
        meta = ensure_account_meta(user)
        old_plan = meta.plan or get_default_plan()
        if new_plan == old_plan:
            return 0
        cycle = ensure_subscription_state(user, now=now)
        bonus = _calculate_prorated_bonus(meta, old_plan, new_plan, now)
        meta.plan = new_plan
        meta.plan_started_at = now
        meta.pending_plan = None
        meta.save(update_fields=["plan", "plan_started_at", "pending_plan", "updated_at"])
        if bonus > 0:
            CreditLedgerEntry.objects.create(
                user=user,
                cycle=cycle,
                source_type=CreditLedgerEntry.SOURCE_ADJUSTMENT,
                source_identifier=f"plan-upgrade:{new_plan.slug}",
                amount=bonus,
                remaining_amount=bonus,
                metadata={"reason": "plan_upgrade"},
            )
        return bonus


def schedule_plan_downgrade(user: User, target_plan: PlanTier):
    meta = ensure_account_meta(user)
    meta.pending_plan = target_plan
    meta.save(update_fields=["pending_plan", "updated_at"])


def _calculate_prorated_bonus(meta: UserAccountMeta, old_plan: PlanTier, new_plan: PlanTier, now) -> int:
    if new_plan.monthly_credits <= old_plan.monthly_credits:
        return 0
    next_billing = meta.next_billing_at or (meta.current_cycle_started_at or now) + billing_period()
    remaining = max((next_billing - now).total_seconds(), 0)
    total_period = billing_period().total_seconds()
    if total_period <= 0:
        return new_plan.monthly_credits - old_plan.monthly_credits
    fraction = Decimal(remaining) / Decimal(total_period)
    delta = new_plan.monthly_credits - old_plan.monthly_credits
    bonus = int((fraction * Decimal(delta)).quantize(Decimal("1."), rounding="ROUND_HALF_UP"))
    return max(bonus, 0)
