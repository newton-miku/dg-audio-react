"""实时调度：把映射结果以 100ms 脉冲流喂给 DG-Lab，处理开关/绑定/强度钳制。

模式语义（脉冲始终连续存在，间隔/强度/频率都由声音实时决定）：
- follow  纯跟随：强度与频率都随音量平滑变化（无强调）
- beat    节拍跟随(默认)：连续跟随 + 检测到稳定节拍时，在节拍点上叠一拍强调
- hybrid  连续跟随 + 节拍强调 + 未锁定时音量瞬态(重音)也轻强调

节拍追踪 = analyzer 的周期锁相，仅决定"哪里强调"，不产生固定间隔。
"""
from __future__ import annotations

import asyncio
import time

from pydglab_ws import Channel, StrengthOperationType

from .analyzer import Analyzer
from .feed import LevelFeed
from .mapper import FOLLOW_SHAPE, Mapper
from .state import State

PULSE_S = 0.100
HOLD_S = 0.18          # 前视队列目标（秒）
HOLD_S_LOW = 0.06
RAMP_S = 0.6           # 启动缓升时长
FREQ_MIN = 10
FREQ_MAX = 240


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


# ---------- 加重"拍形"波形表 ----------
# 每项是一串 (距拍点秒数 dt, 电平 0..1) 的线性折线；超出最后时刻 -> 0
HIT_SHAPES: dict = {
    "sharp":   ((0.00, 1.00), (0.10, 0.00)),               # 单击：干脆一击
    "double":  ((0.00, 1.00), (0.07, 0.10), (0.09, 0.60), (0.27, 0.00)),   # 双击：重+回弹
    "triple":  ((0.00, 1.00), (0.06, 0.05), (0.09, 0.50), (0.17, 0.05), (0.20, 0.78), (0.32, 0.00)),  # 三连
    "knead":   ((0.00, 0.92), (0.34, 0.92), (0.50, 0.00)),  # 长揉：持续按压后收
    "swell":   ((0.00, 0.20), (0.13, 0.45), (0.28, 1.00), (0.43, 0.60), (0.62, 0.00)),  # 呼吸渐强再落
}
SHAPE_NAMES = {
    "sharp": "单击", "double": "双击", "triple": "三连", "knead": "长揉", "swell": "呼吸渐强",
}
SHAPE_MAX = 0.70   # 超过拍点这么久就不再加（覆盖一拍窗口）


def _shape_level(shape: str, d: float) -> float:
    """距拍点 d 秒处该拍形的电平（0..1）。"""
    pts = HIT_SHAPES.get(shape, HIT_SHAPES["sharp"])
    if d <= pts[0][0]:
        return pts[0][1]
    for i in range(len(pts) - 1):
        (t0, l0), (t1, l1) = pts[i], pts[i + 1]
        if d <= t1:
            f = (d - t0) / max(1e-6, (t1 - t0))
            return l0 + (l1 - l0) * f
    return 0.0


class Scheduler:
    def __init__(self, cfg, state: State, feed: LevelFeed, capture, dg) -> None:
        self.cfg = cfg
        self.state = state
        self.feed = feed
        self.capture = capture
        self.dg = dg
        self.analyzer = Analyzer()
        self.mapper = Mapper()
        self._q_end: float | None = None
        self._recal_pending = False
        self._last_map_t: float | None = None
        self._enable_t: float | None = None
        self._last_stream = {"A": False, "B": False}
        self._applied = {"A": None, "B": None}
        self._pulses_sent: int = 0
        self._last_send_t: float = 0.0
        self._emitted_beat_t: float = 0.0
        self._onset_amp: float = 0.0   # 未锁定时用于 hybrid 瞬态强调的衰减包络

    # ---------- 外部控制 ----------
    def schedule_recal(self) -> None:
        self._recal_pending = True

    async def _recal(self) -> None:
        self.feed.drain()
        self.analyzer.begin_calibration()
        self._recal_pending = False

    # ---------- 主循环 ----------
    async def run(self) -> None:
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.state.update(error=f"scheduler: {e}")
                await asyncio.sleep(0.5)
            self.state.wake.clear()
            delay = 0.03 if (self.state.enabled and self.state.bound) else 0.25
            try:
                await asyncio.wait_for(self.state.wake.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    # ---------- 单 tick ----------
    async def _tick(self) -> None:
        now = time.monotonic()
        cfg = self.cfg.d
        if self._recal_pending:
            await self._recal()
            now = time.monotonic()

        threshold = float(cfg.get("threshold", 0.0))
        release_tau = max(0.02, float(cfg.get("release_ms", 220)) / 1000.0)
        samples = self.feed.drain()
        snap = self.analyzer.consume(samples, threshold_db=threshold, release_tau=release_tau)
        self.analyzer.idle_check(now)

        bound = self.state.bound
        enabled = bool(self.state.enabled)
        a_on = bound and enabled and bool(cfg.get("chA", True))
        b_on = bound and enabled and bool(cfg.get("chB", True))
        stream = {"A": a_on, "B": b_on}

        for chk in ("A", "B"):
            if self._last_stream[chk] and not stream[chk]:
                await self._clear(chk)
        self._last_stream = dict(stream)

        if not bound:
            self._q_end = None
            self._publish(snap, amp=0.0, kick=0.0, outA=0.0, outB=0.0, bound=False,
                          status="等待 App 扫码绑定…")
            return
        if not enabled:
            self._q_end = None
            await self._set_zero_strength(now)
            self._publish(snap, amp=0.0, kick=0.0, outA=0.0, outB=0.0, bound=True,
                          status=f"已绑定 {self.state.target} · 已停止")
            return

        await self._apply_strengths(a_on, b_on)

        # ---------- 连续跟随基准（强度、频率都随音量）----------
        if self._last_map_t is None:
            self._last_map_t = now
            self._enable_t = now
        dt = min(0.5, max(0.02, now - self._last_map_t))
        self._last_map_t = now
        mode = cfg.get("mode", "beat")
        res = self.mapper.step(snap, "follow", float(cfg.get("sensitivity", 45)),
                               cfg.get("style", "mid"), dt)
        amp_ref = res["amp_follow"]               # 0..1 音量包络
        ramp = 1.0 if self._enable_t is None else min(1.0, (now - self._enable_t) / RAMP_S)

        # 当前动态频率：越响越高频（质感档的低频为底，向高频档靠）
        span = max(1, res["kick_hz"] - res["freq_base"])
        fx = int(_clamp(round(res["freq_base"] + span * _clamp(amp_ref, 0.0, 1.0)), FREQ_MIN, FREQ_MAX))
        fx = _clamp(fx, FREQ_MIN, FREQ_MAX)

        # 瞬态强调包络（供 hybrid 未锁定时给重音加一点，随 dt 衰减）
        if snap.onset:
            self._onset_amp = min(1.0, max(0.45, snap.onset_db / 9.0))
        if self._onset_amp > 0.0:
            self._onset_amp *= 0.45 ** (dt / 0.10)
            if self._onset_amp < 0.05:
                self._onset_amp = 0.0

        locked = snap.locked

        def follow_strengths(a: float) -> tuple:
            return tuple(int(_clamp(FOLLOW_SHAPE[i] * a * ramp, 0.0, 1.0) * 100) for i in range(4))

        # 强调源：节拍(锁定)按"拍形"波形 + hybrid 的瞬态强调
        bshape = str(cfg.get("beat_shape", "sharp"))

        def slot_accent(st: float):
            """返回该槽要用的加重幅度(0..1)；无则 None。"""
            if mode == "follow":
                return None
            eff = None
            if locked:
                base_acc = min(1.0, 0.60 + amp_ref * 0.5)   # 保底够明显，仍随音量微调
                in_slot = self.analyzer.beats_between(st, st + PULSE_S)
                if in_slot:
                    d = 0.0
                else:
                    b0 = self.analyzer.beat_before(st)
                    d = (st - b0) if b0 is not None else None
                if d is not None and d < SHAPE_MAX:
                    v = base_acc * _shape_level(bshape, d)
                    if v >= 0.12:
                        eff = v
            if mode == "hybrid" and self._onset_amp > 0.05 and st <= now + 0.35:
                tacc = min(1.0, self._onset_amp * 1.15 + 0.15)
                eff = tacc if eff is None else max(eff, tacc)
            return eff

        # ---------- 前视队列：成批发 ----------
        if self._q_end is None or self._q_end < now - 0.05:
            self._q_end = now + HOLD_S
        batchA: list = []
        batchB: list = []
        pushed = 0
        peak_amp = amp_ref * ramp
        while (self._q_end - now) < HOLD_S_LOW and pushed < 5:
            pushed += 1
            st = self._q_end
            a_strength = amp_ref
            acc = slot_accent(st)
            if acc is not None:
                q = int(_clamp(acc * ramp, 0.0, 1.0) * 100)
                f, s = (fx, fx, fx, fx), (q, q, q, q)
                a_strength = acc
            else:
                f = (fx, fx, fx, fx)
                s = follow_strengths(amp_ref)
            if a_strength * ramp > peak_amp:
                peak_amp = a_strength * ramp
            if a_on:
                batchA.append((f, s))
            if b_on:
                batchB.append((f, s))
            self._q_end += PULSE_S
        if pushed == 0:
            self._q_end = max(self._q_end, now + HOLD_S_LOW)

        if batchA:
            await self._send_batch(Channel.A, batchA)
        if batchB:
            await self._send_batch(Channel.B, batchB)

        self._publish(snap, amp=peak_amp, kick=self._onset_amp * ramp,
                      outA=peak_amp if a_on else 0.0, outB=peak_amp if b_on else 0.0,
                      bound=True, bpm=snap.bpm,
                      status=self._status_text(mode, locked, bound, enabled, snap.bpm))

    def _status_text(self, mode: str, locked: bool, bound: bool, enabled: bool, bpm) -> str:
        if not bound:
            return "等待 App 扫码绑定…"
        base = f"已绑定 {self.state.target}"
        if not enabled:
            return base + " · 已停止"
        if mode == "follow":
            return base + " · 跟随运行中"
        if locked:
            what = "节拍跟随" if mode == "beat" else "混合"
            return f"{base} · {what} · 锁定 {round(bpm)} BPM"
        return base + (f" · 节拍检测中…（未锁定时仅连续跟随）" if mode == "beat" else " · 运行中")

    # ---------- 底层 ----------
    async def _clear(self, ch: str) -> None:
        client = self.dg.client
        if client is None:
            return
        try:
            await client.clear_pulses(Channel.A if ch == "A" else Channel.B)
        except Exception:  # noqa: BLE001
            pass

    async def _send_batch(self, ch: Channel, batch: list) -> None:
        client = self.dg.client
        if client is None or not batch:
            return
        try:
            ok = await client.add_pulses(ch, *batch)
            if ok is False:
                self._q_end = None
        except Exception:  # noqa: BLE001
            self._q_end = None
            return
        self._pulses_sent += len(batch)
        self._last_send_t = time.monotonic()

    async def test_wave(self, ch: str) -> bool:
        """诊断用：往指定通道发 ~1.2s 强波形，与音频无关。"""
        if not self.state.bound or self.dg.client is None:
            return False
        channel = Channel.A if ch == "A" else Channel.B
        ceil_key = "ceilA" if ch == "A" else "ceilB"
        limit = self.state.a_limit if ch == "A" else self.state.b_limit
        strength = int(min(200, max(10, float(self.cfg.get(ceil_key, 50)))))
        if limit > 0:
            strength = min(strength, limit)
        try:
            await self.dg.client.set_strength(channel, StrengthOperationType.SET_TO, strength)
            self._applied[ch] = strength
            pulses = []
            for i in range(12):
                amp = 100 if i % 4 == 0 else 88
                pulses.append(((110, 110, 110, 110), (amp, amp, amp, amp)))
            ok = await self.dg.client.add_pulses(channel, *pulses)
            if ok is False:
                return False
            self._pulses_sent += len(pulses)
            self._last_send_t = time.monotonic()
            self.state.update(status=f"测试波形已发到通道 {ch}（1.2s）")
            return True
        except Exception:  # noqa: BLE001
            return False

    async def _apply_strengths(self, a_on: bool, b_on: bool) -> None:
        client = self.dg.client
        if client is None or not self.state.bound:
            return
        try:
            la, lb = self.state.a_limit, self.state.b_limit
            da = int(min(200, max(0, float(self.cfg.get("ceilA", 50)))))
            db = int(min(200, max(0, float(self.cfg.get("ceilB", 50)))))
            if la > 0:
                da = min(da, la)
            if lb > 0:
                db = min(db, lb)
            if not a_on:
                da = 0
            if not b_on:
                db = 0
            if a_on and abs(self.state.a - da) > 1 and self._applied["A"] != da:
                await client.set_strength(Channel.A, StrengthOperationType.SET_TO, da)
                self._applied["A"] = da
            if b_on and abs(self.state.b - db) > 1 and self._applied["B"] != db:
                await client.set_strength(Channel.B, StrengthOperationType.SET_TO, db)
                self._applied["B"] = db
        except Exception:  # noqa: BLE001
            pass

    async def _set_zero_strength(self, now: float) -> None:
        client = self.dg.client
        if client is None or not self.state.bound:
            return
        try:
            if (self._applied["A"] not in (None, 0)) or abs(self.state.a) > 1:
                await client.set_strength(Channel.A, StrengthOperationType.SET_TO, 0)
                self._applied["A"] = 0
            if (self._applied["B"] not in (None, 0)) or abs(self.state.b) > 1:
                await client.set_strength(Channel.B, StrengthOperationType.SET_TO, 0)
                self._applied["B"] = 0
        except Exception:  # noqa: BLE001
            pass

    # ---------- 遥测 ----------
    def _publish(self, snap, amp, kick, outA, outB, bound, status=None, bpm=None) -> None:
        err = self.capture.error if self.capture else ""
        st = status or self.state.status
        cap = self.capture
        if cap is not None and cap.silent and self.state.enabled:
            st += " · 无声/等待播放（环回静音时会停流）"
        base = dict(
            level_db=round(snap.env_db, 1),
            gate_db=round(snap.gate_db, 1),
            amp=round(amp, 3),
            kick=round(kick, 3),
            outA=round(outA, 3),
            outB=round(outB, 3),
            bpm=bpm,
            locked=bool(snap.locked),
            pulses_sent=self._pulses_sent,
            wave_on=(time.monotonic() - self._last_send_t) < 0.8,
            status=st,
        )
        if err:
            base["error"] = err
        self.state.update(**base)
