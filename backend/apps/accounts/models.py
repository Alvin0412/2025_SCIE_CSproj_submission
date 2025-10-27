from __future__ import annotations

import hashlib
import uuid
from collections import deque
from typing import Iterable

from django.conf import settings
from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.auth.models import PermissionsMixin
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone
from django.utils.functional import cached_property
from django.utils.translation import gettext_lazy as _


class UserManager(BaseUserManager):
    """Custom user manager that uses email as the primary identifier."""

    use_in_migrations = True

    def _create_user(self, email: str, password: str | None, **extra_fields):
        if not email:
            raise ValueError("Users must have an email address")
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_user(self, email: str, password: str | None = None, **extra_fields):
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(self, email: str, password: str | None = None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        if extra_fields.get("is_staff") is not True or extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_staff=True and is_superuser=True.")
        return self._create_user(email, password, **extra_fields)


class User(AbstractBaseUser, PermissionsMixin):
    """Primary system user stored with UUID identifiers and email login."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(_("email address"), unique=True)
    first_name = models.CharField(max_length=150, blank=True)
    last_name = models.CharField(max_length=150, blank=True)
    is_staff = models.BooleanField(
        _("staff status"),
        default=False,
        help_text=_("Designates whether the user can log into this admin site."),
    )
    is_active = models.BooleanField(
        _("active"),
        default=True,
        help_text=_(
            "Designates whether this user should be treated as active. Unselect this instead of deleting accounts."
        ),
    )
    date_joined = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    objects = UserManager()

    USERNAME_FIELD = "email"
    EMAIL_FIELD = "email"
    REQUIRED_FIELDS: list[str] = []

    class Meta:
        ordering = ["-date_joined"]

    def __str__(self) -> str:
        return self.email

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    def short_name(self) -> str:
        return self.first_name or self.email

    def active_memberships(self):
        """Return active role memberships with prefetch for permissions."""
        now = timezone.now()
        return (
            self.role_memberships.filter(is_active=True)
            .filter(models.Q(expires_at__isnull=True) | models.Q(expires_at__gt=now))
            .select_related("role")
            .prefetch_related("role__permissions", "role__inherits", "role__inherits__permissions")
        )

    @cached_property
    def permission_codes(self) -> set[str]:
        roles = [membership.role for membership in self.active_memberships()]
        return collect_permission_codes(roles)

    def clear_cached_permissions(self):
        self.__dict__.pop("permission_codes", None)

    def has_permission_code(self, code: str) -> bool:
        return code in self.permission_codes

    def has_permission_codes(self, codes: Iterable[str], require_all: bool = True) -> bool:
        codes = set(codes)
        if require_all:
            return codes.issubset(self.permission_codes)
        return bool(self.permission_codes.intersection(codes))

    def bump_token_version(self):
        if hasattr(self, "account_meta"):
            self.account_meta.token_version = models.F("token_version") + 1
            self.account_meta.save(update_fields=["token_version"])
            self.account_meta.refresh_from_db(fields=["token_version"])


class AccessPermission(models.Model):
    """Fine-grained permission that can be attached to roles."""

    code = models.CharField(max_length=120, unique=True)
    name = models.CharField(max_length=150)
    description = models.TextField(blank=True)
    category = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["code"]

    def __str__(self) -> str:
        return self.code


class Role(models.Model):
    """Bundle of permissions that can inherit from other roles."""

    slug = models.SlugField(unique=True, max_length=120)
    name = models.CharField(max_length=150)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    is_system = models.BooleanField(default=False)
    requires_mfa = models.BooleanField(default=False)
    priority = models.PositiveIntegerField(default=100)
    permissions = models.ManyToManyField(AccessPermission, related_name="roles", blank=True)
    inherits = models.ManyToManyField("self", symmetrical=False, related_name="inherited_by", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["priority", "slug"]

    def __str__(self) -> str:
        return self.name

    def resolved_permissions(self) -> set[str]:
        return collect_permission_codes([self])


class UserAccountMeta(models.Model):
    """Security metadata associated with a user."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="account_meta")
    token_version = models.PositiveIntegerField(default=1)
    mfa_enrolled_at = models.DateTimeField(null=True, blank=True)
    mfa_secret = models.CharField(max_length=64, blank=True)
    default_role = models.ForeignKey(Role, null=True, blank=True, on_delete=models.SET_NULL, related_name="default_for")
    plan = models.ForeignKey("PlanTier", null=True, blank=True, on_delete=models.PROTECT, related_name="account_metas")
    pending_plan = models.ForeignKey(
        "PlanTier",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="pending_account_metas",
        help_text="Plan that will activate on the next billing renewal.",
    )
    plan_started_at = models.DateTimeField(null=True, blank=True)
    current_cycle_started_at = models.DateTimeField(null=True, blank=True)
    next_billing_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "User security profile"

    def __str__(self):
        return f"Meta({self.user.email})"

    def bump_token_version(self):
        self.token_version = models.F("token_version") + 1
        self.save(update_fields=["token_version"])
        self.refresh_from_db(fields=["token_version"])


class UserRole(models.Model):
    """Assignment of a role to a user with audit context."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, related_name="role_memberships", on_delete=models.CASCADE)
    role = models.ForeignKey(Role, related_name="memberships", on_delete=models.CASCADE)
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="granted_roles",
    )
    note = models.CharField(max_length=255, blank=True)
    is_active = models.BooleanField(default=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "role"],
                condition=models.Q(is_active=True),
                name="unique_active_role_per_user",
            )
        ]

    def __str__(self) -> str:
        return f"{self.user.email} -> {self.role.slug}"

    @property
    def expired(self) -> bool:
        return bool(self.expires_at and self.expires_at <= timezone.now())


class RefreshToken(models.Model):
    """Stored refresh tokens hashed for revocation checks."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, related_name="refresh_tokens", on_delete=models.CASCADE)
    jti = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    token_hash = models.CharField(max_length=128, unique=True)
    token_version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)
    user_agent = models.CharField(max_length=255, blank=True)
    ip_address = models.CharField(max_length=45, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "expires_at"]),
            models.Index(fields=["token_hash"]),
        ]

    def __str__(self) -> str:
        return f"RefreshToken(user={self.user_id}, jti={self.jti})"

    @staticmethod
    def hash_token(raw_token: str) -> str:
        return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

    def is_expired(self) -> bool:
        return timezone.now() >= self.expires_at

    def mark_revoked(self):
        if not self.revoked_at:
            self.revoked_at = timezone.now()
            self.save(update_fields=["revoked_at"])


class AuditLog(models.Model):
    """Tracks authentication and authorization sensitive events."""

    ACTION_CHOICES = [
        ("login.success", "Successful login"),
        ("login.failure", "Failed login"),
        ("auth.logout", "Logout"),
        ("role.assigned", "Role assigned"),
        ("role.revoked", "Role revoked"),
        ("token.revoked", "Token revoked"),
        ("token.refresh", "Token refreshed"),
        ("user.registered", "User registered"),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="audit_events",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="actor_events",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    action = models.CharField(max_length=64, choices=ACTION_CHOICES)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Audit({self.action})"

    @classmethod
    def record(cls, *, action: str, user=None, actor=None, metadata: dict | None = None):
        return cls.objects.create(action=action, user=user, actor=actor, metadata=metadata or {})


class PlanTier(models.Model):
    """Commercial SaaS plans that drive pricing, concurrency, and monthly credits."""

    slug = models.SlugField(max_length=50, unique=True)
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    monthly_price = models.DecimalField(
        max_digits=9,
        decimal_places=2,
        validators=[MinValueValidator(0)],
        help_text="USD price charged monthly.",
    )
    monthly_credits = models.PositiveIntegerField(help_text="Credits granted each billing cycle.")
    concurrency_limit = models.PositiveSmallIntegerField(default=1)
    is_active = models.BooleanField(default=True)
    is_default = models.BooleanField(default=False, help_text="New accounts start on the default plan.")
    features = models.JSONField(default=list, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["monthly_price"]

    def __str__(self) -> str:
        return f"{self.name} ({self.slug})"


class BillingCycle(models.Model):
    """30-day billing cycles that track allocations and rollover totals."""

    STATUS_CURRENT = "current"
    STATUS_CLOSED = "closed"
    STATUS_CHOICES = [
        (STATUS_CURRENT, "Current"),
        (STATUS_CLOSED, "Closed"),
    ]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, related_name="billing_cycles", on_delete=models.CASCADE)
    plan = models.ForeignKey(PlanTier, related_name="billing_cycles", on_delete=models.PROTECT)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_CURRENT)
    cycle_start = models.DateTimeField()
    cycle_end = models.DateTimeField()
    monthly_allocation = models.PositiveIntegerField()
    rollover_allocation = models.PositiveIntegerField(default=0)
    bonus_allocation = models.PositiveIntegerField(default=0, help_text="Credits added via proration or adjustments.")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-cycle_start"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "cycle_start"],
                name="unique_cycle_per_user_and_start",
            ),
        ]

    @property
    def total_allocation(self) -> int:
        return self.monthly_allocation + self.rollover_allocation + self.bonus_allocation

    def __str__(self) -> str:
        return f"Cycle({self.user_id}, {self.cycle_start:%Y-%m-%d} -> {self.cycle_end:%Y-%m-%d})"


class CreditLedgerEntry(models.Model):
    """Individual credit buckets that can be consumed according to priority rules."""

    SOURCE_MONTHLY = "monthly"
    SOURCE_ROLLOVER = "rollover"
    SOURCE_PROMO = "promo"
    SOURCE_TOP_UP = "top_up"
    SOURCE_ADJUSTMENT = "adjustment"
    SOURCE_CHOICES = [
        (SOURCE_MONTHLY, "Monthly allocation"),
        (SOURCE_ROLLOVER, "Rollover"),
        (SOURCE_PROMO, "Promotional"),
        (SOURCE_TOP_UP, "Top up"),
        (SOURCE_ADJUSTMENT, "Adjustment"),
    ]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, related_name="credit_ledger", on_delete=models.CASCADE)
    cycle = models.ForeignKey(
        BillingCycle,
        null=True,
        blank=True,
        related_name="ledger_entries",
        on_delete=models.SET_NULL,
    )
    source_type = models.CharField(max_length=20, choices=SOURCE_CHOICES)
    source_identifier = models.CharField(max_length=120, blank=True)
    amount = models.PositiveIntegerField()
    remaining_amount = models.PositiveIntegerField()
    expires_at = models.DateTimeField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["user", "remaining_amount"], name="idx_ledger_user_remaining"),
        ]

    def __str__(self) -> str:
        return f"LedgerEntry(user={self.user_id}, type={self.source_type}, remaining={self.remaining_amount})"

    def consume(self, credits: int):
        if credits <= 0:
            return
        if credits > self.remaining_amount:
            raise ValueError("Cannot consume more credits than remain on this entry.")
        self.remaining_amount -= credits
        self.save(update_fields=["remaining_amount", "updated_at"])


class CreditUsageLog(models.Model):
    """Audit log of credit consumption with contextual metadata."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, related_name="credit_usage", on_delete=models.CASCADE)
    cycle = models.ForeignKey(
        BillingCycle,
        null=True,
        blank=True,
        related_name="usage_logs",
        on_delete=models.SET_NULL,
    )
    credits_used = models.PositiveIntegerField()
    reason = models.CharField(max_length=120)
    source_summary = models.JSONField(default=list, blank=True)
    reference_type = models.CharField(max_length=80, blank=True)
    reference_id = models.CharField(max_length=120, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"CreditUsage(user={self.user_id}, credits={self.credits_used})"


def collect_permission_codes(roles: Iterable[Role]) -> set[str]:
    """Resolve the transitive closure of permissions for the provided roles."""

    collected: set[str] = set()
    visited: set[int] = set()
    queue = deque(r for r in roles if r)
    while queue:
        role = queue.popleft()
        if not role or not role.pk or role.pk in visited:
            continue
        visited.add(role.pk)
        for perm in role.permissions.all():
            collected.add(perm.code)
        for inherited in role.inherits.all():
            if inherited and inherited.pk not in visited:
                queue.append(inherited)
    return collected
