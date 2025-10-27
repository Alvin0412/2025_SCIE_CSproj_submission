from __future__ import annotations

from django.contrib.auth import authenticate, get_user_model
from django.utils import timezone
from rest_framework import serializers

from .models import AccessPermission, PlanTier, Role, UserRole

User = get_user_model()


class PermissionSerializer(serializers.ModelSerializer):
    class Meta:
        model = AccessPermission
        fields = ("code", "name", "description", "category")


class RoleSerializer(serializers.ModelSerializer):
    permissions = PermissionSerializer(many=True, read_only=True)

    class Meta:
        model = Role
        fields = (
            "id",
            "slug",
            "name",
            "description",
            "is_active",
            "is_system",
            "requires_mfa",
            "priority",
            "permissions",
        )


class PlanTierSerializer(serializers.ModelSerializer):
    class Meta:
        model = PlanTier
        fields = (
            "slug",
            "name",
            "description",
            "monthly_price",
            "monthly_credits",
            "concurrency_limit",
            "features",
        )


class CreditBreakdownSerializer(serializers.Serializer):
    total_allocated = serializers.IntegerField()
    total_remaining = serializers.IntegerField()
    total_used = serializers.IntegerField()
    promo_remaining = serializers.IntegerField()
    rollover_remaining = serializers.IntegerField()
    monthly_remaining = serializers.IntegerField()
    add_on_remaining = serializers.IntegerField()
    cycle_start = serializers.DateTimeField(allow_null=True)
    cycle_end = serializers.DateTimeField(allow_null=True)
    next_reset_at = serializers.DateTimeField(allow_null=True)


class UserSerializer(serializers.ModelSerializer):
    roles = serializers.SerializerMethodField()
    permissions = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ("id", "email", "first_name", "last_name", "roles", "permissions", "date_joined")

    def get_roles(self, obj: User):
        return [
            {
                "slug": membership.role.slug,
                "name": membership.role.name,
                "expires_at": membership.expires_at.isoformat() if membership.expires_at else None,
            }
            for membership in obj.active_memberships()
        ]

    def get_permissions(self, obj: User):
        return sorted(obj.permission_codes)


class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, min_length=8)

    class Meta:
        model = User
        fields = ("email", "password", "first_name", "last_name")

    def create(self, validated_data):
        password = validated_data.pop("password")
        return User.objects.create_user(password=password, **validated_data)


class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)

    def validate(self, attrs):
        email = attrs.get("email")
        password = attrs.get("password")
        user = authenticate(request=self.context.get("request"), email=email, password=password)
        if not user:
            raise serializers.ValidationError("Invalid credentials")
        if not user.is_active:
            raise serializers.ValidationError("User disabled")
        attrs["user"] = user
        return attrs


class RefreshSerializer(serializers.Serializer):
    refresh_token = serializers.CharField()


class LogoutSerializer(serializers.Serializer):
    refresh_token = serializers.CharField(required=False, allow_blank=True)


class UserRoleSerializer(serializers.ModelSerializer):
    role = RoleSerializer(read_only=True)

    class Meta:
        model = UserRole
        fields = ("id", "role", "is_active", "expires_at", "note", "assigned_by", "created_at")


class RoleAssignmentSerializer(serializers.Serializer):
    user_id = serializers.UUIDField()
    role_slug = serializers.SlugField()
    expires_at = serializers.DateTimeField(required=False, allow_null=True)
    note = serializers.CharField(required=False, allow_blank=True, max_length=255)

    def validate(self, attrs):
        expires_at = attrs.get("expires_at")
        if expires_at and expires_at <= timezone.now():
            raise serializers.ValidationError({"expires_at": "Expiration must be in the future"})
        return attrs


class RoleRevokeSerializer(serializers.Serializer):
    user_id = serializers.UUIDField()
    role_slug = serializers.SlugField()


class AccountProfileSerializer(serializers.Serializer):
    user = UserSerializer()
    plan = PlanTierSerializer()
    pending_plan = PlanTierSerializer(allow_null=True)
    credits = CreditBreakdownSerializer()
    status = serializers.DictField(child=serializers.BooleanField(), required=False)
