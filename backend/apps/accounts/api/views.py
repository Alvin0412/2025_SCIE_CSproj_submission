from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from ..models import Role
from ..permissions import HasPermissionCode
from ..serializers import (
    AccountProfileSerializer,
    LoginSerializer,
    LogoutSerializer,
    RefreshSerializer,
    RegisterSerializer,
    RoleAssignmentSerializer,
    RoleRevokeSerializer,
    RoleSerializer,
    UserRoleSerializer,
    UserSerializer,
)
from ..services import (
    assign_role,
    issue_login_tokens,
    refresh_login_tokens,
    revoke_refresh_token,
    revoke_role,
)
from ..subscriptions import credit_snapshot, ensure_account_meta, ensure_subscription_state, get_default_plan

User = get_user_model()


class AuthViewSet(viewsets.GenericViewSet):
    serializer_class = LoginSerializer
    permission_classes = [AllowAny]

    @action(methods=["post"], detail=False, permission_classes=[AllowAny])
    def register(self, request):
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        tokens = issue_login_tokens(user, request)
        payload = {"user": UserSerializer(user).data, **tokens}
        return Response(payload, status=status.HTTP_201_CREATED)

    @action(methods=["post"], detail=False, permission_classes=[AllowAny])
    def login(self, request):
        serializer = LoginSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        user = serializer.validated_data["user"]
        tokens = issue_login_tokens(user, request)
        return Response({"user": UserSerializer(user).data, **tokens})

    @action(methods=["post"], detail=False, permission_classes=[AllowAny])
    def refresh(self, request):
        serializer = RefreshSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        tokens = refresh_login_tokens(serializer.validated_data["refresh_token"], request)
        return Response(tokens)

    @action(methods=["post"], detail=False, permission_classes=[AllowAny])
    def logout(self, request):
        serializer = LogoutSerializer(data=request.data or {})
        serializer.is_valid(raise_exception=True)
        token = serializer.validated_data.get("refresh_token")
        if token:
            revoke_refresh_token(token)
        response = Response({"detail": "Logged out"}, status=status.HTTP_200_OK)
        response.delete_cookie("refresh_token")
        return response

    @action(methods=["get"], detail=False, permission_classes=[IsAuthenticated])
    def me(self, request):
        snapshot = credit_snapshot(request.user)
        meta = ensure_account_meta(request.user)
        plan = meta.plan or get_default_plan()
        payload = {
            "user": request.user,
            "plan": plan,
            "pending_plan": meta.pending_plan,
            "credits": snapshot.__dict__,
            "status": {
                "has_credits": snapshot.total_remaining > 0,
                "pending_plan_change": bool(meta.pending_plan),
            },
        }
        serializer = AccountProfileSerializer(payload)
        return Response(serializer.data)


class AccountStatsViewSet(viewsets.ViewSet):
    permission_classes = [IsAuthenticated]

    def list(self, request):
        now = timezone.now()
        range_param = (request.query_params.get("range") or "billing_cycle").lower()
        start, end = self._resolve_period(range_param, request.user, now)
        payload = {
            "tests_generated": 0,
            "favorites_saved": 0,
            "period": {
                "start": start,
                "end": end,
                "label": range_param,
            },
        }
        return Response(payload)

    def _resolve_period(self, range_param: str, user: User, now):
        if range_param == "7d":
            return now - timedelta(days=7), now
        if range_param == "30d":
            return now - timedelta(days=30), now
        if range_param == "billing_cycle":
            cycle = ensure_subscription_state(user, now=now)
            return cycle.cycle_start, cycle.cycle_end
        if range_param.startswith("custom:"):
            try:
                days = int(range_param.split(":", 1)[1])
                return now - timedelta(days=max(days, 1)), now
            except ValueError:
                pass
        # default fallback
        return now - timedelta(days=30), now


class RoleViewSet(viewsets.ModelViewSet):
    queryset = Role.objects.prefetch_related("permissions").all()
    serializer_class = RoleSerializer
    permission_classes = [HasPermissionCode]

    def get_required_permissions(self, request):
        if self.action in ("list", "retrieve"):
            return ("role.view",)
        return ("role.manage",)


class MembershipViewSet(viewsets.GenericViewSet):
    permission_classes = [HasPermissionCode]

    def get_required_permissions(self, request):
        if self.action == "list":
            return ("role.view",)
        return ("role.assign",)

    def list(self, request):
        user_id = request.query_params.get("user_id")
        if not user_id:
            return Response({"detail": "user_id is required"}, status=status.HTTP_400_BAD_REQUEST)
        user = get_object_or_404(User, pk=user_id)
        serializer = UserRoleSerializer(user.role_memberships.filter(is_active=True), many=True)
        return Response(serializer.data)

    @action(methods=["post"], detail=False, url_path="assign")
    def assign(self, request):
        serializer = RoleAssignmentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = get_object_or_404(User, pk=serializer.validated_data["user_id"])
        role = get_object_or_404(Role, slug=serializer.validated_data["role_slug"])
        membership = assign_role(
            user,
            role,
            actor=request.user if request.user.is_authenticated else None,
            note=serializer.validated_data.get("note", ""),
            expires_at=serializer.validated_data.get("expires_at"),
        )
        data = UserRoleSerializer(membership).data
        return Response(data, status=status.HTTP_201_CREATED)

    @action(methods=["post"], detail=False, url_path="revoke")
    def revoke(self, request):
        serializer = RoleRevokeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = get_object_or_404(User, pk=serializer.validated_data["user_id"])
        role = get_object_or_404(Role, slug=serializer.validated_data["role_slug"])
        if not user.role_memberships.filter(role=role, is_active=True).exists():
            return Response({"detail": "Membership not found"}, status=status.HTTP_404_NOT_FOUND)
        revoke_role(user, role, actor=request.user if request.user.is_authenticated else None)
        return Response(status=status.HTTP_204_NO_CONTENT)
