# TODO: Write a auto-documentation module
import uuid
from typing import Any, Dict, Set, Optional, Callable, Iterable, Tuple, TypedDict
import time
import logging
from django.conf import settings
from channels.generic.websocket import AsyncJsonWebsocketConsumer

from backend.apps.service.realtime.replay import ReplayStore, NullReplayStore
from backend.apps.service.realtime.auth import verify_subscription, mint_token
from backend.apps.service.realtime.types import ProgressEvent

logger = logging.getLogger(__name__)


# class ProgressStatus(str, Enum):
#     STARTED = "started"  # 任务开始
#     MESSAGE = "message"  # 普通中间输出
#     FINISHED = "finished"  # 任务完成
#     ERROR = "error"  # 出错终止
#
#
# class ProgressEvent(TypedDict):
#     type: # Literal["progress"]  # 固定为 progress
#     rid: str  # 资源 ID（订阅 ID）
#     status: ProgressStatus  # 状态码（枚举）
#     seq: int  # 单 rid 内递增序号
#     ts: float  # 事件时间戳（Unix 秒）
#     msg: Optional[str]
#     progress: Optional[float]
#     data: Optional[Dict[str, Any]]  # 附加业务数据
#     meta: Optional[Dict[str, Any]]  # 调试/跟踪元数据

class Subscribe(TypedDict, total=False):
    rid: str
    token: str
    last_seq: Optional[int]
    user_id: Optional[int]


def _now() -> float:
    return time.time()


def _generate_rid():
    return uuid.uuid4().hex


def action(_fn: Optional[Callable] = None, *, name: Optional[str] = None, alias: Iterable[str] = ()):
    def _decorate(fn: Callable):
        base = name or fn.__name__
        setattr(fn, "_ws_action", True)
        setattr(fn, "_ws_action_name", base)
        setattr(fn, "_ws_action_alias", tuple(alias) if alias else tuple())
        return fn

    if callable(_fn):
        return _decorate(_fn)
    return _decorate


def _snake_to_kebab(s: str) -> str:
    return s.replace("_", "-")


def construct_group_name(topic, rid):
    return f"{settings.CHANNEL_GROUP_PREFIX}_{topic}-{rid}"


def _class_namespace(cls_name: str) -> str:
    n = cls_name
    if n.endswith("Consumer"):
        n = n[:-8]
    return n.lower() or "ws"


class SubscriptionConsumer(AsyncJsonWebsocketConsumer):
    """
    可继承基类：
      - 子类用 @action 注册方法；无需重写 receive_json
      - 动作名既支持 "name" 也支持 "namespace.name"
      - 内建 subscribe / unsubscribe / ping
      - 支持补播与事件透传（默认处理 type="progress"）
    """
    topic: str = "resource"
    replay_store: ReplayStore = NullReplayStore()
    allowed_event_types: Optional[Iterable[str]] = None
    require_authenticated: bool = False
    max_subscriptions: Optional[int] = None

    subscribed_ids: Set[str]
    action_map: Dict[str, str]
    namespace: str

    @classmethod
    def build_aliases(cls):
        for attr_name, fn in cls.__dict__.items():
            if callable(fn) and getattr(fn, "_ws_action", False):
                base = getattr(fn, "_ws_action_name") or attr_name
                kebab = _snake_to_kebab(base)
                aliases = set(getattr(fn, "_ws_action_alias") or ())

                keys = {
                    base,
                    kebab,
                    f"{cls.namespace}.{base}",
                    f"{cls.namespace}.{kebab}",
                }
                for a in aliases:
                    keys.add(a)
                    keys.add(f"{cls.namespace}.{a}")

                for key in keys:
                    if key in cls.action_map and cls.action_map[key] != attr_name:
                        logger.warning("Action key '%s' overridden by %s.%s (was %s)",
                                       key, cls.__name__, attr_name, cls.action_map[key])
                    cls.action_map[key] = attr_name

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        parent_map = getattr(cls, "action_map", {}) or {}
        cls.action_map = dict(parent_map)
        cls.namespace = _class_namespace(cls.__name__)
        cls.build_aliases()
        for builtin in ("subscribe", "unsubscribe", "ping"):
            if callable(getattr(cls, builtin, None)):
                for key in (builtin, f"{cls.namespace}.{builtin}"):
                    if key not in cls.action_map:
                        cls.action_map[key] = builtin

    async def connect(self):
        if self.require_authenticated and not self._is_authenticated():
            await self.close(code=4403)
            return
        self.subscribed_ids = set()
        await self.accept()
        await self.replay_store.open()

    async def disconnect(self, code):
        for rid in list(self.subscribed_ids):
            await self.channel_layer.group_discard(self._group(rid), self.channel_name)
        self.subscribed_ids.clear()
        await self.replay_store.close()

    async def receive_json(self, content: Dict[str, Any], **kwargs):
        action_name = content.get("action")
        if not action_name:
            return await self._send_error("action_required")

        method_name = self.action_map.get(action_name)
        if not method_name:
            return await self._send_error(f"unknown_action:{action_name}")

        try:
            method = getattr(self, method_name)
            payload = {k: v for k, v in content.items() if k != "action"}
            await method(payload)
        except Exception as e:
            await self._send_error(f"server_error:{e}")
            raise e

    @action()
    async def subscribe(self, payload: Subscribe):
        rid = payload.get("rid")
        token = payload.get("token")
        last_seq = payload.get("last_seq")
        user_id = payload.get("user_id")

        if not rid or not token:
            logger.info("Receiving new subscription")
            rid = _generate_rid()
            token = mint_token(resource_id=rid, user_id=user_id)
        else:
            logger.info("Processing existed subscription")

        if self.max_subscriptions and len(self.subscribed_ids) >= self.max_subscriptions:
            return await self._send_error("too_many_subscriptions")

        ok, err = await self._verify(rid, token)
        if not ok:
            return await self._send_error(err or "unauthorized")

        logger.info(f"Group name: {self._group(rid)}, {self.channel_name}")
        await self.channel_layer.group_add(self._group(rid), self.channel_name)
        self.subscribed_ids.add(rid)
        await self._send_json({"type": "subscribed", "rid": rid, "ts": _now()})
        await self.on_after_subscribe(rid)

        events = await self.replay_store.read_recent(rid, last_seq=last_seq)
        for ev in events:
            await self._emit_event(ev, is_replay=True)

    @action()
    async def unsubscribe(self, payload: Dict[str, Any]):
        rid = payload.get("rid")
        if rid and rid in self.subscribed_ids:
            await self.channel_layer.group_discard(self._group(rid), self.channel_name)
            self.subscribed_ids.discard(rid)
            await self._send_json({"type": "unsubscribed", "rid": rid, "ts": _now()})
            await self.on_after_unsubscribe(rid)
        else:
            await self._send_error("not_subscribed")

    @action()
    async def ping(self, payload: Dict[str, Any]):
        await self._send_json({"type": "pong", "ts": _now()})

    async def progress(self, event: ProgressEvent):
        """We only allow worker to communicate to the consumer through `progress`"""
        await self._emit_event(event)

    async def _verify(self, resource_id: str, token: str) -> Tuple[bool, Optional[str]]:
        user = self.scope.get("user")
        return verify_subscription(resource_id, token, user)

    async def on_after_subscribe(self, rid: str):
        ...

    async def on_after_unsubscribe(self, rid: str):
        ...

    async def _emit_event(self,
                          event: ProgressEvent,
                          is_replay: bool = False):
        data = event.get("data", {})

        if event.get("ts") is not None:
            data["ts"] = float(event.get("ts"))
        elif "ts" in data:
            data["ts"] = float(data["ts"])
        else:
            data["ts"] = _now()

        rid = event.get("rid")
        if rid and not is_replay and self.replay_store:  # Not writing the event when replaying
            try:
                self.replay_store.write(rid, data)
            except Exception as e:
                logging.exception(f"Failed to write replay for {rid}: {e}")

        await self._send_json(data)

    async def _send_json(self, obj: Dict[str, Any]):
        await self.send_json(obj)

    async def _send_error(self, msg: str):
        """Defined for convenience"""
        await self._send_json({"type": "error", "error": msg, "ts": _now()})

    def _group(self, rid: str) -> str:
        return construct_group_name(self.topic, rid)

    def _is_authenticated(self) -> bool:
        user = self.scope.get("user")
        return bool(getattr(user, "is_authenticated", False))
