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
    # 二维码/提示里显示的地址："" = 自动挑选真实局域网 IP（跳过 VPN）
    "dg_ip": "",
    # 广播给手机扫码的 IP："" = 自动选真实局域网地址（避开 VPN/虚拟网卡）
    "dg_ip": "",
    # Web 控制页
    "web_host": "127.0.0.1",
    "web_port": 8900,
    # 音频设备："" = 自动选"默认播放设备"的环回端点（系统声音）
    "device": "",
    # 响应模式: beat(节拍脉冲,默认,寸止用) / hybrid / follow
    "mode": "beat",
    # 通道
    "chA": True,
    "chB": True,
    # 每通道输出主强度上限（0-200，也受手机 App 侧上限约束）
    "ceilA": 50,
    "ceilB": 50,
    # 阈值：在自动底噪之上再抬高多少 dB 才算有声音（0-30）
    "threshold": 0.0,
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
