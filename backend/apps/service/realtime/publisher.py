from typing import Any, Dict, Optional

from channels.layers import get_channel_layer

from backend.apps.service.realtime.types import ProgressEvent, ProgressStatus
from backend.apps.service.realtime.consumer import _now, construct_group_name


class ProgressPublisher:
    def __init__(self, rid, topic: str = "resource"):
        self.topic = topic
        self.layer = get_channel_layer()
        self.rid = rid

    async def _send(self, ev: ProgressEvent) -> None:
        ev.setdefault("type", "progress")
        ev.setdefault("rid", self.rid)
        ev.setdefault("ts", _now())
        await self.layer.group_send(construct_group_name(self.topic, self.rid), ev)

    async def started(self, msg: Optional[str] = None, data: Optional[Dict[str, Any]] = None):
        await self._send({"status": ProgressStatus.STARTED, "msg": msg, "data": data or {}})

    async def message(self, msg: Optional[str] = None, data: Optional[Dict[str, Any]] = None,
                      progress: Optional[float] = None):
        ev: ProgressEvent = {"status": ProgressStatus.MESSAGE, "data": data or {}}
        if msg is not None:
            ev["msg"] = msg
        if progress is not None:
            ev["progress"] = progress
        await self._send(ev)

    async def finished(self, msg: Optional[str] = None, data: Optional[Dict[str, Any]] = None):
        await self._send({"status": ProgressStatus.FINISHED, "msg": msg, "data": data or {}})

    async def error(self, msg: Optional[str] = None, data: Optional[Dict[str, Any]] = None,
                    meta: Optional[Dict[str, Any]] = None):
        ev: ProgressEvent = {"status": ProgressStatus.ERROR}
        if msg is not None:
            ev["msg"] = msg
        if data is not None:
            ev["data"] = data
        if meta is not None:
            ev["meta"] = meta
        await self._send(ev)
