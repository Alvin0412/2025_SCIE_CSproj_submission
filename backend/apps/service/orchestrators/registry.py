import asyncio
import time
import threading
import weakref
from typing import Dict, Tuple, Optional
from dataclasses import dataclass
from contextlib import contextmanager


@dataclass
class FutureInfo:
    """Future 的元信息"""
    future: asyncio.Future
    created_time: float
    timeout: float
    callback_url: str
    task_name: str = ""


class FutureRegistry:
    """Future 注册表，支持自动清理和监控"""
    
    def __init__(self, cleanup_interval: float = 60.0, max_age: float = 3600.0):
        self._pending: Dict[str, FutureInfo] = {}
        self._lock = threading.Lock()
        self._cleanup_interval = cleanup_interval
        self._max_age = max_age
        self._running = False
        self._cleanup_thread: Optional[threading.Thread] = None
        self._stats = {
            'total_registered': 0,
            'total_resolved': 0,
            'total_expired': 0,
            'total_errors': 0,
        }
    
    def start_cleanup(self):
        """启动清理线程"""
        if self._running:
            return
        
        self._running = True
        self._cleanup_thread = threading.Thread(target=self._cleanup_loop, daemon=True)
        self._cleanup_thread.start()
        print(f"FutureRegistry cleanup thread started (interval: {self._cleanup_interval}s)")
    
    def stop_cleanup(self):
        """停止清理线程"""
        self._running = False
        if self._cleanup_thread:
            self._cleanup_thread.join(timeout=5.0)
    
    def _cleanup_loop(self):
        """清理循环"""
        while self._running:
            try:
                time.sleep(self._cleanup_interval)
                self._cleanup_expired_futures()
            except Exception as e:
                print(f"Error in cleanup loop: {e}")
    
    def _cleanup_expired_futures(self):
        """清理过期的 Future"""
        current_time = time.time()
        expired_keys = []
        
        with self._lock:
            for msg_id, info in self._pending.items():
                age = current_time - info.created_time
                if age > self._max_age:
                    expired_keys.append(msg_id)
            
            # 清理过期的 Future
            for msg_id in expired_keys:
                info = self._pending.pop(msg_id)
                if not info.future.done():
                    info.future.set_exception(
                        asyncio.TimeoutError(f"Future {msg_id} expired after {self._max_age}s")
                    )
                self._stats['total_expired'] += 1
        
        if expired_keys:
            print(f"Cleaned up {len(expired_keys)} expired futures")
    
    def register_future(self, msg_id: str, timeout: float = 60.0, 
                       callback_url: str = "", task_name: str = "") -> asyncio.Future:
        """注册一个新的 Future"""
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        
        info = FutureInfo(
            future=future,
            created_time=time.time(),
            timeout=timeout,
            callback_url=callback_url,
            task_name=task_name
        )
        
        with self._lock:
            self._pending[msg_id] = info
            self._stats['total_registered'] += 1
        
        # 设置超时清理
        loop.call_later(timeout + 30, self._cleanup_single_future, msg_id)
        
        return future
    
    def _cleanup_single_future(self, msg_id: str):
        """清理单个 Future（超时后）"""
        with self._lock:
            info = self._pending.pop(msg_id, None)
            if info and not info.future.done():
                info.future.set_exception(
                    asyncio.TimeoutError(f"Future {msg_id} timed out after {info.timeout}s")
                )
                self._stats['total_expired'] += 1
    
    def resolve_future(self, msg_id: str, value, *, is_error: bool = False):
        """解析 Future"""
        with self._lock:
            info = self._pending.pop(msg_id, None)
            if info and not info.future.done():
                if is_error:
                    info.future.set_exception(value)
                    self._stats['total_errors'] += 1
                else:
                    info.future.set_result(value)
                    self._stats['total_resolved'] += 1
    
    def get_stats(self) -> Dict[str, int]:
        """获取统计信息"""
        with self._lock:
            stats = self._stats.copy()
            stats['current_pending'] = len(self._pending)
        return stats
    
    def get_pending_futures(self) -> Dict[str, Dict]:
        """获取当前待处理的 Future 信息（用于调试）"""
        current_time = time.time()
        with self._lock:
            return {
                msg_id: {
                    'age': current_time - info.created_time,
                    'timeout': info.timeout,
                    'callback_url': info.callback_url,
                    'task_name': info.task_name,
                    'done': info.future.done(),
                    'cancelled': info.future.cancelled(),
                }
                for msg_id, info in self._pending.items()
            }
    
    @contextmanager
    def temporary_future(self, msg_id: str, timeout: float = 60.0):
        """临时 Future 上下文管理器"""
        future = self.register_future(msg_id, timeout)
        try:
            yield future
        finally:
            # 如果 Future 还没有被解析，自动清理
            if not future.done():
                self.resolve_future(msg_id, asyncio.CancelledError("Context manager cleanup"))


# 全局实例
_registry = FutureRegistry()


def register_future(msg_id: str, timeout: float = 60.0, 
                   callback_url: str = "", task_name: str = "") -> asyncio.Future:
    """注册 Future 的便捷函数"""
    return _registry.register_future(msg_id, timeout, callback_url, task_name)


def resolve_future(msg_id: str, value, *, is_error: bool = False):
    """解析 Future 的便捷函数"""
    _registry.resolve_future(msg_id, value, is_error=is_error)


def get_registry_stats() -> Dict[str, int]:
    """获取注册表统计信息"""
    return _registry.get_stats()


def get_pending_futures() -> Dict[str, Dict]:
    """获取待处理的 Future 信息"""
    return _registry.get_pending_futures()


def start_cleanup():
    """启动清理机制"""
    _registry.start_cleanup()


def stop_cleanup():
    """停止清理机制"""
    _registry.stop_cleanup()
