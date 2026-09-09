"""aiohttp Web 服务：控制页 + REST + WebSocket 遥测 + QR 图。"""
from __future__ import annotations

import asyncio
import io
import os

import qrcode
from aiohttp import web

from .capture import list_devices
from .dg import advertised_ip, default_lan_ip

WEBUI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webui")

# 允许前端保存的配置键
CONFIG_KEYS = {
    "mode", "beat_shape", "style", "chA", "chB", "ceilA", "ceilB",
    "sensitivity", "release_ms",
    "threshold_auto", "threshold_db",
    "boost_on", "boost_mode", "safety_cap",
}

_qr_cache: dict = {"text": None, "png": b""}


def _qr_png(text: str) -> bytes:
    if _qr_cache["text"] != text:
        img = qrcode.make(text, box_size=8)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        _qr_cache["text"] = text
        _qr_cache["png"] = buf.getvalue()
    return _qr_cache["png"]


def build_app(state, cfg, scheduler, dg, start_capture) -> web.Application:
    app = web.Application()

    async def index(_: web.Request) -> web.StreamResponse:
        return web.FileResponse(os.path.join(WEBUI_DIR, "index.html"))

    async def api_state(_: web.Request) -> web.Response:
        return web.json_response(state.snapshot())

    async def api_config(request: web.Request) -> web.Response:
        if request.method == "GET":
            return web.json_response(cfg.d)
        try:
            data = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "bad json"}, status=400)
        if not isinstance(data, dict):
            return web.json_response({"error": "bad body"}, status=400)
        allowed = {k: v for k, v in data.items() if k in CONFIG_KEYS}
        if cfg.set_many(allowed):
            state.poke()
        return web.json_response(cfg.d)

    async def api_devices(_: web.Request) -> web.Response:
        devs = list_devices()
        return web.json_response({"devices": devs, "selected": cfg.get("device", "")})

    async def api_net(_: web.Request) -> web.Response:
        candidates = dg.candidates if dg else []
        return web.json_response({
            "candidates": candidates,
            "chosen": cfg.get("dg_ip", ""),
            "auto": default_lan_ip(),
            "advertised": advertised_ip(cfg),
            "ws_url": dg.ws_uri() if dg else "",
        })

    async def api_cmd(request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "bad json"}, status=400)
        action = data.get("action")
        if action == "start":
            if not state.bound:
                return web.json_response({"error": "未绑定 App"}, status=409)
            state.update(enabled=True)
            state.poke()
            return web.json_response({"ok": True})
        if action == "stop":
            state.update(enabled=False)
            state.poke()
            return web.json_response({"ok": True})
        if action == "recal":
            scheduler.schedule_recal()
            return web.json_response({"ok": True})
        if action == "reset_boost":
            scheduler.reset_boost()
            return web.json_response({"ok": True})
        if action == "set_device":
            device = data.get("device", "")
            cfg.set_many({"device": device})
            ok, msg = start_capture(device)
            return web.json_response({"ok": ok, "msg": msg})
        if action == "set_ip":
            ip = str(data.get("ip", "") or "").strip()
            cfg.set_many({"dg_ip": ip})
            state.poke()
            return web.json_response({"ok": True, "ws_url": dg.ws_uri() if dg else ""})
        if action == "test":
            ch = str(data.get("ch", "A")).upper()
            if ch not in ("A", "B"):
                return web.json_response({"error": "ch 只能是 A/B"}, status=400)
            ok = await scheduler.test_wave(ch)
            if not ok:
                return web.json_response({"error": "未绑定或发送失败"}, status=409)
            return web.json_response({"ok": True})
        return web.json_response({"error": f"未知 action: {action}"}, status=400)

    async def api_qr(_: web.Request) -> web.StreamResponse:
        if not dg or dg.client is None:
            return web.json_response({"error": "服务尚未就绪"}, status=503)
        try:
            text = dg.client.get_qrcode(dg.ws_uri())
        except Exception:  # noqa: BLE001
            text = None
        if not text:
            return web.json_response({"error": "二维码尚未生成"}, status=404)
        return web.Response(body=_qr_png(text), content_type="image/png")

    async def ws_handler(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=10)
        await ws.prepare(request)
        try:
            while True:
                await ws.send_json(state.snapshot())
                await asyncio.sleep(0.1)
        except (asyncio.CancelledError, ConnectionError, RuntimeError):
            pass
        finally:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass
        return ws

    # 注册顺序：API 优先于静态，避免 /api 被静态兜底
    app.router.add_get("/", index)
    app.router.add_get("/api/state", api_state)
    app.router.add_get("/api/config", api_config)
    app.router.add_post("/api/config", api_config)
    app.router.add_get("/api/devices", api_devices)
    app.router.add_get("/api/net", api_net)
    app.router.add_post("/api/cmd", api_cmd)
    app.router.add_get("/api/qr.png", api_qr)
    app.router.add_get("/ws", ws_handler)
    app.router.add_static("/static/", WEBUI_DIR)
    return app
