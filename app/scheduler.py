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
from collections import deque

from pydglab_ws import Channel, StrengthOperationType

from .analyzer import Analyzer
from .feed import LevelFeed
from .mapper import FOLLOW_SHAPE, STYLES, Mapper
from .state import State
from . import waveforms

PULSE_S = 0.100
HOLD_S = 0.18          # 前视队列目标（秒）
HOLD_S_LOW = 0.06
RAMP_S = 0.6           # 启动缓升时长
FREQ_MIN = 10
FREQ_MAX = 240
SCOPE_SLOTS = 60       # 输出波形示波器保留的槽数（10 槽/秒 -> 6 秒）


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


# ---------- 自动增强参数 ----------
BOOST_TRIG = 0.60      # 幅度持续超过该值 -> 进入"达标"状态
BOOST_REL = 0.35       # 幅度低于该值 -> 视为条件消失
BOOST_UP_S = 8.0       # 持续达标多久升一级
BOOST_DOWN_S = 5.0     # 条件消失多久降一级（仅 recover 模式）
BOOST_STEP = 8         # 每级加多少强度


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
        self._onset_amp: float = 0.0   # 未锁定时用于 hybrid 瞬态强调的衰减包络
        self._beat_grid_t: float | None = None   # 最近一次拍点对齐到的槽起点
        # 自动增强状态
        self._boost = {"A": 0, "B": 0}
        self._hot_s: float = 0.0
        self._cool_s: float = 0.0
        # 输出波形历史（供界面示波器）：(ampA, ampB, freqA, freqB, beat)
        self._scope: deque = deque(maxlen=SCOPE_SLOTS)
        self._last_freq: tuple = (0, 0)   # 停止后仍显示最后频率
        # 官方波形回放位置（每通道独立）
        self._wave_pos: dict = {"A": 0, "B": 0}
        self._wave_used: dict = {"A": None, "B": None}
        self._was_enabled: bool = False

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

        # 门限：自动(跟随环境底噪) 或 手动绝对 dB
        gate_manual = None
        if not bool(cfg.get("threshold_auto", True)):
            gate_manual = float(cfg.get("threshold_db", -50.0))
        release_tau = max(0.02, float(cfg.get("release_ms", 220)) / 1000.0)
        samples = self.feed.drain()
        snap = self.analyzer.consume(samples, gate_manual=gate_manual, release_tau=release_tau)
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
            self._was_enabled = False
            await self._set_zero_strength(now)
            self._publish(snap, amp=0.0, kick=0.0, outA=0.0, outB=0.0, bound=True,
                          status=f"已绑定 {self.state.target} · 已停止")
            return

        await self._apply_strengths(a_on, b_on)

        # ---------- 每通道独立配置（A/B 联动时 B 跟随 A）----------
        link = bool(cfg.get("ab_link", True))
        if self._last_map_t is None:
            self._last_map_t = now
            self._enable_t = now
        dt = min(0.5, max(0.02, now - self._last_map_t))
        self._last_map_t = now
        # 只取"音量->幅度"包络（与质感/模式无关）
        res = self.mapper.step(snap, "follow", float(cfg.get("sensitivity", 45)), "mid", dt)
        amp_ref = _clamp(res["amp_follow"], 0.0, 1.0)     # 0..1 音量包络
        ramp = 1.0 if self._enable_t is None else min(1.0, (now - self._enable_t) / RAMP_S)

        chanA = self._chan_plan("A", cfg, link)
        chanB = self._chan_plan("B", cfg, link)
        self._last_freq = (chanA["base"], chanB["base"])

        # 自动增强/恢复（达标升一级；条件消失按模式恢复或保持）
        self._update_boost(amp_ref, dt)

        # 瞬态强调包络（供 hybrid 未锁定时给重音加一点，随 dt 衰减）
        if snap.onset:
            self._onset_amp = min(1.0, max(0.45, snap.onset_db / 9.0))
        if self._onset_amp > 0.0:
            self._onset_amp *= 0.45 ** (dt / 0.10)
            if self._onset_amp < 0.05:
                self._onset_amp = 0.0

        locked = snap.locked
        base_acc = min(1.0, 0.60 + amp_ref * 0.5)   # 每拍加重保底幅度
        transient = self._onset_amp                 # hybrid 的瞬态强调

        def follow_strengths(a: float) -> tuple:
            return tuple(int(_clamp(FOLLOW_SHAPE[i] * a * ramp, 0.0, 1.0) * 100) for i in range(4))

        def slot_acc(mode_c: str, shape_c: str, hit_d: float | None):
            """该通道在本槽的加重幅度(0..1)；无则 None。"""
            if mode_c not in ("beat", "hybrid"):
                return None
            eff = None
            if hit_d is not None and hit_d < SHAPE_MAX:
                v = base_acc * _shape_level(shape_c, hit_d)
                if v >= 0.12:
                    eff = v
            if mode_c == "hybrid" and transient > 0.05:
                tacc = min(1.0, transient * 1.15 + 0.15)
                eff = tacc if eff is None else max(eff, tacc)
            return eff

        # ---------- 前视队列：成批发 ----------
        if self._q_end is None or self._q_end < now - 0.05:
            self._q_end = now + HOLD_S
        batchA: list = []
        batchB: list = []
        pushed = 0
        peakA = peakB = amp_ref * ramp
        freqA_now = chanA["base"]
        freqB_now = chanB["base"]
        # 每次"开始"都把官方波形从头播
        if not self._was_enabled:
            self._wave_pos = {"A": 0, "B": 0}
            self._was_enabled = True
        while (self._q_end - now) < HOLD_S_LOW and pushed < 5:
            pushed += 1
            st = self._q_end

            # 节拍对齐到 100ms 槽网格：拍落在哪个格，那个格就是拍形起点（力度稳定，
            # 后续格子按 d 铺开形状的尾巴）。未锁定时清空。
            hit_d = None
            if locked:
                b = self.analyzer.beat_before(st + PULSE_S)
                if b is not None and b >= st:
                    self._beat_grid_t = st
                if self._beat_grid_t is not None:
                    d = st - self._beat_grid_t
                    if 0.0 <= d < SHAPE_MAX:
                        hit_d = d
            else:
                self._beat_grid_t = None
            beat_slot = hit_d == 0.0

            lvA = lvB = 0.0
            for ch, plan in (("A", chanA), ("B", chanB)):
                acc = slot_acc(plan["mode"], plan["shape"], hit_d)
                lvl = (acc * ramp) if acc is not None else (amp_ref * ramp)
                f = s = None
                freq_now = plan["base"]
                if plan["src"] == "official":
                    got = self._official_slot(ch, plan, lvl)
                    if got is not None:
                        f, s, freq_now = got
                if f is None:                      # 自建映射
                    if acc is not None:            # 撞击：高频、短促
                        q = int(_clamp(acc * ramp, 0.0, 1.0) * 100)
                        f, s = (plan["hit"],) * 4, (q, q, q, q)
                        freq_now = plan["hit"]
                    else:                          # 连续：低频跟随音量
                        f = (plan["base"],) * 4
                        s = follow_strengths(amp_ref)
                        freq_now = plan["base"]
                if ch == "A":
                    lvA = lvl
                    peakA = max(peakA, lvl)
                    freqA_now = freq_now
                    if a_on:
                        batchA.append((f, s))
                else:
                    lvB = lvl
                    peakB = max(peakB, lvl)
                    freqB_now = freq_now
                    if b_on:
                        batchB.append((f, s))
            # 记录输出波形（供界面示波器）
            self._scope.append((
                int(_clamp(lvA, 0.0, 1.0) * 100),
                int(_clamp(lvB, 0.0, 1.0) * 100),
                freqA_now, freqB_now,
                1 if beat_slot else 0,
            ))
            self._q_end += PULSE_S
        if pushed == 0:
            self._q_end = max(self._q_end, now + HOLD_S_LOW)

        if batchA:
            await self._send_batch(Channel.A, batchA)
        if batchB:
            await self._send_batch(Channel.B, batchB)

        self._publish(snap, amp=max(peakA, peakB), kick=self._onset_amp * ramp,
                      outA=peakA if a_on else 0.0, outB=peakB if b_on else 0.0,
                      bound=True, bpm=snap.bpm, freqA=freqA_now, freqB=freqB_now,
                      status=self._status_text(chanA["mode"], chanB["mode"], locked, bound, enabled, snap.bpm))

    def _status_text(self, mode_a: str, mode_b: str, locked: bool, bound: bool, enabled: bool, bpm) -> str:
        if not bound:
            return "等待 App 扫码绑定…"
        base = f"已绑定 {self.state.target}"
        if not enabled:
            return base + " · 已停止"
        if mode_a == "follow" and mode_b == "follow":
            return base + " · 普通音频跟随运行中"
        if locked:
            return f"{base} · 节拍锁定 {round(bpm)} BPM"
        return base + " · 节拍检测中…（未锁定时仅连续跟随）"

    # ---------- 自动增强 ----------
    def reset_boost(self) -> None:
        """手动恢复基础强度（清空自动增强）。"""
        self._boost = {"A": 0, "B": 0}
        self._hot_s = 0.0
        self._cool_s = 0.0
        self._applied = {"A": None, "B": None}
        self.state.poke()

    def _safety_cap(self, ch: str) -> int:
        """该通道独立的安全硬上限（0-200）：手动/自动/测试波形都不允许越过。"""
        cfg = self.cfg.d
        v = cfg.get(f"safety_cap{ch}")
        if v is None:                       # 兼容只有全局 safety_cap 的旧配置
            v = cfg.get("safety_cap", 200)
        try:
            return int(min(200, max(0, float(v))))
        except (TypeError, ValueError):
            return 200

    def _target_strength(self, ch: str) -> int:
        """该通道当前目标主强度 = min(本通道安全上限, 基础上限 + 自动增强)。"""
        cfg = self.cfg.d
        ceil = float(cfg.get("ceilA", 50) if ch == "A" else cfg.get("ceilB", 50))
        return int(min(self._safety_cap(ch), min(200, ceil) + self._boost[ch]))

    def _update_boost(self, amp: float, dt: float) -> None:
        cfg = self.cfg.d
        if not bool(cfg.get("boost_on", False)):
            if self._boost["A"] or self._boost["B"]:
                self._boost = {"A": 0, "B": 0}
                self._applied = {"A": None, "B": None}
            self._hot_s = self._cool_s = 0.0
            return
        hot = amp >= BOOST_TRIG
        if hot:
            self._hot_s += dt
            self._cool_s = 0.0
        elif amp < BOOST_REL:
            self._cool_s += dt
            self._hot_s = 0.0
        changed = False
        if self._hot_s >= BOOST_UP_S:
            self._hot_s = 0.0
            for ch in ("A", "B"):
                ceil = float(cfg.get("ceilA", 50) if ch == "A" else cfg.get("ceilB", 50))
                room = max(0, self._safety_cap(ch) - int(min(200, ceil)))
                if self._boost[ch] < room:
                    self._boost[ch] = min(room, self._boost[ch] + BOOST_STEP)
                    changed = True
        elif self._cool_s >= BOOST_DOWN_S:
            self._cool_s = 0.0
            if str(cfg.get("boost_mode", "recover")) == "recover":
                for ch in ("A", "B"):
                    if self._boost[ch] > 0:
                        self._boost[ch] = max(0, self._boost[ch] - BOOST_STEP)
                        changed = True
        if changed:
            self._applied = {"A": None, "B": None}
            self.state.poke()

    # ---------- 底层 ----------
    async def _clear(self, ch: str) -> None:
        client = self.dg.client
        if client is None:
            return
        try:
            await client.clear_pulses(Channel.A if ch == "A" else Channel.B)
        except Exception:  # noqa: BLE001
            pass

    # ---------- 每通道配置 ----------
    @staticmethod
    def _chan_plan(ch: str, cfg: dict, link: bool) -> dict:
        """该通道的输出计划：模式/拍形/频率档/波形来源（联动时 B 跟随 A）。"""
        key = "A" if (ch == "A" or link) else "B"
        m = str(cfg.get(f"mode{key}", "beat"))
        if m == "audio":
            m = "follow"
        src = str(cfg.get(f"src{key}", "map"))
        if src not in ("map", "official"):
            src = "map"
        style_c = str(cfg.get(f"style{key}", "mid"))
        _, base_hz, hit_hz = STYLES.get(style_c, STYLES["mid"])
        return {
            "mode": m,
            "shape": str(cfg.get(f"shape{key}", "sharp")),
            "base": int(base_hz),      # 连续输出用低频（低频通常一直有 -> 细麻）
            "hit": int(hit_hz),        # 撞击用高频（高频稀少 -> 间断）
            "src": src,
            "wave": str(cfg.get(f"wave{key}", "BREATHING")),
        }

    def _official_slot(self, ch: str, plan: dict, level: float):
        """按官方波形取下一槽 -> (freq4, strength4, 显示用频率)。序列空则返回 None。"""
        seq = waveforms.pulses(plan["wave"])
        if not seq:
            return None
        if self._wave_used.get(ch) != plan["wave"]:
            self._wave_used[ch] = plan["wave"]
            self._wave_pos[ch] = 0
        pos = self._wave_pos.get(ch, 0) % len(seq)
        self._wave_pos[ch] = pos + 1
        f0, s0 = seq[pos]
        lvl = _clamp(level, 0.0, 1.0)
        s = tuple(int(_clamp(v * lvl, 0.0, 100.0)) for v in s0)
        return tuple(f0), s, int(f0[0])

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
        limit = self.state.a_limit if ch == "A" else self.state.b_limit
        strength = self._target_strength(ch)
        if limit > 0:
            strength = min(strength, limit)
        strength = max(10, strength)                  # 保证测试波有可感知效果
        strength = min(strength, self._safety_cap(ch))  # 但绝不越过本通道安全上限
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
            da = self._target_strength("A")
            db = self._target_strength("B")
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
    def _publish(self, snap, amp, kick, outA, outB, bound, status=None, bpm=None,
                 freqA=0, freqB=0) -> None:
        err = self.capture.error if self.capture else ""
        st = status or self.state.status
        cap = self.capture
        if cap is not None and cap.silent and self.state.enabled:
            st += " · 无声/等待播放（环回静音时会停流）"
        spec = self.feed.spec() if self.feed is not None else []
        if not freqA:
            freqA = self._last_freq[0]
        if not freqB:
            freqB = self._last_freq[1]
        base = dict(
            level_db=round(snap.env_db, 1),
            gate_db=round(snap.gate_db, 1),
            amp=round(amp, 3),
            kick=round(kick, 3),
            outA=round(outA, 3),
            outB=round(outB, 3),
            bpm=bpm,
            locked=bool(snap.locked),
            low_on=bool(getattr(snap, "low_on", False)),
            boostA=int(self._boost.get("A", 0)),
            boostB=int(self._boost.get("B", 0)),
            pulses_sent=self._pulses_sent,
            wave_on=(time.monotonic() - self._last_send_t) < 0.8,
            freqA=int(freqA), freqB=int(freqB),
            spec=spec,
            scope=list(self._scope),
            status=st,
        )
        if err:
            base["error"] = err
        self.state.update(**base)
