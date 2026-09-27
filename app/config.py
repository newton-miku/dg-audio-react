"""配置：读/写 config.json，扁平 dict，未知新键用默认值补齐。"""
from __future__ import annotations

import copy
import json
import os

CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")

# 默认配置（数值含义见 README）
DEFAULTS: dict = {
    # DG-Lab WebSocket 服务（手机 App 扫码连接）
    "dglab_port": 56742,
    # 广播给手机扫码的 IP："" = 自动选真实局域网地址（避开 VPN/虚拟网卡）
    "dg_ip": "",
    # Web 控制页
    "web_host": "127.0.0.1",
    "web_port": 8900,
    # 音频设备："" = 自动选"默认播放设备"的环回端点（系统声音）
    "device": "",
    # A/B 是否联动（联动时 B 跟随 A 的设置）
    "ab_link": True,
    # 每通道波形来源：map=自建映射 / official=官方波形
    "srcA": "map", "srcB": "map",
    # 官方波形（srcX=official 时生效，见 app/waveforms.py 的 key）
    "waveA": "BREATHING", "waveB": "RHYTHM",
    # 每通道独立：响应模式 / 质感 / 拍形  (beat|hybrid|follow) (deep|mid|tingle) (sharp|double|triple|knead|swell)
    "modeA": "beat", "modeB": "beat",
    "styleA": "mid", "styleB": "tingle",
    "shapeA": "sharp", "shapeB": "double",
    # 旧版全局键（仅用于读取老配置时迁移）
    "mode": "beat",
    "beat_shape": "sharp",
    "style": "mid",
    # 通道
    "chA": True,
    "chB": True,
    # 每通道输出主强度上限（0-200，也受手机 App 侧上限约束）
    "ceilA": 50,
    "ceilB": 50,
    # 门限：threshold_auto=True 自动跟随环境底噪；False 时用 threshold_db(绝对 dB)
    "threshold_auto": True,
    "threshold_db": -50.0,
    # 自动增强（持续达标逐步加强度）
    "boost_on": False,
    # 达标停止后：recover=自动逐步恢复 / hold=一直保持直到手动恢复
    "boost_mode": "recover",
    # 安全硬上限（每通道独立，0-200）：无论手动/自动/测试波形都不超过的输出强度
    "safety_capA": 160,
    "safety_capB": 160,
    # 旧版全局安全上限（仅用于读取老配置时迁移）
    "safety_cap": 160,
    # 灵敏度：0-100，越高"曲线越陡"（小音量相对更弱、大音量更冲）
    "sensitivity": 45,
    # 音量包络释放时间 ms（越大越平滑/粘连，越小反应越脆）
    "release_ms": 220,
    # 质感预设: deep / mid / tingle
    "style": "mid",
    # 是否自动打开浏览器
    "open_browser": True,
}


class Config:
    def __init__(self, path: str = CONFIG_PATH):
        self.path = path
        self.d: dict = copy.deepcopy(DEFAULTS)
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            self.save()
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            self.save()
            return
        if isinstance(data, dict):
            for k, v in data.items():
                if k in DEFAULTS:
                    self.d[k] = v
            self._migrate(data)

    def _migrate(self, data: dict) -> None:
        """老配置迁移：① 全局 mode/style/beat_shape -> 每通道；② 全局 safety_cap -> 每通道。"""
        changed = False
        if not any(k in data for k in ("modeA", "modeB", "styleA", "styleB", "shapeA", "shapeB")):
            legacy = {"mode": "modeA", "style": "styleA", "beat_shape": "shapeA"}
            hit = False
            for old, new in legacy.items():
                if old in data:
                    self.d[new] = data[old]
                    self.d[new[:-1] + "B"] = data[old]
                    hit = True
            if hit:
                self.d["ab_link"] = True
                changed = True
        # 只有全局 safety_cap 的老配置：原值复制到 A/B，避免被默认 160 抬高
        if "safety_capA" not in data and "safety_capB" not in data and "safety_cap" in data:
            v = int(min(200, max(0, float(data["safety_cap"]))))
            self.d["safety_capA"] = v
            self.d["safety_capB"] = v
            changed = True
        if changed:
            self.save()

    def save(self) -> None:
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.d, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def get(self, key: str, default=None):
        return self.d.get(key, default if default is not None else DEFAULTS.get(key))

    def set_many(self, mapping: dict, save: bool = True) -> bool:
        """写入白名单内的键。返回是否有变化。"""
        changed = False
        for k, v in mapping.items():
            if k not in DEFAULTS:
                continue
            if self.d.get(k) != v:
                self.d[k] = v
                changed = True
        if changed and save:
            self.save()
        return changed
