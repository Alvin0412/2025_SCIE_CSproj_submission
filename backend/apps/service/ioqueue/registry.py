import asyncio
import functools
import hashlib
import json
import logging
import pickle
import time
from typing import Callable, Dict, Optional

from django.conf import settings

from .models import IOJob

logger = logging.getLogger(__name__)

try:
    import redis
except ImportError:  # pragma: no cover - runtime guard for optional dependency
    redis = None

TASK_REGISTRY: Dict[str, Callable] = {}


def _memory_queue_url() -> str:
    default_root = getattr(settings, "REDIS_URL", "redis://localhost:6379")
    return getattr(settings, "IOQUEUE_REDIS_URL", f"{default_root}/5")


def _memory_queue_key() -> str:
    return getattr(settings, "IOQUEUE_REDIS_QUEUE_KEY", "ioqueue:memory")


_sync_memory_client = None


def _get_sync_memory_client():
    global _sync_memory_client
    if _sync_memory_client is None:
        if redis is None:
            raise RuntimeError("redis package is not installed; cannot enqueue memory IO tasks.")
        _sync_memory_client = redis.Redis.from_url(_memory_queue_url(), decode_responses=False)
    return _sync_memory_client


def _enqueue_memory_task(task_name: str, args: tuple, kwargs: dict) -> int:
    client = _get_sync_memory_client()
    payload = pickle.dumps((task_name, args, kwargs))
    try:
        client.rpush(_memory_queue_key(), payload)
        size = client.llen(_memory_queue_key())
    except Exception as exc:  # redis.exceptions.RedisError is a subclass of Exception
        raise RuntimeError(f"Failed to enqueue memory task in redis: {exc}") from exc
    return size


def memory_queue_url() -> str:
    return _memory_queue_url()


def memory_queue_key() -> str:
    return _memory_queue_key()


def _make_dedupe_key(task_name, args, kwargs):
    raw = json.dumps([args, kwargs], separators=(",", ":"), ensure_ascii=False)
    h = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]  # 16 hex = 64 bits
    return f"{task_name}:{h}"[:255]


def _qualname(func: Callable) -> str:
    return f"{func.__module__}.{func.__name__}"




def io_task(
        name: Optional[str] = None,
        *,
        max_retries: int = 3,
        dedupe: bool = False,
        persist: bool = True,
        throttle_interval: float = 0.0,
):
    """
    装饰器：把函数注册成 IO 任务。
    - persist=True（默认）：持久化到数据库，返回 job_id
    - persist=False：不落库，直接进入内存队列，返回 None
    - dedupe 仅在 persist=True 时生效
    """

    def decorator(func: Callable):
        task_name = name or _qualname(func)
        func._io_throttle_interval = throttle_interval
        TASK_REGISTRY[task_name] = func

        def _submit(*args, **kwargs):
            if persist is False:
                # if dedupe or max_retries:
                #     logger.warning("dedupe and max_retries are ignored when persist is False")
                size = _enqueue_memory_task(task_name, args, kwargs)
                # logger.debug(f"memory io task enqueued ({args})", extra={"task": task_name, "queue_size": size})
                return None  # 没有 job_id

            # 持久化任务：写库并返回 job_id
            # 1) JSON 序列化校验
            try:
                payload_args = json.loads(json.dumps(args))
                payload_kwargs = json.loads(json.dumps(kwargs))
            except Exception as e:
                raise ValueError(f"IO-Task args/kwargs must be JSON-serializable: {e}")

            # 2) 去重（可选）
            dedupe_key = ""
            if dedupe:
                dedupe_key = _make_dedupe_key(task_name, payload_args, payload_kwargs)
                existing = IOJob.objects.filter(
                    task_name=task_name,
                    dedupe_key=dedupe_key,
                    status__in=["pending", "running"],
                ).first()
                if existing:
                    return existing.id

            job = IOJob.objects.create(
                task_name=task_name,
                args=payload_args,
                kwargs=payload_kwargs,
                max_retries=max_retries,
                dedupe_key=dedupe_key,
            )
            return job.id

        @functools.wraps(func)
        def submit(*args, **kwargs):
            return func(*args, **kwargs)

        submit.send = _submit
        submit.send.task_name = task_name
        submit.send.persist = persist
        return submit

    return decorator
