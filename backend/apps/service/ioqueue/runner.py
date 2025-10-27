import asyncio
import signal
import importlib
import logging
import time

from django.apps import apps

from typing import Tuple, Any
from django.conf import settings
from .models import IOJob
from .registry import TASK_REGISTRY
from .broker import DBBroker, MemoryBroker
from .warnup import warmup

logger = logging.getLogger(__name__)
MAX_CONCURRENCY = getattr(settings, "IOQUEUE_MAX_CONCURRENCY", 64)
_THROTTLE_GATES = {}


def auto_import_all_tasks():
    for app_config in apps.get_app_configs():
        module_name = f"{app_config.name}.tasks"
        try:
            importlib.import_module(module_name)
        except ModuleNotFoundError:
            # 没有 tasks.py 就跳过
            continue

class _ThrottleGate:
    def __init__(self, interval: float):
        self.interval = interval
        self._lock = asyncio.Lock()
        self._last_time = 0.0

    async def acquire(self):
        async with self._lock:
            now = time.monotonic()
            delta = now - self._last_time
            if delta < self.interval:
                await asyncio.sleep(self.interval - delta)
            self._last_time = time.monotonic()


def get_gate(interval: float):
    if interval <= 0:
        return None
    if interval not in _THROTTLE_GATES:
        _THROTTLE_GATES[interval] = _ThrottleGate(interval)
    return _THROTTLE_GATES[interval]

class IORunner:
    def __init__(self, worker_id: str):
        self.worker_id = worker_id
        self.queue: "asyncio.Queue[Tuple[str, Any]]" = asyncio.Queue(maxsize=MAX_CONCURRENCY * 4)
        self.db = DBBroker(worker_id)
        self.mem = MemoryBroker()
        self.sem = asyncio.Semaphore(MAX_CONCURRENCY)
        self._shutdown = asyncio.Event()
        warmup()

    @staticmethod
    def _load_task_modules():
        # for mod in getattr(settings, "IOQUEUE_TASK_MODULES", []):
        #     importlib.import_module(mod)
        auto_import_all_tasks()

    async def _exec_db_job(self, job_id: int):
        job = await asyncio.to_thread(IOJob.objects.get, id=job_id)
        func = TASK_REGISTRY.get(job.task_name)
        if not func:
            await self.db.finalize(job_id, ok=False, error_msg=f"Task {job.task_name} not found")
            return

        try:
            throttle_interval = getattr(func, "_io_throttle_interval", 0.0)
            gate = get_gate(throttle_interval)
            if gate:
                await gate.acquire()
            async with self.sem:
                if asyncio.iscoroutinefunction(func):
                    result = await func(*job.args, **job.kwargs)
                else:
                    result = await asyncio.to_thread(func, *job.args, **job.kwargs)
            await self.db.finalize(job_id, ok=True, result=result)
        except Exception as e:
            await self.db.finalize(job_id, ok=False, error_msg=str(e))

    async def _exec_memory_task(self, task_tuple):
        task_name, args, kwargs = task_tuple
        func = TASK_REGISTRY.get(task_name)
        if not func:
            logger.warning(f"[MemoryTask] Task {task_name} not found; dropped.")
            return
        try:
            async with self.sem:
                throttle_interval = getattr(func, "_io_throttle_interval", 0.0)
                gate = get_gate(throttle_interval)
                if gate:
                    await gate.acquire()
                if asyncio.iscoroutinefunction(func):
                    await func(*args, **kwargs)
                else:
                    await asyncio.sleep(0.05)
                    await asyncio.to_thread(func, *args, **kwargs)
        except Exception as e:
            logger.error(f"[MemoryTask] {task_name} failed: {e}")

    async def _workers(self):
        pendings = set()
        while not self._shutdown.is_set():
            # logger.info("Waiting for new item in queue, current pendings: %d", len(pendings))
            try:
                item = await asyncio.wait_for(self.queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            # logger.info(f"Got item from queue, current pendings: {len(pendings)}")
            kind, payload = item
            if kind == "db":
                coro = self._exec_db_job(payload)
            else:
                coro = self._exec_memory_task(payload)

            t = asyncio.create_task(coro)
            pendings.add(t)

            def _cleanup(
                    fut: asyncio.Task):  # TODO: test whether it solve the issue of asyncio: Task exception was never retrieved
                try:
                    fut.result()  # 取走异常，避免 "exception was never retrieved"
                except Exception as e:
                    logger.error(f"Task failed: {e}")
                finally:
                    pendings.discard(fut)
                    self.queue.task_done()
                    logger.info(f"Task done for {fut}")

            t.add_done_callback(_cleanup)

        # drain
        if pendings:
            await asyncio.gather(*pendings, return_exceptions=True)

    async def run(self):
        self._load_task_modules()
        logger.info(f"found these IO tasks: {list(TASK_REGISTRY.keys())}")

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._shutdown.set)

        fetch_db = asyncio.create_task(self.db.fetch_loop(self.queue))
        fetch_mem = asyncio.create_task(self.mem.fetch_loop(self.queue))
        workers = asyncio.create_task(self._workers())

        await self._shutdown.wait()

        # 停止拉取
        for t in (fetch_db, fetch_mem):
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass

        await self.mem.close()

        await self.queue.join()
        workers.cancel()
        try:
            await workers
        except asyncio.CancelledError:
            pass
