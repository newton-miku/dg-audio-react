"""郊狼3 · 电脑声音反应控制器 —— 入口。

用法：
  python run.py              正常启动（环回监听系统声音 + DG-Lab 服务）
  python run.py --devices    仅列出可用音频设备后退出
  python run.py --demo       无设备自检：合成音源跑通「分析->映射->调度->WebUI」
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import threading
import webbrowser

os.environ.setdefault("PYTHONUTF8", "1")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
if sys.stdout:
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except Exception:  # noqa: BLE001
        pass

from aiohttp import web  # noqa: E402

from app.capture import AudioCapture, device_label, find_device, list_devices, pick_default  # noqa: E402
from app.config import Config  # noqa: E402
from app.demo import DemoFeed  # noqa: E402
from app.dg import DGLab  # noqa: E402
from app.feed import LevelFeed  # noqa: E402
from app.scheduler import Scheduler  # noqa: E402
from app.state import State  # noqa: E402
from app.web import build_app  # noqa: E402


def cmd_devices() -> None:
    devs = list_devices()
    if not devs:
        print("没有发现可用的音频输入设备。")
        print("提示：若想监听系统声音，请确保已安装 pyaudiowpatch（WASAPI 环回），或启用“立体声混音/虚拟声卡”。")
        return
    for d in devs:
        kind = "环回(系统声音)" if d["loopback"] else "输入"
        print(f"  {device_label(d)}    [{kind}]  {d['rate']}Hz {d['ch']}ch")
    print("\n默认将自动选择:", pick_default(devs) and device_label(pick_default(devs)) or "(无)")


async def amain(demo: bool, cfg: Config) -> None:
    state = State()
    feed = LevelFeed()
    cap: AudioCapture | None = None
    demofeed: DemoFeed | None = None

    def start_capture(label: str):
        nonlocal cap, demofeed
        if demofeed:
            return True, "演示模式不需要音频设备"
        devs = list_devices()
        dev = find_device(label, devs) if label else pick_default(devs)
        if dev is None:
            cfg.set_many({"device": ""})
            if cap:
                cap.error = "没有可用的音频输入设备"
            return False, "未找到匹配的音频设备"
        if cap is None:
            cap = AudioCapture(feed)
        cap.start(dev)
        cfg.set_many({"device": label})
        sched.schedule_recal()
        return True, f"正在捕获：{device_label(dev)}"

    # --- 音频源 ---
    if demo:
        demofeed = DemoFeed(feed)
        demofeed.start()
    else:
        cap = AudioCapture(feed)
        devs = list_devices()
        dev = find_device(cfg.get("device", ""), devs) if cfg.get("device") else pick_default(devs)
        if dev is None:
            cap.error = "没有可用的音频输入设备：请在界面里选择，或安装 pyaudiowpatch/启用立体声混音"
        else:
            cap.start(dev)
            print("音频捕获:", device_label(dev))

    # --- DG-Lab 服务 + 调度 ---
    dg = DGLab(cfg, state)
    sched = Scheduler(cfg, state, feed, cap if not demo else None, dg)
    if not demo and cap and cap.device:
        sched.schedule_recal()

    # --- Web ---
    app = build_app(state, cfg, sched, dg, start_capture)
    runner = web.AppRunner(app)
    await runner.setup()
    host = str(cfg.get("web_host", "127.0.0.1"))
    port = int(cfg.get("web_port", 8900))
    site = web.TCPSite(runner, host, port)
    await site.start()
    url = f"http://{host}:{port}"
    print("=" * 60)
    print(f"  郊狼3 声音反应控制器已启动")
    print(f"  控制页: {url}   DG-Lab端口: {cfg.get('dglab_port')}")
    if demo:
        print("  演示模式：合成音源，未使用真实音频设备")
    print("  首次使用请先在手机 DG-Lab App 里把强度调低。")
    print("=" * 60)

    if cfg.get("open_browser", True):
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    tasks = [
        asyncio.create_task(dg.run(), name="dg"),
        asyncio.create_task(sched.run(), name="scheduler"),
    ]
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass
    finally:
        # 尽量给 scheduler 一次机会清空队列并把强度归零
        state.update(enabled=False)
        state.poke()
        try:
            await asyncio.sleep(0.4)
        except asyncio.CancelledError:
            pass
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await runner.cleanup()
        if cap:
            cap.stop()
        if demofeed:
            demofeed.stop()


def main() -> None:
    ap = argparse.ArgumentParser(description="郊狼3 · 电脑声音反应控制器")
    ap.add_argument("--devices", action="store_true", help="列出音频设备后退出")
    ap.add_argument("--demo", action="store_true", help="无设备自检（合成音源）")
    args = ap.parse_args()

    cfg = Config()
    if args.devices:
        cmd_devices()
        return
    try:
        asyncio.run(amain(demo=args.demo, cfg=cfg))
    except KeyboardInterrupt:
        print("\n已退出。")


if __name__ == "__main__":
    main()
