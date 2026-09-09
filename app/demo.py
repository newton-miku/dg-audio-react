"""演示模式：无真实音频设备/无郊狼也能跑通全链路。

用合成电平喂 feed —— 模拟"有节拍的背景乐"：低频底噪 + 周期性重音。
"""
from __future__ import annotations

import math
import random
import threading
import time

from .feed import LevelFeed

PERIOD = 0.6   # 重音周期（≈100 BPM）


class DemoFeed:
    def __init__(self, feed: LevelFeed) -> None:
        self._feed = feed
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.error = ""
        self.running = False
        self.active_dev_label = "演示音源（合成）"

    def start(self) -> None:
        self.stop()
        self._stop.clear()
        self.running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="demo-feed")
        self._thread.start()

    def stop(self) -> None:
        self.running = False
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)
            self._thread = None
        self._feed.clear()

    def _run(self) -> None:
        t0 = time.monotonic()
        rng = random.Random(7)
        while not self._stop.is_set():
            t = time.monotonic() - t0
            ph = t % PERIOD
            db: float
            if ph < 0.14:  # 重音瞬态：迅速回落
                k = math.exp(-ph / 0.035)
                db = -8.0 - 30.0 * (1 - k) - 8.0 * k
            else:          # 底鼓之间的"低频持续 + 细碎噪音"
                wob = 0.5 * math.sin(2 * math.pi * 0.9 * t)
                db = -46.0 + wob + rng.uniform(-2, 2) * 0.3
            self._feed.push(db)
            self._stop.wait(0.012)
