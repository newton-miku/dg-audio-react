"""DG-Lab WebSocket 服务端封装：扫码绑定、断线自动重绑、强度数据回传、QR 生成。"""
from __future__ import annotations

import asyncio
import socket

from pydglab_ws import DGLabWSServer, RetCode, StrengthData

from .state import State

# 需要避开的"虚拟网卡/VPN"地址段：CGNAT(Tailscale/WireGuard 等常用) 与链路本地
_VPN_PREFIXES = ("100.64.", "100.65.", "100.66.", "100.67.", "100.68.", "100.69.",
                 "100.70.", "100.71.", "100.72.", "100.73.", "100.74.", "100.75.",
                 "100.76.", "100.77.", "100.78.", "100.79.", "100.8", "100.9",
                 "169.254.")
# 真实局域网优先段
_LAN_PREFIXES = ("192.168.", "10.", "172.16.", "172.17.", "172.18.", "172.19.",
                 "172.2", "172.30.", "172.31.")


def list_local_ips() -> list[str]:
    """枚举本机非回环 IPv4 地址（含默认路由接口），供界面挑选。"""
    ips: set[str] = set()
    try:
        for res in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = res[4][0]
            if not ip.startswith("127."):
                ips.add(ip)
    except Exception:  # noqa: BLE001
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:  # noqa: BLE001
        pass
    return sorted(ips)


def _is_vpn(ip: str) -> bool:
    return any(ip.startswith(p) for p in _VPN_PREFIXES)


def _is_lan(ip: str) -> bool:
    return any(ip.startswith(p) for p in _LAN_PREFIXES)


def default_lan_ip() -> str:
    """选一个手机可达的广播 IP：优先真实局域网；其次非 VPN；最后任意非回环。"""
    ips = list_local_ips()
    for ip in ips:
        if _is_lan(ip) and not _is_vpn(ip):
            return ip
    for ip in ips:
        if not _is_vpn(ip):
            return ip
    if ips:
        return ips[0]
    return "127.0.0.1"


def advertised_ip(cfg) -> str:
    """实际写进二维码/IP 显示的地址：手动指定优先，否则自动。"""
    manual = str(cfg.get("dg_ip", "") or "").strip()
    if manual:
        return manual
    return default_lan_ip()


class DGLab:
    def __init__(self, cfg, state: State) -> None:
        self.cfg = cfg
        self.state = state
        self.server = None
        self.client = None
        self.qr_text: str = ""
        self.candidates = list_local_ips()

    def ws_uri(self) -> str:
        return f"ws://{advertised_ip(self.cfg)}:{int(self.cfg.get('dglab_port', 56742))}"

    # ---------- 主协程（asyncio task）：端口被占/异常时自动重试 ----------
    async def run(self) -> None:
        port = int(self.cfg.get("dglab_port", 56742))
        while True:
            try:
                async with DGLabWSServer("0.0.0.0", port) as self.server:
                    self.state.update(dg_running=True, dg_msg="", status="等待 App 扫码绑定…")
                    self.client = self.server.new_local_client()
                    uri = self.ws_uri()
                    self.qr_text = self.client.get_qrcode(uri)
                    self.state.update(status=f"等待 App 扫码绑定…   ws://{advertised_ip(self.cfg)}:{port}")
                    self.state.poke()  # web 据此刷新 QR/状态
                    await self._serve()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.state.update(
                    dg_running=False,
                    status=f"DG-Lab 服务异常：{e}（5 秒后重试）",
                    dg_msg=str(e),
                )
                self.state.poke()
                try:
                    await asyncio.sleep(5)
                except asyncio.CancelledError:
                    raise

    async def _serve(self) -> None:
        """进入已启动的服务端：绑定 -> 数据循环。断开后自动 rebind。"""
        try:
            await self.client.bind()
        except Exception as e:  # noqa: BLE001
            self.state.update(status=f"绑定等待异常: {e}", dg_msg=str(e))
            raise
        self.state.update(bound=True, target=str(self.client.target_id), dg_msg="")
        self.state.poke()

        async for data in self.client.data_generator(StrengthData, RetCode):
            if isinstance(data, StrengthData):
                self.state.update(
                    a=int(data.a), b=int(data.b),
                    a_limit=int(data.a_limit), b_limit=int(data.b_limit),
                )
            elif data == RetCode.CLIENT_DISCONNECTED:
                self.state.update(bound=False, target="", status="App 断开，等待重连…")
                self.state.poke()
                try:
                    await self.client.rebind()
                    self.state.update(bound=True, target=str(self.client.target_id),
                                      status=f"已重连 {self.client.target_id}")
                except Exception:  # noqa: BLE001
                    pass
                self.state.poke()

    def stop(self) -> None:
        try:
            self.state.update(bound=False, dg_running=False, enabled=False)
            self.state.poke()
        except Exception:  # noqa: BLE001
            pass
