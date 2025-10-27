import json
import asyncio
import uuid

import redis
from functools import wraps
from typing import Callable, TypeVar, Awaitable, ParamSpec
from django.conf import settings
import dramatiq
from dramatiq.middleware.current_message import CurrentMessage

from backend.apps.service.orchestrators.errors import TaskTimeoutError
from backend.apps.service.orchestrators.registry import register_future

STREAM: str = settings.RESULT_STREAM_KEY
PREFIX: str = settings.RESULT_ROUTE_PREFIX
CALLBACK_URL: str = settings.ORCHESTRATOR_CALLBACK_URL

P = ParamSpec("P")
R = TypeVar("R")


def awaitable_actor(**actor_kwargs) -> Callable[[Callable[P, R]], Callable[P, Awaitable[R]]]:
    def deco(fn: Callable[P, R]) -> Callable[P, Awaitable[R]]:
        r: redis.Redis = redis.from_url(settings.DRAMATIQ_REDIS_URL, decode_responses=True)
        task_name = fn.__name__

        @dramatiq.actor(store_results=True, **actor_kwargs)
        def _actor(*args, **kwargs) -> R:
            print(f"Actor called with these arguments: \n {args} \n {kwargs} \n ")
            msg = CurrentMessage.get_current_message()
            msg_id = msg.message_id
            try:
                print(f"Actor starting execution for message ID: {msg_id}")
                value = fn(*args, **kwargs)
                payload = {"v": value, "exc": None}
                print(f"Actor completed execution successfully for message ID: {msg_id}, result: {value}")
                return value
            except Exception as e:
                payload = {"v": None, "exc": str(e)}
                print(f"Actor failed for message ID: {msg_id}, exception: {str(e)}")
                raise
            finally:
                print(f"Sending payload for message ID: {msg_id} to Redis stream.")
                r.xadd(
                    STREAM,
                    fields={"msg_id": msg_id, "payload": json.dumps(payload)},
                    maxlen=100_000,
                    approximate=True,
                )
                print(f"Payload sent to Redis stream for message ID: {msg_id}")

        @wraps(fn)
        async def wrapper(
                *args: P.args,
                timeout: float = 60.0,
                callback_url: str = CALLBACK_URL,
                **kwargs: P.kwargs,
        ) -> R:
            print(f"Sending actor message with timeout {timeout} and callback URL {callback_url}.")
            
            # 先存储路由信息，防止竞态条件
            rr = redis.from_url(settings.DRAMATIQ_REDIS_URL, decode_responses=True)
            temp_id = f"temp_{uuid.uuid4()}"
            route_key = f"{PREFIX}{temp_id}"
            rr.hset(route_key, mapping={"callback_url": callback_url})

            # 发送任务
            try:
                msg = _actor.send(*args, **kwargs)
            except Exception:
                rr.delete(route_key)
                raise

            # 更新路由信息
            final_key = f"{PREFIX}{msg.message_id}"
            rr.rename(route_key, final_key)
            rr.expire(final_key, int(timeout) + 30)
            print(f"Stored message ID {msg.message_id} with callback URL in Redis.")

            # 注册 Future，包含任务名称和超时信息
            fut: asyncio.Future = register_future(
                msg_id=msg.message_id,
                timeout=timeout,
                callback_url=callback_url,
                task_name=task_name
            )
            
            try:
                print(f"Waiting for future result for message ID: {msg.message_id}.")
                return await asyncio.wait_for(fut, timeout=timeout)  # type: ignore
            except asyncio.TimeoutError as e:
                print(f"Timeout error while waiting for message ID: {msg.message_id}. Attempting to fetch result.")
                try:
                    message = _actor.message(msg.message_id)
                    result = message.get_result(block=True, timeout=timeout)
                    print(f"Successfully fetched result for message ID: {msg.message_id}")
                    return result
                except Exception as ex:
                    print(f"Failed to fetch result for message ID: {msg.message_id}. Exception: {str(ex)}")
                    raise TaskTimeoutError(
                        f"Task {msg.message_id} timed out and could not be fetched from Dramatiq") from e

        wrapper.actor = _actor  # type: ignore[attr-defined]
        return wrapper

    return deco
