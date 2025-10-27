# realtime/service/auth.py
import time, jwt
from django.conf import settings
from typing import Optional


def default_decode_token(token: str) -> dict:
    secret = getattr(settings, "CHANNELS_WS_SECRET", None) or settings.SECRET_KEY
    return jwt.decode(token, secret, algorithms=["HS256"])


def verify_subscription(resource_id: str, token: str, user=None) -> tuple[bool, Optional[str]]:
    """
    默认策略：
      token.payload = { "id": <resource_id>, "sub": <user_id or None>, "exp": ... }
      若服务端有登录态，则要求 sub == user.id
    由于登陆暂时未实现，所以不验证user相关参数
    """
    try:
        payload = default_decode_token(token)
    except Exception as e:
        return False, f"invalid token: {e}"
    if payload.get("id") != resource_id:
        return False, "token-resource mismatch"
    # if user and getattr(user, "is_authenticated", False):
    #     if str(payload.get("sub")) != str(user.id):
    #         return False, "token-sub mismatch"
    return True, None


def mint_token(resource_id: str, user_id: Optional[int] = None, ttl_seconds: int = 3600) -> str:
    secret = settings.WS_SECRET
    return jwt.encode({"id": resource_id, "sub": user_id, "exp": int(time.time()) + ttl_seconds},
                      secret, algorithm="HS256")
