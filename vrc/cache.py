"""只读查询的内存 TTL 缓存。

VRChat 官方 API 对频率非常敏感（429 随时可能出现），因此所有只读查询
统一经过 60 秒（可配置）TTL 缓存，缓存键中带入用户标识，保证不同 QQ
用户之间的私有数据（好友列表、收藏等）互不可见。
"""

from __future__ import annotations

import threading
import time
from typing import Any


class TTLCache:
    """线程安全的 TTL 缓存（插件运行在单事件循环里，锁只是防御性设计）。"""

    def __init__(self, ttl: float = 60.0) -> None:
        self._ttl = max(0.05, float(ttl))
        self._store: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    @property
    def ttl(self) -> float:
        return self._ttl

    def get(self, key: str) -> Any | None:
        """命中且未过期返回缓存值，否则返回 None。"""
        now = time.monotonic()
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if now >= expires_at:
                self._store.pop(key, None)
                return None
            return value

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._store[key] = (time.monotonic() + self._ttl, value)

    def clear_prefix(self, prefix: str) -> None:
        """清除某个用户（前缀）的全部缓存，写操作后调用。"""
        with self._lock:
            for key in [k for k in self._store if k.startswith(prefix)]:
                self._store.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._store)
