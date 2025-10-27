from __future__ import annotations

import secrets
import uuid
from datetime import timedelta
from typing import Iterable

import jwt
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework import exceptions

from .models import (
    AuditLog,
    RefreshToken,
    Role,
    User,
    UserAccountMeta,
    UserRole,
    collect_permission_codes,
)

ACCESS_TOKEN_LIFETIME = timedelta(minutes=getattr(settings, "ACCOUNTS_ACCESS_TOKEN_LIFETIME_MINUTES", 15))
REFRESH_TOKEN_LIFETIME = timedelta(days=getattr(settings, "ACCOUNTS_REFRESH_TOKEN_LIFETIME_DAYS", 14))
ROTATE_REFRESH = getattr(settings, "ACCOUNTS_ROTATE_REFRESH_TOKENS", True)


def _client_meta(request):
    if not request:
        return "", ""
    user_agent = request.META.get("HTTP_USER_AGENT", "")[:255]
    ip = request.META.get("HTTP_X_FORWARDED_FOR", "") or request.META.get("REMOTE_ADDR", "")
    if ip and "," in ip:
        ip = ip.split(",")[0].strip()
    return user_agent, ip[:45]


def issue_login_tokens(user: User, request=None) -> dict:
    """Generate a full set of tokens for the user."""

    memberships = list(user.active_memberships())
    roles = [m.role for m in memberships]
    permissions = collect_permission_codes(roles)
    meta = getattr(user, "account_meta", None)
    if meta is None:
        meta, _ = UserAccountMeta.objects.get_or_create(user=user)
    access_token, access_exp, payload = _generate_access_token(user, roles, permissions, meta)
    refresh_token, refresh_obj = _create_refresh_token(user, meta, request=request)
    AuditLog.record(action="login.success", user=user, metadata={"roles": [r.slug for r in roles]})
    return {
        "access_token": access_token,
        "access_token_expires_at": access_exp.isoformat(),
        "refresh_token": refresh_token,
        "refresh_token_expires_at": refresh_obj.expires_at.isoformat(),
        "token_type": "Bearer",
        "permissions": sorted(permissions),
        "roles": [r.slug for r in roles],
        "mfa_required": payload["mfa_required"],
        "mfa_enrolled": payload["mfa_enrolled"],
    }


def _generate_access_token(user: User, roles: list[Role], permissions: set[str], meta: UserAccountMeta):
    now = timezone.now()
    exp = now + ACCESS_TOKEN_LIFETIME
    requires_mfa = any(role.requires_mfa for role in roles)
    payload = {
        "iss": "pastpaper-rank",
        "sub": str(user.pk),
        "email": user.email,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
        "jti": uuid.uuid4().hex,
        "type": "access",
        "roles": [role.slug for role in roles],
        "permissions": sorted(permissions),
        "token_version": meta.token_version,
        "mfa_required": requires_mfa,
        "mfa_enrolled": bool(meta.mfa_enrolled_at),
    }
    token = jwt.encode(payload, settings.SECRET_KEY, algorithm="HS256")
    return token, exp, payload


def _create_refresh_token(user: User, meta: UserAccountMeta, request=None):
    raw_token = secrets.token_urlsafe(48)
    user_agent, ip_address = _client_meta(request)
    expires_at = timezone.now() + REFRESH_TOKEN_LIFETIME
    token_obj = RefreshToken.objects.create(
        user=user,
        token_hash=RefreshToken.hash_token(raw_token),
        expires_at=expires_at,
        token_version=meta.token_version,
        user_agent=user_agent,
        ip_address=ip_address,
    )
    return raw_token, token_obj


def validate_refresh_token(raw_token: str) -> RefreshToken:
    if not raw_token:
        raise exceptions.AuthenticationFailed("Refresh token missing")
    token_hash = RefreshToken.hash_token(raw_token)
    token = (
        RefreshToken.objects.select_related("user", "user__account_meta")
        .filter(token_hash=token_hash)
        .first()
    )
    if not token:
        raise exceptions.AuthenticationFailed("Invalid refresh token")
    if token.revoked_at:
        raise exceptions.AuthenticationFailed("Refresh token revoked")
    if token.is_expired():
        raise exceptions.AuthenticationFailed("Refresh token expired")
    if not token.user.is_active:
        raise exceptions.AuthenticationFailed("User disabled")
    if token.user.account_meta.token_version != token.token_version:
        raise exceptions.AuthenticationFailed("Token no longer valid")
    token.last_used_at = timezone.now()
    token.save(update_fields=["last_used_at"])
    return token


def rotate_refresh_token(refresh_token: RefreshToken, request=None) -> tuple[str, RefreshToken]:
    refresh_token.mark_revoked()
    AuditLog.record(action="token.revoked", user=refresh_token.user, metadata={"jti": str(refresh_token.jti)})
    return _create_refresh_token(refresh_token.user, refresh_token.user.account_meta, request=request)


def refresh_login_tokens(raw_token: str, request=None) -> dict:
    stored_token = validate_refresh_token(raw_token)
    meta = stored_token.user.account_meta
    memberships = list(stored_token.user.active_memberships())
    roles = [m.role for m in memberships]
    permissions = collect_permission_codes(roles)
    access_token, access_exp, payload = _generate_access_token(stored_token.user, roles, permissions, meta)
    if ROTATE_REFRESH:
        refresh_token, refresh_obj = rotate_refresh_token(stored_token, request=request)
    else:
        refresh_token, refresh_obj = raw_token, stored_token
    AuditLog.record(action="token.refresh", user=stored_token.user, metadata={"jti": str(stored_token.jti)})
    return {
        "access_token": access_token,
        "access_token_expires_at": access_exp.isoformat(),
        "refresh_token": refresh_token,
        "refresh_token_expires_at": refresh_obj.expires_at.isoformat(),
        "token_type": "Bearer",
        "permissions": sorted(permissions),
        "roles": [r.slug for r in roles],
        "mfa_required": payload["mfa_required"],
        "mfa_enrolled": payload["mfa_enrolled"],
    }


def revoke_refresh_token(raw_token: str):
    try:
        stored = validate_refresh_token(raw_token)
    except exceptions.AuthenticationFailed:
        return
    stored.mark_revoked()
    AuditLog.record(action="token.revoked", user=stored.user, metadata={"jti": str(stored.jti)})


def decode_access_token(token: str, verify_exp: bool = True) -> dict:
    if not token:
        raise exceptions.AuthenticationFailed("Missing access token")
    options = {"verify_exp": verify_exp}
    try:
        payload = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=["HS256"],
            options=options,
        )
    except jwt.ExpiredSignatureError as exc:
        raise exceptions.AuthenticationFailed("Access token expired") from exc
    except jwt.InvalidTokenError as exc:
        raise exceptions.AuthenticationFailed("Invalid access token") from exc
    if payload.get("type") != "access":
        raise exceptions.AuthenticationFailed("Incorrect token type")
    return payload


def assign_role(user: User, role: Role, *, actor: User | None = None, note: str = "", expires_at=None) -> UserRole:
    with transaction.atomic():
        membership, created = UserRole.objects.update_or_create(
            user=user,
            role=role,
            defaults={"is_active": True, "note": note, "assigned_by": actor, "expires_at": expires_at},
        )
        user.clear_cached_permissions()
        AuditLog.record(
            action="role.assigned",
            user=user,
            actor=actor,
            metadata={"role": role.slug, "created": created},
        )
        return membership


def revoke_role(user: User, role: Role, *, actor: User | None = None, note: str = ""):
    updated = (
        UserRole.objects.filter(user=user, role=role, is_active=True)
        .update(is_active=False, note=note or "", updated_at=timezone.now())
    )
    if updated:
        user.clear_cached_permissions()
        AuditLog.record(
            action="role.revoked",
            user=user,
            actor=actor,
            metadata={"role": role.slug},
        )


def serialize_memberships(user: User) -> list[dict]:
    memberships = user.active_memberships()
    return [
        {
            "role": membership.role.slug,
            "role_name": membership.role.name,
            "expires_at": membership.expires_at.isoformat() if membership.expires_at else None,
            "assigned_by": membership.assigned_by_id,
        }
        for membership in memberships
    ]
