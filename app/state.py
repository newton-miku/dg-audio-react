"""进程内共享状态（跨 asyncio 任务 + 捕获线程）。

锁内字段很少、写入频率低（~20Hz），用一把 RLock 足够。
"""
from __future__ import annotations

import threading
import asyncio


class State:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.wake = asyncio.Event()  # 状态/配置变化时置位，唤醒 scheduler
        # DG-Lab 连接
        self.bound: bool = False
        self.target: str = ""
        self.dg_running: bool = False
        self.dg_msg: str = ""          # 最近一条连接提示（给 UI）
        # App 实时强度
        self.a: int = 0
        self.b: int = 0
        self.a_limit: int = 0
        self.b_limit: int = 0
        # 是否输出（UI 开始/停止）
        self.enabled: bool = False
        # 音频 / 映射遥测（scheduler 更新）
        self.level_db: float = -120.0
        self.gate_db: float = -120.0
        self.amp: float = 0.0          # 0..1 目标幅度
        self.kick: float = 0.0         # 0..1 节拍叠加强度
        self.outA: float = 0.0         # 实际下发到 A 的幅度 0..1
        self.outB: float = 0.0
        self.bpm: float | None = None
        self.locked: bool = False          # 节拍器是否已锁定
        self.boostA: int = 0               # 自动增强已加值（通道 A）
        self.boostB: int = 0
        # 波形发送状态（诊断）
        self.pulses_sent: int = 0      # 累计下发脉冲条数
        self.wave_on: bool = False     # 最近 ~0.8s 内是否在发波形
        self.last_amp_text: str = ""
        # 错误 / 状态提示（scheduler / dg 写入）
        self.error: str = ""
        self.status: str = ""

    # ---- 便捷快照（web 遥测使用）----
    def snapshot(self) -> dict:
        with self.lock:
            return {
                "bound": self.bound,
                "target": self.target,
                "dg_running": self.dg_running,
                "dg_msg": self.dg_msg,
                "a": self.a,
                "b": self.b,
                "a_limit": self.a_limit,
                "b_limit": self.b_limit,
                "enabled": self.enabled,
                "level_db": round(self.level_db, 1),
                "gate_db": round(self.gate_db, 1),
                "amp": round(self.amp, 3),
                "kick": round(self.kick, 3),
                "outA": round(self.outA, 3),
                "outB": round(self.outB, 3),
                "bpm": self.bpm,
                "locked": self.locked,
                "boostA": self.boostA,
                "boostB": self.boostB,
                "pulses_sent": self.pulses_sent,
                "wave_on": self.wave_on,
                "error": self.error,
                "status": self.status,
            }

    def update(self, **kw) -> None:
        with self.lock:
            for k, v in kw.items():
                if hasattr(self, k):
                    setattr(self, k, v)

    def poke(self) -> None:
        """唤醒 scheduler（任何状态机字段变化后调用）。"""
        self.wake.set()
