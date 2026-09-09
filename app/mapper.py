"""声音 -> 刺激幅度/频率 的映射。

一条脉冲 = 100ms，内部 4 段 x 25ms（频率各段独立、强度各段独立）。

面向寸止/节拍器场景：`hybrid/beat` 模式会按节拍器 BPM 锁定相位，
在节拍点上叠加强脉冲（见 Scheduler 里对预测拍点的时间轴排布）。
"""
from __future__ import annotations

from typing import Tuple

from .analyzer import Snapshot

# 每条脉冲内 4 段的频率/强度形状
FOLLOW_SHAPE = (0.80, 1.00, 1.00, 0.85)   # 跟随的轻颤形状
KICK_SHAPE = (1.00, 0.75, 0.50, 0.28)     # 一拍（重音）的衰减形状

# dB 满刻度参考：达到该 dB 时幅度视为 1
TOP_DB = -6.0

STYLES = {
    # key: (显示名, 持续频率Hz, 重音攻击频率Hz)
    "deep": ("低沉撞击", 20, 50),
    "mid": ("标准", 55, 100),
    "tingle": ("高频细麻", 130, 180),
}

MODES = {"follow": "平滑跟随", "beat": "节拍脉冲", "hybrid": "混合"}


def sensitivity_to_exp(sensitivity: float) -> float:
    """灵敏度 0..100 -> 幂曲线 0.7..2.3（越大：小音量更弱、大音量更冲）。"""
    s = min(100.0, max(0.0, float(sensitivity)))
    return 0.7 + s / 100.0 * 1.6


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


class Mapper:
    """有内部状态：最近的重音幅度随时间衰减。scheduler 以约 100ms 节奏调用 step()。"""

    def __init__(self) -> None:
        self.kick_amp = 0.0
        self.last_amp = 0.0

    def reset(self) -> None:
        self.kick_amp = 0.0
        self.last_amp = 0.0

    # 独立的"音量跟随"幅度，供节拍叠加参考
    def calc_follow(self, snap: Snapshot, sensitivity: float) -> float:
        if snap.fresh and snap.gate_db < snap.env_db:
            x = (snap.env_db - snap.gate_db) / (TOP_DB - snap.gate_db)
            if x > 0:
                return _clamp(x, 0.0, 1.0) ** sensitivity_to_exp(sensitivity)
        return 0.0

    def step(self, snap: Snapshot, mode: str, sensitivity: float, style_key: str, dt_s: float) -> dict:
        """返回 {amp, kick, amp_follow, slist, flist, freq_base, kick_hz}。"""
        sustain_hz, kick_hz = STYLES.get(style_key, STYLES["mid"])[1:]
        amp_follow = self.calc_follow(snap, sensitivity)

        # --- 实时检测到的重音触发 ---
        if snap.onset and mode in ("hybrid", "beat"):
            strength = _clamp(snap.onset_db / 9.0, 0.2, 1.0)
            self.kick_amp = max(self.kick_amp, strength)

        # 重音幅度随时间衰减（约 100ms 衰减 ~45%）
        if self.kick_amp > 0.0:
            self.kick_amp *= 0.45 ** (dt_s / 0.10)
            if self.kick_amp < 0.04:
                self.kick_amp = 0.0

        kick = self.kick_amp

        # --- 本脉冲 4 段强度 ---
        if mode == "follow":
            amp_meter = amp_follow
            elems = [amp_follow * s for s in FOLLOW_SHAPE]
        elif mode == "beat":
            amp_meter = kick
            elems = [kick * s for s in KICK_SHAPE]
        else:  # hybrid
            amp_meter = max(amp_follow, kick)
            elems = [
                max(amp_follow * 0.92 * f, kick * k)
                for f, k in zip(FOLLOW_SHAPE, KICK_SHAPE)
            ]

        slist = tuple(int(round(_clamp(e, 0.0, 1.0) * 100)) for e in elems)
        flist: Tuple[int, ...]
        if kick > 0.05 and mode in ("hybrid", "beat"):
            flist = (kick_hz, sustain_hz, sustain_hz, sustain_hz)
        else:
            flist = (sustain_hz,) * 4

        self.last_amp = amp_meter
        return {
            "amp": amp_meter,
            "kick": kick,
            "amp_follow": amp_follow,
            "slist": slist,
            "flist": tuple(int(v) for v in flist),
            "freq_base": sustain_hz,
            "kick_hz": kick_hz,
        }

    def overlay_beat(self, res: dict, beat_amp: float, body_freq: int | None = None) -> Tuple[Tuple[int, ...], Tuple[int, ...]]:
        """把一拍盖在某条 100ms 脉冲上：首 25ms 用攻击频率，其余用动态频率。

        返回 (flist, slist)。body_freq 为空则回落 res 的基准频率。
        """
        a = _clamp(beat_amp, 0.0, 1.0)
        slist = tuple(int(round(_clamp(a * k, 0.0, 1.0) * 100)) for k in KICK_SHAPE)
        body = int(body_freq) if body_freq is not None else res["freq_base"]
        flist = (res["kick_hz"], body, body, body)
        return flist, slist
