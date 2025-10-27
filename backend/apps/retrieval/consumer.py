import asyncio
import logging
from typing import Any, Dict, Optional, TypedDict

from backend.apps.retrieval.runner import RetrievalRunner
from backend.apps.service.realtime.consumer import SubscriptionConsumer, action

logger = logging.getLogger(__name__)


class StartPayload(TypedDict, total=False):
    rid: str
    query: str
    text: str
    options: Dict[str, Any]
    user_id: Optional[int]


class RetrievalConsumer(SubscriptionConsumer):
    topic = "retrieval"
    require_authenticated = False

    def __init__(self):
        super().__init__()
        self.runner = RetrievalRunner()
        self._tasks: dict[str, asyncio.Task] = {}
        logger.info("RetrievalConsumer initialized")

    @action(name="start")
    async def start(self, payload: StartPayload):
        rid = payload.get("rid")
        if not rid:
            return await self._send_error("rid_required")

        query = payload.get("query") or payload.get("text")
        if not query:
            return await self._send_error("query_required")

        resolved_user_id: Optional[int] = None
        user = self.scope.get("user")
        if user and getattr(user, "is_authenticated", False):
            user_pk = getattr(user, "id", None)
            if user_pk is not None:
                resolved_user_id = int(user_pk)
        claimed_user_id = payload.get("user_id")
        if claimed_user_id is not None:
            try:
                claimed_int = int(claimed_user_id)
            except (TypeError, ValueError):
                return await self._send_error("user_invalid")
            if resolved_user_id is None or claimed_int != resolved_user_id:
                return await self._send_error("user_mismatch")

        if rid in self._tasks and not self._tasks[rid].done():
            self._tasks[rid].cancel()

        options = payload.get("options") or {}
        task = asyncio.create_task(
            self.runner.run(
                rid=rid,
                query=str(query),
                user_id=resolved_user_id,
                options=options,
            )
        )
        self._tasks[rid] = task

        def _cleanup(_):
            self._tasks.pop(rid, None)

        task.add_done_callback(_cleanup)
        logger.info("Started retrieval task for rid=%s", rid)

    async def disconnect(self, code):
        await super().disconnect(code)
        for rid, task in list(self._tasks.items()):
            if not task.done():
                task.cancel()
        self._tasks.clear()

    async def _verify(self, resource_id: str, token: str):
        return True, None
