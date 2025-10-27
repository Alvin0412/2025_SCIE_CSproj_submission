import os, json, contextlib, asyncio
import httpx
from django.conf import settings
import redis.asyncio as aioredis
from backend.apps.service.orchestrators.registry import resolve_future

STREAM = settings.RESULT_STREAM_KEY
GROUP = settings.RESULT_GROUP
PREFIX = settings.RESULT_ROUTE_PREFIX
DEFAULT_CALLBACK = settings.ORCHESTRATOR_CALLBACK_URL


class ResultOrchestrator:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self.consumer_name = settings.RESULT_CONSUMER % {"pid": os.getpid()}
        self._task: asyncio.Task | None = None
        self._running = False
        print(f"ResultOrchestrator initialized with consumer name: {self.consumer_name}")

    async def start(self):
        if self._running:
            print("Attempted to start an already running orchestrator")
            return
        self._running = True
        print(f"Starting orchestrator and connecting to Redis at: {settings.DRAMATIQ_REDIS_URL}")
        self.redis = aioredis.from_url(settings.DRAMATIQ_REDIS_URL, decode_responses=True)
        try:
            await self.redis.xgroup_create(name=STREAM, groupname=GROUP, id="$", mkstream=True)
            print(f"Created or joined Redis stream group: {GROUP}")
        except aioredis.ResponseError as e:
            if "BUSYGROUP" in str(e):
                print(f"Redis group {GROUP} already exists, joining it.")
            else:
                print(f"Failed to create or join Redis group: {e}")
                self._running = False
                if self.redis:
                    await self.redis.close()
                    self.redis = None
                raise
        self._task = asyncio.create_task(self._loop())

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        if self.redis:
            await self.redis.close()

    async def _loop(self):
        while self._running:
            entries = await self.redis.xreadgroup(
                groupname=GROUP,
                consumername=self.consumer_name,
                streams={STREAM: ">"},
                count=64,
                block=5000,
            )
            if not entries:
                # print("no new entries found, continuing loop")

                continue

            for _stream, messages in entries:
                for sid, fields in messages:
                    try:
                        task_id = fields["msg_id"]
                        payload = json.loads(fields["payload"])
                        print(f"Processing task_id: {task_id}")
                    except Exception as e:
                        print(f"Failed to parse message with sid {sid}: {e}")
                        await self.redis.xack(STREAM, GROUP, sid)
                        continue

                    try:
                        route = await self.redis.hgetall(f"{PREFIX}{task_id}")
                        if route and "callback_url" in route:
                            print(f"Delivering task {task_id} to HTTP endpoint: {route['callback_url']}")
                            await self._push_http(route["callback_url"], task_id, payload)
                        else:
                            print(f"Delivering task {task_id} locally")
                            await self._deliver_local(task_id, payload)
                    except Exception as deliver_error:
                        print(f"Failed to deliver task {task_id}: {deliver_error}")
                        continue

                    print(f"Acknowledging message with sid {sid}")
                    await self.redis.delete(f"{PREFIX}{task_id}")
                    await self.redis.xack(STREAM, GROUP, sid)

    async def _deliver_local(self, task_id: str, payload: dict):
        if payload.get("exc"):
            print(f"Task {task_id} encountered an error: {payload.get('exc')}")
            resolve_future(task_id, Exception(payload["exc"]), is_error=True)
        else:
            print(f"Delivering task {task_id} result: {payload.get('v')}")
            resolve_future(task_id, payload.get("v"))

    async def _push_http(self, url: str, task_id: str, payload: dict):
        print(f"Pushing task {task_id} result to HTTP URL: {url or DEFAULT_CALLBACK}")
        body = {"task_id": task_id, "result": payload.get("v"), "error": payload.get("exc")}
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(url or DEFAULT_CALLBACK, json=body)
