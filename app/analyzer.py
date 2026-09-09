"""dB 电平分析：包络/门限/重音(onset) + **节拍器锁相**。

节拍锁：只有检测到一连串"间隔稳定"的重音（节拍器那种规整脉冲串）才置 locked，
并给出可预测的拍点网格。说话等人声不规整，锁不上 -> beat 模式就不输出。

锁相逻辑（全部单调递增时间 = time.monotonic()）：
- 维护最近的重音时刻 _onsets；
- 用最近的若干拍间隔取中位数作为周期 P；
- 若多数间隔与 P 偏差 <=15%，视为规整 -> locked，拍点相位锚定到最近一拍；
- 超时(超过 ~2~3 拍间隔仍无新重音)自动解锁。
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from statistics import median


@dataclass
class Snapshot:
    env_db: float = -120.0          # 平滑包络（"跟随"用）
    ref_db: float = -120.0          # 慢速参考
    floor_db: float = -55.0         # 自动校准底噪
    gate_db: float = -50.0          # 门限 = floor + threshold
    onset: bool = False             # 本批内是否检测到重音（上升沿）
    onset_db: float = 0.0           # onset 强度（env-ref 超出量，dB）
    locked: bool = False            # 节拍器是否已锁定（周期规整）
    bpm: float | None = None        # 锁定后的 BPM
    fresh: bool = False             # 最近是否有样本


class Analyzer:
    ATTACK_TAU = 0.012       # 包络上升时间常数（s）
    REF_TAU = 0.45           # 慢参考时间常数（s）
    ONSET_MIN_DB = 6.0       # 相对慢参考高出多少算重音
    ONSET_COOLDOWN = 0.16    # 两次重音最小间隔（s），支持到 ~300 BPM
    CALIB_TIME = 0.7         # 底噪校准时长（s）
    CALIB_PCT = 0.15         # 取该分位 dB 作为底噪
    PERIOD_MIN = 0.18        # 允许的最短拍间隔（≈333 BPM）
    PERIOD_MAX = 1.3         # 允许的最长拍间隔（≈46 BPM）
    PERIOD_TOL = 0.15        # 判定"规整"的间隔容差（±15%）
    LOCK_MIN_INTERVALS = 3   # 至少要这么多连续间隔一致才锁定
    LOCK_MIN_GOOD = 2        # 容差内至少几段（满足即视为规整）见 _update_lock

    def __init__(self) -> None:
        self._env = -120.0
        self._ref = -120.0
        self._last_t: float | None = None
        self._floor = -55.0
        self._floor_ready = False
        self._calib_t0: float | None = None
        self._calib: list[float] = []
        self._last_onset_t = -10.0
        # 节拍锁
        self._onsets: list[float] = []     # 最近重音时刻
        self._locked = False
        self._period: float | None = None  # 锁定周期（s）
        self._lock_expire: float = 0.0     # 超过该时刻自动解锁
        self._bpm: float | None = None

    # ---------- 控制 ----------
    def begin_calibration(self) -> None:
        self._floor_ready = False
        self._calib_t0 = time.monotonic()
        self._calib = []

    @property
    def floor_ready(self) -> bool:
        return self._floor_ready

    @property
    def locked(self) -> bool:
        return self._locked

    @property
    def period(self) -> float | None:
        return self._period

    def reset(self) -> None:
        self._env = -120.0
        self._ref = -120.0
        self._last_t = None
        self._last_onset_t = -10.0
        self._onsets.clear()
        self._locked = False
        self._period = None
        self._bpm = None

    def idle_check(self, now: float) -> None:
        """无新样本时也调用：超时解锁（节拍器停止 / 静音）。"""
        if self._locked and now > self._lock_expire:
            self._locked = False
            self._period = None
            self._bpm = None

    # ---------- 处理 ----------
    def consume(self, samples: list[tuple[float, float]], threshold_db: float = 0.0, release_tau: float = 0.25) -> Snapshot:
        onset = False
        onset_db = 0.0
        fresh = bool(samples)

        for t, db in samples:
            if self._last_t is None:
                self._last_t = t
            dt = max(1e-4, t - self._last_t)
            self._last_t = t

            # 底噪校准
            if not self._floor_ready:
                if self._calib_t0 is None:
                    self._calib_t0 = t
                if t - self._calib_t0 < self.CALIB_TIME:
                    self._calib.append(db)
                elif self._calib:
                    c = sorted(self._calib)
                    idx = min(len(c) - 1, int(len(c) * self.CALIB_PCT))
                    self._floor = max(-85.0, min(-35.0, c[idx] - 1.5))
                    self._floor_ready = True

            # 包络：上升用 attack，回落用 release
            env_prev = self._env
            tau = self.ATTACK_TAU if db >= self._env else release_tau
            k = 1.0 - math.exp(-dt / tau)
            self._env += (db - self._env) * k

            # 慢参考
            kref = 1.0 - math.exp(-dt / self.REF_TAU)
            self._ref += (db - self._ref) * kref

            gate = self._floor + threshold_db
            diff = self._env - self._ref
            rising = self._env > env_prev
            if (
                self._floor_ready
                and rising
                and diff > self.ONSET_MIN_DB
                and self._env > gate + 3.0
                and (t - self._last_onset_t) > self.ONSET_COOLDOWN
            ):
                onset = True
                onset_db = diff
                self._last_onset_t = t
                self._onsets.append(t)
                # 只保留最近若干拍，避免旧拍污染
                if len(self._onsets) > 24:
                    self._onsets.pop(0)
                self._update_lock(t)

        # 无新重音也要推进解锁
        now = samples[-1][0] if samples else time.monotonic()
        self.idle_check(now)

        return Snapshot(
            env_db=self._env,
            ref_db=self._ref,
            floor_db=self._floor,
            gate_db=self._floor + threshold_db,
            onset=onset,
            onset_db=onset_db,
            locked=self._locked,
            bpm=self._bpm,
            fresh=fresh,
        )

    # ---------- 节拍锁 ----------
    def _update_lock(self, t: float) -> None:
        ons = self._onsets
        if len(ons) < 3:
            return
        # 最近若干拍间隔
        start = max(0, len(ons) - 8)
        deltas = [ons[i + 1] - ons[i] for i in range(start, len(ons) - 1)]
        if len(deltas) < 3:
            return
        m = median(deltas)
        if not (self.PERIOD_MIN <= m <= self.PERIOD_MAX):
            if self._locked:
                self._unlock()
            return
        good = sum(1 for d in deltas if abs(d - m) <= self.PERIOD_TOL * m)
        need = min(self.LOCK_MIN_GOOD, len(deltas))
        if good >= need and len(deltas) >= self.LOCK_MIN_INTERVALS:
            self._locked = True
            self._period = m
            self._bpm = 60.0 / m
            # 锁定期限：约 2.5 个周期内需有新重音
            self._lock_expire = t + max(1.8, 2.5 * m)
        else:
            if self._locked and t > self._lock_expire:
                self._unlock()

    def _unlock(self) -> None:
        self._locked = False
        self._period = None
        self._bpm = None

    # ---------- 预测拍点 ----------
    def beats_between(self, t0: float, t1: float) -> list[float]:
        """返回落在 [t0, t1] 内的预测拍点时刻（仅锁定时有值）。"""
        if not self._locked or not self._period or not self._onsets:
            return []
        anchor = self._onsets[-1]
        P = self._period
        eps = 1e-6
        m = math.floor((t0 - anchor) / P) - 1
        out: list[float] = []
        while True:
            tt = anchor + m * P
            if tt > t1 + eps:
                break
            if tt >= t0 - eps:
                out.append(tt)
            m += 1
        return out

    def beat_before(self, t: float) -> float | None:
        """返回 <= t 的最近一个（可为未来的）预测拍点时刻；未锁定返回 None。"""
        if not self._locked or not self._period or not self._onsets:
            return None
        P = self._period
        a = self._onsets[-1]
        k = int(math.floor((t - a) / P + 1e-9))
        b = a + k * P
        if b > t + 1e-9:
            b -= P
        return b
