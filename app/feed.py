"""捕获线程 -> 分析端的线程安全电平通道。

每次音频块只算一个 dB 值推入，时间戳用 time.monotonic()。
"""
from __future__ import annotations

import threading
import time
from collections import deque


class LevelFeed:
    def __init__(self, maxlen: int = 4096) -> None:
        self.lock = threading.Lock()
        self._q: deque = deque(maxlen=maxlen)

    def push(self, db: float) -> None:
        with self.lock:
            self._q.append((time.monotonic(), db))

    def drain(self, before_t: float | None = None) -> list[tuple[float, float]]:
        """取出并清空当前缓冲（可选只取 before_t 之前，保留之后的）。"""
        with self.lock:
            if before_t is None:
                items = list(self._q)
                self._q.clear()
                return items
            out = []
            while self._q and self._q[0][0] < before_t:
                out.append(self._q.popleft())
            return out

    def clear(self) -> None:
        with self.lock:
            self._q.clear()

    def __len__(self) -> int:
        with self.lock:
            return len(self._q)
