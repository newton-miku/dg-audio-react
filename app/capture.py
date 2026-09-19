"""Windows 声音捕获后端（回调式）。

主：pyaudiowpatch —— WASAPI 环回（抓"系统正在播放的声音"）+ 普通输入设备。
备：sounddevice —— 仅普通输入设备（pyaudiowpatch 不可用时退化）。

要点：
- 用 stream_callback 而非阻塞 read，空闲/无声端点不会卡死线程；
- 看门狗：超过阈值没有数据回调 -> 报错提示换设备；
- PortAudio 对象生命周期封闭在捕获线程内。
"""
from __future__ import annotations

import math
import threading
import time
from typing import Callable, Optional

import numpy as np

from .feed import LevelFeed

_pa = None
_sd = None

HINT_S = 1.2   # 超过这么久无数据回调 -> 标记"等待声音（静音）"


def _import_pa():
    global _pa
    if _pa is None:
        try:
            import pyaudiowpatch  # type: ignore

            _pa = pyaudiowpatch
        except Exception:
            _pa = False
    return _pa or None


def _import_sd():
    global _sd
    if _sd is None:
        try:
            import sounddevice  # type: ignore

            _sd = sounddevice
        except Exception:
            _sd = False
    return _sd or None


def have_loopback() -> bool:
    return _import_pa() is not None


def _mono_rms(x: np.ndarray) -> float:
    if x.size == 0:
        return 0.0
    x = x.reshape(-1)
    return float(np.sqrt(np.mean(np.square(x), dtype=np.float64)))


def _db(rms: float) -> float:
    if rms <= 1e-9:
        return -120.0
    return max(-120.0, float(20.0 * math.log10(rms)))


# ---------- 频谱 / 低频（供界面频谱图 + 音乐节拍检测）----------
SPEC_BANDS = 40          # 频段数
SPEC_FMIN = 40.0         # 起始频率 Hz
SPEC_FMAX = 16000.0      # 最高频率 Hz
SPEC_FLOOR_DB = -75.0    # 该 dB 以下视为 0
SPEC_TOP_DB = -12.0      # 该 dB 视为满格
LOW_FMIN = 35.0          # "低频"频段（底鼓/贝斯）
LOW_FMAX = 250.0


def band_edges(rate: int, n: int = SPEC_BANDS) -> list[float]:
    """对数分布的频段边界（Hz）。"""
    hi = min(SPEC_FMAX, rate / 2.0 * 0.95)
    lo = SPEC_FMIN
    if hi <= lo:
        return []
    import math as _m
    return [_m.exp(_m.log(lo) + (_m.log(hi) - _m.log(lo)) * i / n) for i in range(n + 1)]


def frame_fft(x: np.ndarray, rate: int, n: int = 1024):
    """一块音频 -> (幅度谱, 各 bin 频率)。太小/空则返回 (None, None)。"""
    if x.size < 128:
        return None, None
    size = 1
    while size * 2 <= x.size and size < n:
        size *= 2
    seg = x[-size:] * np.hanning(size)
    mag = np.abs(np.fft.rfft(seg)) * (2.0 / size)
    return mag, np.fft.rfftfreq(size, 1.0 / rate)


def bands_from_fft(mag: np.ndarray, freqs: np.ndarray, edges: list[float]) -> list[float]:
    """幅度谱 -> 各频段响度 0..1（对数值）。"""
    if mag is None or len(edges) < 2:
        return []
    out: list[float] = []
    for i in range(len(edges) - 1):
        lo, hi = edges[i], edges[i + 1]
        sel = (freqs >= lo) & (freqs < hi)
        if not sel.any():
            out.append(0.0)
            continue
        v = float(mag[sel].max())
        db = -120.0 if v <= 1e-9 else 20.0 * math.log10(v)
        t = (db - SPEC_FLOOR_DB) / (SPEC_TOP_DB - SPEC_FLOOR_DB)
        out.append(round(max(0.0, min(1.0, t)), 3))
    return out


def low_db_from_fft(mag: np.ndarray, freqs: np.ndarray,
                    fmin: float = LOW_FMIN, fmax: float = LOW_FMAX) -> float:
    """低频段能量 -> dB（底鼓/贝斯，用于音乐节拍检测）。"""
    if mag is None:
        return -120.0
    sel = (freqs >= fmin) & (freqs <= fmax)
    if not sel.any():
        return -120.0
    e = float(np.sqrt(np.sum(mag[sel] ** 2)))
    return _db(e)


def spectrum(x: np.ndarray, rate: int, edges: list[float]) -> list[float]:
    """兼容入口：单声道波形 -> 各频段响度（内部各算一次 FFT）。"""
    mag, freqs = frame_fft(x, rate)
    if mag is None:
        return []
    return bands_from_fft(mag, freqs, edges)


def device_label(dev: dict) -> str:
    return ("[环回] " if dev.get("loopback") else "[输入] ") + dev["name"]


def find_device(label: str, devices: list[dict]):
    """按 label 找设备（环回优先精确匹配）。"""
    if label:
        for d in devices:
            if device_label(d) == label:
                return d
    return None


def pick_default(devices: list[dict]):
    """自动选择：默认播放设备对应的环回端点；否则第一个环回；否则第一个输入。"""
    pa = _import_pa()
    if pa:
        try:
            p = pa.PyAudio()
            try:
                out_name = p.get_default_output_device_info().get("name")
            finally:
                p.terminate()
            if out_name:
                for d in devices:
                    if d.get("loopback") and d["name"] == out_name:
                        return d
        except Exception:  # noqa: BLE001
            pass
    for d in devices:
        if d.get("loopback"):
            return d
    for d in devices:
        if not d.get("loopback"):
            return d
    return None


def list_devices() -> list[dict]:
    """返回 [{id, name, loopback, rate, ch}]。环回端点排前。"""
    out: list[dict] = []
    seen: set = set()

    pa = _import_pa()
    if pa:
        p = pa.PyAudio()
        try:
            for info in p.get_loopback_device_info_generator():
                key = (info["name"], True)
                if key not in seen:
                    seen.add(key)
                    out.append(
                        {
                            "id": info["index"],
                            "name": info["name"],
                            "loopback": True,
                            "rate": int(info.get("defaultSampleRate") or 48000),
                            "ch": int(info.get("maxInputChannels") or 2),
                        }
                    )
            for i in range(p.get_device_count()):
                info = p.get_device_info_by_index(i)
                if info["maxInputChannels"] <= 0 or info.get("isLoopbackDevice"):
                    continue
                key = (info["name"], False)
                if key not in seen:
                    seen.add(key)
                    out.append(
                        {
                            "id": info["index"],
                            "name": info["name"],
                            "loopback": False,
                            "rate": int(info.get("defaultSampleRate") or 48000),
                            "ch": int(info.get("maxInputChannels") or 1),
                        }
                    )
        finally:
            p.terminate()
        return out

    sd = _import_sd()
    if sd:
        try:
            for i in range(len(sd.query_devices())):
                info = sd.query_devices(i)
                if info["max_input_channels"] <= 0:
                    continue
                out.append(
                    {
                        "id": i,
                        "name": info["name"],
                        "loopback": False,
                        "rate": int(info.get("default_samplerate") or 48000),
                        "ch": int(info.get("max_input_channels") or 1),
                    }
                )
        except Exception:  # noqa: BLE001
            pass
        return out
    return []


class AudioCapture:
    """回调式捕获。start(dev) 后在独立线程里打开设备，每个音频块转 dB 推入 feed。"""

    def __init__(self, feed: LevelFeed, on_error: Optional[Callable[[str], None]] = None):
        self._feed = feed
        self._on_error = on_error or (lambda m: None)
        self._stop_evt = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.device: Optional[dict] = None
        self.error: str = ""
        self.running = False
        self.silent = True           # 最近没有数据回调（如环回在静音时停流）
        self.active_dev_label: str = ""
        # 回调侧状态
        self._dtype = np.dtype("float32")
        self._scale = 1.0
        self._last_push = 0.0
        self._cont = None
        # 频谱
        self._rate: int = 48000
        self._edges: list[float] = []
        self._spec_tick = 0
        self._spec_every = 4          # 每 4 个音频块算一次（约 50ms）

    @property
    def frames_per_buf(self) -> int:
        rate = (self.device or {}).get("rate") or 48000
        return max(256, int(rate * 12 / 1000.0))

    # ---------- 公开 ----------
    def start(self, dev: dict) -> None:
        self.stop()
        self._stop_evt.clear()
        self.device = dev
        self.error = ""
        self.silent = True
        self.running = True
        self._rate = int(dev.get("rate") or 48000)
        self._edges = []              # 采样率可能变，重算频段
        self._thread = threading.Thread(target=self._run, daemon=True, name="audio-capture")
        self._thread.start()

    def stop(self) -> None:
        self.running = False
        self._stop_evt.set()
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._feed.clear()

    # ---------- 线程体 ----------
    def _run(self) -> None:
        dev = self.device
        fpb = self.frames_per_buf
        pa = _import_pa()
        sd = _import_sd()
        if not pa and not sd:
            self._fail("没有可用的音频后端（需 pyaudiowpatch 或 sounddevice）")
            return
        if pa and dev.get("loopback"):
            # 环回只有 pyaudiowpatch 支持，不退回输入设备
            self._run_pa(pa, dev, fpb)
            return
        if pa:
            if self._run_pa(pa, dev, fpb):
                return
        if sd:
            self._run_sd(sd, dev, fpb)

    def _run_pa(self, pa, dev: dict, fpb: int) -> bool:
        p = pa.PyAudio()
        stream = None
        dtype = np.dtype("float32")
        scale = 1.0
        try:
            for fmt, dt, sc in ((pa.paFloat32, np.dtype("float32"), 1.0),
                                (pa.paInt16, np.dtype("int16"), 1.0 / 32768.0)):
                try:
                    stream = p.open(
                        format=fmt, channels=dev["ch"], rate=dev["rate"], input=True,
                        input_device_index=dev["id"], frames_per_buffer=fpb,
                        stream_callback=self._callback,
                    )
                    dtype, scale = dt, sc
                    break
                except Exception:  # noqa: BLE001
                    continue
            if stream is None:
                self._fail(f"无法打开设备：{dev['name']}")
                return False
            self._dtype, self._scale, self._cont = dtype, scale, pa.paContinue
            self._last_push = time.monotonic()
            self.active_dev_label = dev["name"]
            self.error = ""
            self._keep_alive()
            return True
        except Exception as e:  # noqa: BLE001
            self._fail(f"捕获出错（{dev['name']}）：{e}")
            return False
        finally:
            try:
                if stream is not None:
                    stream.stop_stream()
                    stream.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                p.terminate()
            except Exception:  # noqa: BLE001
                pass

    def _run_sd(self, sd, dev: dict, fpb: int) -> None:
        try:
            with sd.InputStream(
                samplerate=dev["rate"], blocksize=fpb, device=dev["id"],
                channels=dev["ch"], dtype="float32", callback=self._sd_callback,
            ) as stream:
                self._dtype = np.dtype("float32")
                self._scale = 1.0
                self._last_push = time.monotonic()
                self.active_dev_label = dev["name"]
                self.error = ""
                self._keep_alive()
        except Exception as e:  # noqa: BLE001
            self._fail(f"捕获出错（{dev['name']}）：{e}")

    # ---------- 回调 ----------
    def _keep_alive(self) -> None:
        """流已开：保持线程存活等待数据。环回静音时会停流 -> 标记 silent 供 UI 提示。"""
        while not self._stop_evt.is_set():
            idle = (time.monotonic() - self._last_push) > HINT_S
            if idle != self.silent:
                self.silent = idle
                if not idle:
                    self.error = ""
            self._stop_evt.wait(0.25)

    def _callback(self, in_data, frame_count, time_info, status):
        try:
            self._ingest(in_data)
        except Exception:  # noqa: BLE001
            pass
        return (None, self._cont)

    def _sd_callback(self, in_data, frames, time_info, status):
        try:
            self._ingest(in_data)
        except Exception:  # noqa: BLE001
            pass

    def _ingest(self, data) -> None:
        if data is None or (hasattr(data, "__len__") and len(data) == 0):
            return
        if isinstance(data, np.ndarray):
            a = data.astype(np.float64) * self._scale
        else:
            a = np.frombuffer(data, dtype=self._dtype).astype(np.float64) * self._scale
        if a.ndim > 1:
            a = a.mean(axis=1)
        a = a.reshape(-1)
        db = _db(_mono_rms(a))

        # 频谱 / 低频：一次 FFT 同时得到两者（低频每块算，频段图隔几块算）
        low = -120.0
        self._spec_tick += 1
        try:
            mag, freqs = frame_fft(a, int(self._rate or 48000))
            if mag is not None:
                low = low_db_from_fft(mag, freqs)
                if self._spec_tick % self._spec_every == 0:
                    if not self._edges:
                        self._edges = band_edges(int(self._rate or 48000))
                    b = bands_from_fft(mag, freqs, self._edges)
                    if b:
                        self._feed.push_spec(b)
        except Exception:  # noqa: BLE001
            pass

        self._feed.push(db, low)
        self._last_push = time.monotonic()
        if self.silent:
            self.silent = False

    def _fail(self, msg: str) -> None:
        self.error = msg
        self.running = False
        self._on_error(msg)


def scan_working(cap: "AudioCapture", feed: LevelFeed, candidates: list[dict],
                 probe_s: float = 1.1):
    """自动模式：逐个试开设备，第一个在 probe_s 内能出数据的就保留。

    静音/空闲的虚拟端点常常不出数据 -> 自动跳过，换下一个。返回选中的设备或 None。
    """
    cap.stop()
    for dev in candidates:
        cap.start(dev)
        t0 = time.monotonic()
        while time.monotonic() - t0 < probe_s and not cap._stop_evt.is_set():
            if len(feed) > 0:
                return dev
            time.sleep(0.03)
        cap.stop()
    cap.stop()
    return None


def ordered_candidates(devices: list[dict]) -> list[dict]:
    """自动模式候选顺序：猜的默认在前，其后所有环回，最后普通输入，去重。"""
    out: list[dict] = []
    seen = set()

    def add(d):
        if d is not None and id(d) not in seen:
            seen.add(id(d))
            out.append(d)

    add(pick_default(devices))
    for d in devices:
        if d.get("loopback"):
            add(d)
    for d in devices:
        if not d.get("loopback"):
            add(d)
    return out
