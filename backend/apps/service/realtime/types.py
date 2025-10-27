from enum import Enum
from typing import TypedDict, Literal, Optional, Any, Dict


class ProgressStatus(str, Enum):
    STARTED = "started"
    MESSAGE = "message"
    FINISHED = "finished"
    ERROR = "error"


class ProgressEvent(TypedDict, total=False):

    type: Literal["progress"]  # 固定为 progress
    rid: str  # 资源 ID（订阅 ID）
    status: ProgressStatus  # 状态码（枚举）
    seq: int  # 单 rid 内递增序号
    ts: float  # 事件时间戳（Unix 秒）
    msg: Optional[str]
    progress: Optional[float]
    data: Optional[Dict[str, Any]]
    meta: Optional[Dict[str, Any]]
