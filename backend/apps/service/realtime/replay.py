# realtime/service/replay.py
import json
from django.conf import settings
from typing import Optional, Dict, Any, Iterable

STREAM_PREFIX = settings.STREAM_PREFIX
REDIS_URL = settings.REDIS_URL
REPLAY_MAX = settings.REPLAY_MAX

try:
    import redis.asyncio as aioredis  # 异步给Consumer用
    import redis as sync_redis  # 同步给任务端用
except Exception:
    aioredis = None
    sync_redis = None


class ReplayStore:
    async def open(self): ...

    async def close(self): ...

    async def read_recent(self, resource_id: str, last_seq: Optional[int] = None, limit: int = REPLAY_MAX) -> Iterable[
        Dict[str, Any]]: ...

    def write(self, resource_id: str, payload: Dict[str, Any]): ...


class NullReplayStore(ReplayStore):
    async def open(self): pass

    async def close(self): pass

    async def read_recent(self, resource_id, last_seq=None, limit=REPLAY_MAX):
        return []

    def write(self, resource_id, payload): pass


class RedisStreamsReplayStore(ReplayStore):
    def __init__(self, redis_url: str = REDIS_URL):
        self._async = None
        self._sync = None
        self.redis_url = redis_url

    async def open(self):
        if aioredis:
            self._async = aioredis.from_url(self.redis_url, decode_responses=True)

    async def close(self):
        if self._async:
            await self._async.close()

    def write(self, resource_id: str, payload: Dict[str, Any]):
        if not sync_redis:
            return
        r = sync_redis.Redis.from_url(self.redis_url, decode_responses=True)
        r.xadd(f"{STREAM_PREFIX}{resource_id}", {"payload": json.dumps(payload)},
               maxlen=500, approximate=True)

    async def read_recent(self, resource_id: str, last_seq: Optional[int] = None, limit: int = REPLAY_MAX):
        if not self._async:
            return []
        key = f"{STREAM_PREFIX}{resource_id}"
        items = await self._async.xrevrange(key, count=limit)
        out = []
        for _id, fields in reversed(items):
            raw = fields.get("payload")
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            if last_seq is not None:
                seq = obj.get("seq")
                if isinstance(seq, int) and seq <= last_seq:
                    continue
            out.append(obj)
        return out
