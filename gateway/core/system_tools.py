"""系统工具模块：Tailscale/VPN 状态与控制、内存释放、网络适配器、网关自身信息。

供网页控制台「系统工具」卡片使用。全部调用本机系统能力，不做网络外发。
Windows-only（依赖 ctypes/psutil 的 Windows 行为）。
"""

import ctypes
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from ctypes import wintypes
from datetime import datetime
from typing import Optional

import psutil


# ============================================================================
# Tailscale
# ============================================================================

_TAILSCALE_PATHS = [
    r"D:\Tailscale\tailscale.exe",
    r"C:\Program Files\Tailscale\tailscale.exe",
    r"C:\Program Files (x86)\Tailscale\tailscale.exe",
]

# tailscale status 缓存：<timestamp, payload>（5s TTL，避免每轮刷新都拉起进程）
_tailscale_cache: Optional[tuple] = None

_tailscale_exe: Optional[str] = None


def _find_tailscale() -> Optional[str]:
    global _tailscale_exe
    if _tailscale_exe:
        return _tailscale_exe
    found = shutil.which("tailscale")
    if found:
        _tailscale_exe = found
        return found
    for p in _TAILSCALE_PATHS:
        if os.path.isfile(p):
            _tailscale_exe = p
            return p
    return None


# 已知 VPN 网卡用途注释（展示用）
_VPN_NOTES = (
    ("tailscale", "局域网"),
    ("radmin", "游戏"),
    ("ust", "学校"),
    ("openvpn", "OpenVPN"),
    ("wireguard", "WireGuard"),
    ("zerotier", "ZeroTier"),
    ("hamachi", "Hamachi"),
)


def _vpn_note(name: str) -> str:
    n = name.lower()
    for key, note in _VPN_NOTES:
        if key in n:
            return note
    return ""


def _run_tailscale(args: list[str], timeout: int = 12) -> tuple[int, str]:
    exe = _find_tailscale()
    if not exe:
        return -1, "tailscale CLI 未找到"
    try:
        r = subprocess.run(
            [exe, *args],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return r.returncode, (r.stdout or "").strip() + (("\n" + r.stderr.strip()) if r.stderr.strip() else "")
    except subprocess.TimeoutExpired:
        return -1, "tailscale 命令超时"
    except Exception as e:
        return -1, str(e)


def invalidate_tailscale_cache():
    global _tailscale_cache
    _tailscale_cache = None


def tailscale_status() -> dict:
    """返回 Tailscale 连接状态（带 5s 缓存）。"""
    global _tailscale_cache
    now = time.time()
    if _tailscale_cache and now - _tailscale_cache[0] < 5:
        return _tailscale_cache[1]

    if not _find_tailscale():
        payload = {"available": False, "reason": "未找到 tailscale CLI"}
        _tailscale_cache = (now, payload)
        return payload

    out = {
        "available": True,
        "state": "unknown",
        "online": False,
        "hostname": "",
        "ips": [],
        "self_ip": "",
        "exit_node": None,
    }
    code, text = _run_tailscale(["status", "--json"])
    if code != 0:
        out["state"] = "Stopped"
        _tailscale_cache = (now, out)
        return out
    try:
        d = json.loads(text)
    except Exception:
        _tailscale_cache = (now, out)
        return out

    out["state"] = d.get("BackendState") or "unknown"
    self_ = d.get("Self") or {}
    out["online"] = bool(self_.get("Online"))
    out["hostname"] = (self_.get("DNSName") or "").rstrip(".")
    out["ips"] = self_.get("TailscaleIPs") or []
    v4 = [i for i in out["ips"] if ":" not in i]
    out["self_ip"] = v4[0] if v4 else ""
    es = d.get("ExitNodeStatus") or {}
    if es.get("Online"):
        out["exit_node"] = {
            "hostname": es.get("HostName") or "",
            "active": bool(es.get("ActiveExit")),
        }
    _tailscale_cache = (now, out)
    return out


def tailscale_action(action: str) -> tuple[int, str]:
    """执行 tailscale up / down。返回 (returncode, 输出文本)。"""
    invalidate_tailscale_cache()
    return _run_tailscale([action])


# ============================================================================
# Windows 内置 VPN（Get-VpnConnection / Connect/Disconnect-VpnConnection）
# 连接/断开是普通用户操作，不需要管理员权限。
# ============================================================================

def vpn_profiles() -> list[dict]:
    """列出 Windows 内置 VPN 配置及当前连接状态。"""
    ps = (
        "$ErrorActionPreference='Stop'; "
        "Get-VpnConnection | Select-Object Name, ServerAddress, TunnelType, ConnectionStatus "
        "| ConvertTo-Json -Compress"
    )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, encoding="utf-8", errors="replace", timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        return []
    if r.returncode != 0 or not r.stdout.strip():
        return []
    try:
        data = json.loads(r.stdout)
    except Exception:
        return []
    if isinstance(data, dict):
        data = [data]
    out = []
    for p in data:
        out.append({
            "name": p.get("Name", ""),
            "server": p.get("ServerAddress", ""),
            "tunnel": p.get("TunnelType", ""),
            "connected": p.get("ConnectionStatus") == "Connected",
        })
    return out


def vpn_action(name: str, connect: bool) -> dict:
    """连接/断开一个内置 VPN 配置。"""
    esc = name.replace("'", "''")
    verb = "Connect" if connect else "Disconnect"
    ps = f"$ErrorActionPreference='Stop'; {verb}-VpnConnection -Name '{esc}'"
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, encoding="utf-8", errors="replace", timeout=90,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as e:
        return {"ok": False, "detail": str(e)}
    if r.returncode == 0:
        return {"ok": True, "detail": f"VPN「{name}」已{'连接' if connect else '断开'}"}
    detail = (r.stderr or r.stdout or "").strip()
    return {"ok": False, "detail": detail or f"操作失败 (rc={r.returncode})"}


# ============================================================================
# 网络适配器
# ============================================================================

_VPN_KEYWORDS = (
    "tailscale", "radmin", "vpn", "openvpn", "wireguard", "zerotier",
    "hamachi", "nord", "anyconnect", "forti", "wintun", "tap", "utun",
)


def _is_vpn(name: str) -> bool:
    n = name.lower()
    return any(k in n for k in _VPN_KEYWORDS)


def list_adapters() -> list[dict]:
    """列出所有网络适配器（VPN 优先、在线优先排序）。"""
    addrs = psutil.net_if_addrs()
    out = []
    for nic, st in psutil.net_if_stats().items():
        ipv4 = []
        for a in addrs.get(nic, []):
            if a.family == socket.AF_INET:
                ipv4.append(a.address)
        is_vpn = _is_vpn(nic)
        out.append({
            "name": nic,
            "up": bool(st.isup),
            "speed": st.speed,
            "ipv4": ipv4,
            "vpn": is_vpn,
            "note": _vpn_note(nic) if is_vpn else "",
        })
    out.sort(key=lambda a: (not a["vpn"], not a["up"]))
    return out


# ============================================================================
# 内存释放（EmptyWorkingSet：把各进程工作集刷回物理内存，Windows 内置机制）
# ============================================================================

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_psapi = ctypes.WinDLL("psapi", use_last_error=True)

_PROCESS_QUERY_INFORMATION = 0x0400
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_PROCESS_SET_QUOTA = 0x0100

_psapi.EmptyWorkingSet.argtypes = [wintypes.HANDLE]
_psapi.EmptyWorkingSet.restype = wintypes.BOOL
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL


def free_memory() -> dict:
    """对所有可访问进程 EmptyWorkingSet，返回释放前后可用内存。"""
    before = psutil.virtual_memory().available
    n = 0
    for p in psutil.process_iter(["pid"]):
        try:
            h = _kernel32.OpenProcess(
                _PROCESS_QUERY_LIMITED_INFORMATION | _PROCESS_SET_QUOTA | _PROCESS_QUERY_INFORMATION,
                False,
                p.info["pid"],
            )
        except Exception:
            continue
        if not h:
            continue
        try:
            if _psapi.EmptyWorkingSet(h):
                n += 1
        except Exception:
            pass
        finally:
            _kernel32.CloseHandle(h)
    after = psutil.virtual_memory().available
    return {
        "before_gb": round(before / 1e9, 2),
        "after_gb": round(after / 1e9, 2),
        "freed_gb": round(max(0, after - before) / 1e9, 2),
        "processes": n,
    }


# ============================================================================
# 网关自身信息（本服务）
# ============================================================================


def gateway_info() -> dict:
    proc = psutil.Process(os.getpid())
    create = proc.create_time()
    return {
        "pid": proc.pid,
        "started": datetime.fromtimestamp(create).strftime("%Y-%m-%d %H:%M:%S"),
        "uptime_sec": int(time.time() - create),
        "mem_mb": round(proc.memory_info().rss / 1024 / 1024, 1),
        "python": ".".join(str(v) for v in sys.version_info[:3]),
        "hostname": socket.gethostname(),
    }


def session_processes(sessions) -> list[dict]:
    """返回运行中会话的进程资源占用（ttyd 进程 ≈ agent 子进程宿主）。"""
    out = []
    for s in sessions:
        if not getattr(s, "running", False) or not s.pid:
            continue
        try:
            p = psutil.Process(s.pid)
            with p.oneshot():
                cpu = p.cpu_percent(interval=None)
                mem_mb = p.memory_info().rss / 1024 / 1024
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        out.append({
            "session_id": s.id,
            "agent_key": s.agent_key,
            "status": s.status,
            "pid": s.pid,
            "port": s.port,
            "cpu_percent": round(cpu, 1),
            "mem_mb": round(mem_mb, 1),
        })
    return out


# ============================================================================
# 汇总
# ============================================================================


def get_system_report(sessions=None) -> dict:
    vm = psutil.virtual_memory()
    return {
        "mem": {
            "used_gb": round(vm.used / 1e9, 2),
            "avail_gb": round(vm.available / 1e9, 2),
            "total_gb": round(vm.total / 1e9, 2),
            "percent": vm.percent,
        },
        "adapters": list_adapters(),
        "tailscale": tailscale_status(),
        "vpn_profiles": vpn_profiles(),
        "processes": session_processes(sessions or []),
        "gateway": gateway_info(),
    }
