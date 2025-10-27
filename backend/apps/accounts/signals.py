from __future__ import annotations

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .models import AuditLog, Role, UserAccountMeta, UserRole

User = get_user_model()


@receiver(post_save, sender=User)
def create_user_meta(sender, instance: User, created: bool, **kwargs):
    if not created:
        return
    meta, _ = UserAccountMeta.objects.get_or_create(user=instance)
    default_role_slug = getattr(settings, "ACCOUNTS_DEFAULT_ROLE_SLUG", "viewer")
    role = Role.objects.filter(slug=default_role_slug).first()
    if role:
        meta.default_role = role
        meta.save(update_fields=["default_role"])
        UserRole.objects.get_or_create(user=instance, role=role, defaults={"is_active": True})
    from .subscriptions import ensure_subscription_state

    ensure_subscription_state(instance)
    AuditLog.record(action="user.registered", user=instance, metadata={"email": instance.email})


@receiver(post_save, sender=UserRole)
def refresh_user_permissions(sender, instance: UserRole, **kwargs):
    instance.user.clear_cached_permissions()


@receiver(post_delete, sender=UserRole)
def clear_permissions_on_delete(sender, instance: UserRole, **kwargs):
    instance.user.clear_cached_permissions()
