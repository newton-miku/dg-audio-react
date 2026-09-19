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
    low_on: bool = False            # 最近一次重音是否由低频（底鼓/贝斯）触发
    fresh: bool = False             # 最近是否有样本


class Analyzer:
    ATTACK_TAU = 0.012       # 包络上升时间常数（s）
    REF_TAU = 0.45           # 慢参考时间常数（s）
    ONSET_MIN_DB = 6.0       # 全频：相对慢参考高出多少算重音
    ONSET_MIN_DB_LOW = 5.0   # 低频（底鼓/贝斯）：音乐强拍主要在这里
    ONSET_COOLDOWN = 0.16    # 两次重音最小间隔（s），支持到 ~300 BPM
    CALIB_TIME = 0.7         # 底噪校准时长（s）
    CALIB_PCT = 0.15         # 取该分位 dB 作为底噪
    PERIOD_MIN = 0.20        # 允许的最短拍间隔（≈300 BPM；八分音符网格也在内）
    PERIOD_MAX = 1.5         # 允许的最长拍间隔（≈40 BPM）
    PERIOD_TOL = 0.28        # 判定"落在网格上"的相对容差（±28% 周期）
    PERIOD_TOL_ABS = 0.075   # 但绝对容差不超过 ±75ms（人的时间抖动是绝对的，
                             # 不随周期放大；否则长周期时窗口过宽，人声会被误锁）
    LOCK_MIN_ONSETS = 6      # 至少要有这么多拍点才考虑锁定（防止人声误锁）
    LOCK_ALIGN_RATIO = 0.75  # 至少这么多比例的拍点要落在同一网格上

    def __init__(self) -> None:
        self._env = -120.0
        self._ref = -120.0
        self._last_t: float | None = None
        self._floor = -55.0
        self._floor_ready = False
        self._calib_t0: float | None = None
        self._calib: list[float] = []
        self._calib_low: list[float] = []
        # 低频通道（底鼓/贝斯）
        self._env_low = -120.0
        self._ref_low = -120.0
        self._floor_low = -60.0
        self._floor_low_ready = False
        self._low_on = False                      # 最近一次 onset 是否由低频触发
        self._last_onset_t = -10.0
        # 节拍锁
        self._onsets: list[float] = []     # 最近重音时刻
        self._onset_str: list[float] = []  # 与 _onsets 平行的强度（dB 超出量）
        self._locked = False
        self._period: float | None = None  # 锁定周期（s）
        self._lock_expire: float = 0.0     # 超过该时刻自动解锁
        self._bpm: float | None = None

    # ---------- 控制 ----------
    @staticmethod
    def _pct(vals: list[float], lo: float, hi: float) -> float:
        """取低分位作为底噪估计，并钳制到合理区间。"""
        c = sorted(vals)
        idx = min(len(c) - 1, int(len(c) * Analyzer.CALIB_PCT))
        return max(lo, min(hi, c[idx] - 1.5))

    def begin_calibration(self) -> None:
        self._floor_ready = False
        self._floor_low_ready = False
        # 由"第一个样本的时间戳"作为校准起点（与样本时钟保持一致）
        self._calib_t0 = None
        self._calib = []
        self._calib_low = []

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
        self._env_low = -120.0
        self._ref_low = -120.0
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
    def consume(self, samples, gate_manual: float | None = None, release_tau: float = 0.25) -> Snapshot:
        """处理新样本。

        每个样本为 (t, db, low)；db=全频电平，low=低频电平（可选）。
        gate_manual: 手动绝对门限(dB)；None = 自动跟随环境底噪。
        """
        onset = False
        onset_db = 0.0
        fresh = bool(samples)

        for item in samples:
            t = item[0]
            db = item[1]
            low = item[2] if len(item) > 2 else None

            if self._last_t is None:
                self._last_t = t
            dt = max(1e-4, t - self._last_t)
            self._last_t = t

            # 底噪校准（全频与低频各一份）
            if not self._floor_ready:
                if self._calib_t0 is None:
                    self._calib_t0 = t
                if t - self._calib_t0 < self.CALIB_TIME:
                    self._calib.append(db)
                    if low is not None and low > -119.0:
                        self._calib_low.append(low)
                elif self._calib:
                    self._floor = self._pct(self._calib, -85.0, -35.0)
                    self._floor_ready = True
            if not self._floor_low_ready and self._calib_low:
                if self._floor_ready or (self._calib_t0 is not None and t - self._calib_t0 >= self.CALIB_TIME):
                    self._floor_low = self._pct(self._calib_low, -90.0, -35.0)
                    self._floor_low_ready = True

            # 全频包络：上升用 attack，回落用 release
            env_prev = self._env
            tau = self.ATTACK_TAU if db >= self._env else release_tau
            k = 1.0 - math.exp(-dt / tau)
            self._env += (db - self._env) * k
            kref = 1.0 - math.exp(-dt / self.REF_TAU)
            self._ref += (db - self._ref) * kref

            gate = self._floor if gate_manual is None else gate_manual
            diff = self._env - self._ref
            trig_full = (
                self._floor_ready
                and self._env > env_prev
                and diff > self.ONSET_MIN_DB
                and self._env > gate + 3.0
            )

            # 低频通道（音乐节拍主要由底鼓/贝斯承载）
            trig_low = False
            diff_low = 0.0
            if low is not None and low > -119.0:
                env_low_prev = self._env_low
                tau_l = self.ATTACK_TAU if low >= self._env_low else release_tau
                kl = 1.0 - math.exp(-dt / tau_l)
                self._env_low += (low - self._env_low) * kl
                krefl = 1.0 - math.exp(-dt / self.REF_TAU)
                self._ref_low += (low - self._ref_low) * krefl
                # 手动门限按"相对底噪的抬升量"作用到低频，保持语义一致
                if gate_manual is None:
                    gate_low = self._floor_low
                else:
                    gate_low = self._floor_low + max(0.0, gate_manual - self._floor)
                diff_low = self._env_low - self._ref_low
                trig_low = (
                    self._floor_low_ready
                    and self._env_low > env_low_prev
                    and diff_low > self.ONSET_MIN_DB_LOW
                    and self._env_low > gate_low + 3.0
                )

            if (trig_full or trig_low) and (t - self._last_onset_t) > self.ONSET_COOLDOWN:
                onset = True
                onset_db = max(diff, diff_low)
                self._low_on = trig_low and not trig_full
                self._last_onset_t = t
                self._onsets.append(t)
                self._onset_str.append(onset_db)
                if len(self._onsets) > 24:
                    self._onsets.pop(0)
                    self._onset_str.pop(0)
                self._update_lock(t)

        now = samples[-1][0] if samples else time.monotonic()
        self.idle_check(now)

        return Snapshot(
            env_db=self._env,
            ref_db=self._ref,
            floor_db=self._floor,
            gate_db=self._floor if gate_manual is None else gate_manual,
            onset=onset,
            onset_db=onset_db,
            locked=self._locked,
            bpm=self._bpm,
            low_on=self._low_on,
            fresh=fresh,
        )

    # ---------- 节拍锁 ----------
    @staticmethod
    def _align_ratio(ons: list[float], P: float, anchor: float, tol_frac: float, tol_abs: float) -> float:
        tol = min(tol_frac * P, tol_abs)
        n = 0
        for x in ons:
            ph = (x - anchor) % P
            if ph < tol or (P - ph) < tol:
                n += 1
        return n / len(ons)

    def _update_lock(self, t: float) -> None:
        """网格对齐率判据：以中位间隔为初值，再小步精修周期，取对齐率最高者。

        比"逐个间隔比较"更抗漏拍/切分；周期精修解决"中位数略有偏差、长窗口内相位累积漂移"。
        """
        ons = self._onsets[-14:]
        strs = self._onset_str[-14:]
        if len(ons) < self.LOCK_MIN_ONSETS:
            return
        fits = self._fit_grid(ons)
        if fits is None:
            if self._locked:
                self._unlock()
            return
        best_r, best_P = fits

        # 若"较强拍点"能支撑一个明显更慢的网格，优先它：
        # 那才是主拍，而不是八分/十六分音符的细分（切分鼓点会拉快整格）
        if len(ons) >= 8:
            med_s = median(strs)
            strong = [x for x, s in zip(ons, strs) if s >= med_s]
            if len(strong) >= self.LOCK_MIN_ONSETS:
                fs = self._fit_grid(strong)
                if fs is not None and fs[0] >= self.LOCK_ALIGN_RATIO:
                    Ps = fs[1]
                    ratio = best_P and Ps / best_P
                    if ratio and 1.5 <= ratio <= 4.2:
                        best_P = Ps

        if best_r >= self.LOCK_ALIGN_RATIO:
            # 小幅漂移做平滑（跟得住速度变化）；大幅变化（如细分纠正）直接切换，
            # 否则平滑会把两个周期拉扯成中间值
            if self._locked and self._period and abs(best_P - self._period) <= 0.25 * self._period:
                best_P = 0.7 * self._period + 0.3 * best_P
            self._locked = True
            self._period = best_P
            self._bpm = 60.0 / best_P
            self._lock_expire = t + max(1.8, 2.5 * best_P)
        elif self._locked and t > self._lock_expire:
            self._unlock()

    def _fit_grid(self, ons: list[float]):
        """返回 (最佳对齐率, 对应周期)；无合适周期则 None。"""
        deltas = [ons[i + 1] - ons[i] for i in range(len(ons) - 1)]
        p0 = median(deltas)
        if not (self.PERIOD_MIN <= p0 <= self.PERIOD_MAX):
            return None
        anchor = ons[-1]
        best_P, best_r = p0, 0.0
        for k in range(-24, 25):                 # ±12%，步进 0.5%
            Pc = p0 * (1.0 + k * 0.005)
            if not (self.PERIOD_MIN <= Pc <= self.PERIOD_MAX):
                continue
            r = self._align_ratio(ons, Pc, anchor, self.PERIOD_TOL, self.PERIOD_TOL_ABS)
            if r > best_r:
                best_r, best_P = r, Pc
        return best_r, best_P

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
