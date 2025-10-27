from __future__ import annotations

from django.contrib.auth import get_user_model
from rest_framework import authentication, exceptions

from .services import decode_access_token

User = get_user_model()


class JWTAuthentication(authentication.BaseAuthentication):
    """Authenticates users based on signed access tokens."""

    keyword = "Bearer"

    def authenticate(self, request):
        header = authentication.get_authorization_header(request).decode("utf-8")
        if not header:
            return None
        parts = header.split()
        if len(parts) != 2 or parts[0] != self.keyword:
            raise exceptions.AuthenticationFailed("Invalid authorization header")
        token = parts[1]
        payload = decode_access_token(token)
        user = self._get_user(payload)
        self._validate_token_version(user, payload)
        request.auth = payload
        return (user, payload)

    def _get_user(self, payload: dict) -> User:
        user_id = payload.get("sub")
        if not user_id:
            raise exceptions.AuthenticationFailed("Invalid token payload")
        try:
            user = User.objects.select_related("account_meta").get(pk=user_id)
        except User.DoesNotExist as exc:
            raise exceptions.AuthenticationFailed("User not found") from exc
        if not user.is_active:
            raise exceptions.AuthenticationFailed("User disabled")
        return user

    def _validate_token_version(self, user: User, payload: dict):
        token_version = payload.get("token_version")
        if not token_version:
            raise exceptions.AuthenticationFailed("Invalid token metadata")
        meta = getattr(user, "account_meta", None)
        if not meta or meta.token_version != token_version:
            raise exceptions.AuthenticationFailed("Token revoked")
