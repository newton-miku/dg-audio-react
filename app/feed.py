"""捕获线程 -> 分析端的线程安全电平/频谱通道。

每次音频块算一个 dB 值推入（时间戳 time.monotonic()）；频谱另存"最新一帧"，
供界面画频谱图（不需要历史，只要最新）。
"""
from __future__ import annotations

import threading
import time
from collections import deque


class LevelFeed:
    def __init__(self, maxlen: int = 4096) -> None:
        self.lock = threading.Lock()
        self._q: deque = deque(maxlen=maxlen)
        self._spec: list[float] = []       # 最新一帧频谱（各频段 dB，已归一化）
        self._spec_t: float = 0.0

    def push(self, db: float, low: float = -120.0) -> None:
        """db=全频电平；low=低频电平（底鼓/贝斯，用于音乐节拍检测）。"""
        with self.lock:
            self._q.append((time.monotonic(), db, low))

    def drain(self, before_t: float | None = None) -> list[tuple[float, float, float]]:
        """取出并清空当前缓冲（可选只取 before_t 之前，保留之后的）。元素为 (t, db, low)。"""
        with self.lock:
            if before_t is None:
                items = list(self._q)
                self._q.clear()
                return items
            out = []
            while self._q and self._q[0][0] < before_t:
                out.append(self._q.popleft())
            return out

    def push_spec(self, bands: list[float]) -> None:
        with self.lock:
            self._spec = bands
            self._spec_t = time.monotonic()

    def spec(self, max_age: float = 1.0) -> list[float]:
        """最新频谱；太旧（无数据）则返回空。"""
        with self.lock:
            if not self._spec or (time.monotonic() - self._spec_t) > max_age:
                return []
            return list(self._spec)

    def clear(self) -> None:
        with self.lock:
            self._q.clear()
            self._spec = []

    def __len__(self) -> int:
        with self.lock:
            return len(self._q)
