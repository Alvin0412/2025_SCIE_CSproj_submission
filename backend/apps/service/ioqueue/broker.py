import abc
import asyncio
import logging
import pickle
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import IOJob
from .registry import memory_queue_url, memory_queue_key

DEFAULT_VISIBILITY_SEC = getattr(settings, "IOQUEUE_VISIBILITY_TIMEOUT_SEC", 300)
DEFAULT_POLL_INTERVAL = getattr(settings, "IOQUEUE_POLL_INTERVAL_SEC", 0.5)
MEMORY_BLPOP_TIMEOUT = getattr(settings, "IOQUEUE_MEMORY_BLPOP_TIMEOUT_SEC", 5)

logger = logging.getLogger(__name__)

try:
    import redis.asyncio as aioredis
except ImportError:  # pragma: no cover - optional dependency guard
    aioredis = None


class IOQueueBaseBroker(abc.ABC):
    @abc.abstractmethod
    async def fetch_loop(self, out_queue: "asyncio.Queue"):
        pass


class DBBroker(IOQueueBaseBroker):
    """从数据库抓取任务，放入 runner 主队列"""

    def __init__(self, worker_id: str):
        self.worker_id = worker_id

    async def fetch_loop(self, out_queue: "asyncio.Queue"):
        while True:
            got = await asyncio.to_thread(self._fetch_once, out_queue)
            if not got:
                await asyncio.sleep(DEFAULT_POLL_INTERVAL)

    def _fetch_once(self, out_queue: "asyncio.Queue") -> bool:
        now = timezone.now()
        with transaction.atomic():
            job = (
                IOJob.objects
                .select_for_update(skip_locked=True)
                .filter(status="pending", scheduled_at__lte=now)
                .order_by("queued_at")
                .first()
            )
            if not job:
                job = (
                    IOJob.objects
                    .select_for_update(skip_locked=True)
                    .filter(status="running", visible_until__lte=now)
                    .order_by("picked_at")
                    .first()
                )
            if not job:
                return False

            job.status = "running"
            job.picked_at = now
            job.visible_until = now + timedelta(seconds=DEFAULT_VISIBILITY_SEC)
            job.picked_by = self.worker_id
            job.save()

        try:
            out_queue.put_nowait(("db", job.id))
            return True
        except asyncio.QueueFull:
            # 内存队列满了，回滚到 pending
            with transaction.atomic():
                j = IOJob.objects.select_for_update().get(id=job.id)
                j.status = "pending"
                j.picked_at = None
                j.visible_until = None
                j.picked_by = ""
                j.save()
            return False

    async def finalize(self, job_id: int, *, ok: bool, result=None, error_msg: str = ""):
        await asyncio.to_thread(self._finalize_sync, job_id, ok, result, error_msg)

    def _finalize_sync(self, job_id: int, ok: bool, result, error_msg: str):
        now = timezone.now()
        with transaction.atomic():
            j = IOJob.objects.select_for_update().get(id=job_id)
            if ok:
                j.status = "done"
                j.result = result
                j.last_error = ""
            else:
                j.attempts += 1
                if j.attempts > j.max_retries:
                    j.status = "error"
                    j.last_error = error_msg[:8000]
                else:
                    delay = min(60, 2 ** (j.attempts - 1))
                    j.status = "pending"
                    j.scheduled_at = now + timedelta(seconds=delay)
                    j.last_error = error_msg[:8000]
            j.picked_at = None
            j.visible_until = None
            j.picked_by = ""
            j.save()


class MemoryBroker(IOQueueBaseBroker):
    """从全局内存队列（非持久化任务）搬运到 runner 主队列"""

    def __init__(self):
        self._redis = None
        self._queue_key = memory_queue_key()
        self._queue_url = memory_queue_url()

    async def _get_client(self):
        if aioredis is None:
            raise RuntimeError("redis.asyncio package is required for memory IO tasks.")
        if self._redis is None:
            self._redis = aioredis.from_url(self._queue_url, decode_responses=False)
        return self._redis

    async def fetch_loop(self, out_queue: "asyncio.Queue"):
        while True:
            try:
                client = await self._get_client()
                data = await client.blpop(self._queue_key, timeout=MEMORY_BLPOP_TIMEOUT)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("MemoryBroker failed to fetch task from redis")
                await asyncio.sleep(DEFAULT_POLL_INTERVAL)
                continue

            if not data:
                await asyncio.sleep(DEFAULT_POLL_INTERVAL)
                continue
            # logger.info("MemoryBroker fetched one task from redis")
            _, payload = data
            try:
                task_tuple = pickle.loads(payload)
            except Exception:
                logger.exception("Failed to deserialize memory task payload")
                continue

            await out_queue.put(("memory", task_tuple))

    async def close(self):
        if self._redis is not None:
            try:
                await self._redis.aclose()
            finally:
                self._redis = None
