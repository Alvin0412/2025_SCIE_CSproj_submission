from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.utils.translation import gettext_lazy as _

from .models import (
    AccessPermission,
    AuditLog,
    RefreshToken,
    Role,
    User,
    UserAccountMeta,
    UserRole,
)


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    ordering = ("email",)
    list_display = ("email", "first_name", "last_name", "is_staff", "is_active", "date_joined")
    list_filter = ("is_staff", "is_active")
    search_fields = ("email", "first_name", "last_name")
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        (_("Personal info"), {"fields": ("first_name", "last_name")}),
        (_("Permissions"), {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")}),
        (_("Important dates"), {"fields": ("last_login", "date_joined")}),
    )
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("email", "password1", "password2", "is_staff", "is_superuser"),
            },
        ),
    )


@admin.register(Role)
class RoleAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "priority", "is_active", "requires_mfa")
    search_fields = ("name", "slug", "description")
    filter_horizontal = ("permissions", "inherits")


@admin.register(AccessPermission)
class AccessPermissionAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "category")
    search_fields = ("code", "name")


@admin.register(UserRole)
class UserRoleAdmin(admin.ModelAdmin):
    list_display = ("user", "role", "is_active", "expires_at", "assigned_by")
    autocomplete_fields = ("user", "role", "assigned_by")
    list_filter = ("is_active", "role")


@admin.register(UserAccountMeta)
class UserAccountMetaAdmin(admin.ModelAdmin):
    list_display = ("user", "token_version", "default_role", "mfa_enrolled_at")


@admin.register(RefreshToken)
class RefreshTokenAdmin(admin.ModelAdmin):
    list_display = ("user", "jti", "expires_at", "revoked_at")
    search_fields = ("user__email", "jti")


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("action", "user", "actor", "created_at")
    search_fields = ("action", "user__email", "actor__email")
    list_filter = ("action",)
