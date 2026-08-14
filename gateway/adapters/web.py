"""网页控制台适配器。

基于 FastAPI 的控制面板，是 Agent Gateway 的唯一入口。
调用 gateway/core/router.py 的共享逻辑。

安全: 全部 /api/* 需要 cookie token 认证。登录后种 cookie。
WebSocket 端点也在 accept 前校验 cookie。
"""

import json
import mimetypes
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, File, Form, HTTPException, Response, UploadFile, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from gateway.core.runtime import collect_stats
from gateway.core.session import get_session_manager
from gateway.core.registry import get_agent_registry
from gateway.core.agent_proc import AgentProcessManager, get_agent_process_manager
from gateway.core.system_tools import (
    get_system_report,
    free_memory,
    tailscale_action,
    vpn_action,
)
from gateway.core.router import process_message, get_user_mode
from gateway.core.runtime_watch import (
    get_notifications_enabled,
    get_runtime_watcher,
    set_notifications_enabled,
)
from gateway.core.ttyd_relay import get_ttyd_relay_manager
from gateway.core.gotify_proc import get_gotify_manager

# ============================================================================
# 常量与配置
# ============================================================================

WEB_USER = "web"  # 网页面板统一的用户标识
_COOKIE_NAME = "gateway_token"

_DEFAULT_TOKEN = "jinxi-gateway"


def _get_token() -> str:
    """读取 GATEWAY_TOKEN：优先 os.environ，其次直接解析项目 .env 文件。"""
    token = os.getenv("GATEWAY_TOKEN")
    if token:
        return token
    env_path = Path(__file__).parent.parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                if key.strip() == "GATEWAY_TOKEN":
                    return value.strip()
    print(f"[Gateway-Web] WARNING: GATEWAY_TOKEN 未配置，使用默认 token '{_DEFAULT_TOKEN}'。请在 .env 中设置。")
    return _DEFAULT_TOKEN


_TOKEN = _get_token()

_INDEX_PATH = Path(__file__).parent / "webui" / "index.html"
# 独立终端页（手机优化，直连 ttyd WS）：/term?session=<id>
_TERM_PATH = Path(__file__).parent / "webui" / "term.html"
# 独立文件管理页（大区域浏览 + 手机上传/下载，当数据传输用）
_FILES_PATH = Path(__file__).parent / "webui" / "files.html"
_WEBUI_DIR = Path(__file__).parent / "webui"

# 前端静态资源（xterm.js 等）
_STATIC_FILES = {
    "xterm.min.js": "application/javascript",
    "xterm.min.css": "text/css",
    "xterm-addon-fit.min.js": "application/javascript",
    "favicon.ico": "image/x-icon",
}

# ============================================================================
# 单例
# ============================================================================

sm = get_session_manager()
registry = get_agent_registry()
proc_mgr = get_agent_process_manager()
relay_mgr = get_ttyd_relay_manager()

# 启动运行时状态轮询 daemon（读 agent 的 runtime 文件、推送通知、rediscovery）
get_runtime_watcher()
# 确保 Gotify 推送服务在跑（手机通知依赖它；没在跑则自动拉起）
get_gotify_manager().ensure_running()


# ============================================================================
# 认证依赖
# ============================================================================


async def require_auth(gateway_token: Optional[str] = Cookie(None)):
    """Cookie 认证依赖。未登录返回 401。"""
    if gateway_token != _TOKEN:
        raise HTTPException(status_code=401, detail="未授权")


# ============================================================================
# 请求模型
# ============================================================================


class LoginBody(BaseModel):
    token: str


class CreateSessionBody(BaseModel):
    agent_name: str
    project: str = ""


class AttachBody(BaseModel):
    session_id: str


class CommandBody(BaseModel):
    text: str


class InputBody(BaseModel):
    text: str


class VpnBody(BaseModel):
    name: str
    action: str  # connect | disconnect


class NotifyBody(BaseModel):
    enabled: bool


# ============================================================================
# 文件服务工具
# ============================================================================


def _agent_file_roots(agent, project: str) -> list[Path]:
    """解析 agent 配置的 file_roots，替换占位符。"""
    project = project or agent.default_project or ""
    subs = [AgentProcessManager._sub(agent, r, project) for r in agent.file_roots]
    return [Path(r) for r in subs if r]


def _within(base: Path, target: Path) -> bool:
    """target 是否在 base 目录内（防路径穿越）。"""
    base = base.resolve()
    target = target.resolve()
    try:
        target.relative_to(base)
        return True
    except ValueError:
        return False


def _is_abs(path: str) -> bool:
    """判断是否为 Windows 绝对路径（盘符 D:/ 或 UNC \\\\server\\share）。"""
    return (
        path.startswith("\\\\")
        or (len(path) >= 3 and path[1] == ":" and path[0].isalpha() and path[2] in "/\\")
    )


def _resolve_file(agent, project: str, path: str):
    """把 API 的 path 解析为 (root_dir, full_path)。

    - path="" → 顶层（root=None, full 空），列出所有 file_root
    - path 是绝对路径（D:/… 或 \\\\server\\share）→ 直接浏览该目录（root=None，
      用于从项目根往上级/全盘找文件）
    - 其余 → path 第一段是 file_root 目录名，其后为相对路径（仍受越界保护）
    """
    roots = _agent_file_roots(agent, project)
    if not roots:
        raise HTTPException(status_code=404, detail="该 Agent 未配置文件目录")

    # 校验 project（防止用 project 参数穿越到任意目录）
    if project and agent.projects and project not in agent.projects:
        raise HTTPException(status_code=400, detail="未知项目")

    if not path:
        return None, Path(""), roots

    if _is_abs(path):
        return None, Path(path).resolve(), roots

    parts = path.split("/")
    root = next((r for r in roots if r.name == parts[0]), None)
    if root is None:
        raise HTTPException(status_code=403, detail="路径越界")
    rel = Path(*parts[1:])
    full = (root / rel).resolve()
    if not _within(root, full):
        raise HTTPException(status_code=403, detail="路径越界")
    return root, full, roots


def _item_path(root_name: str, root: Path, full: Path, name: str) -> str:
    """把文件拼回 API 相对路径（rootname/.../name）。"""
    rel_parts = full.relative_to(root).parts
    return "/".join([root_name, *rel_parts, name])


def _abs_item_path(full: Path, name: str) -> str:
    """把绝对目录下的文件拼成 API 绝对路径（正斜杠，方便放 query）。"""
    return str((full / name).resolve()).replace("\\", "/")


# ============================================================================
# 路由
# ============================================================================

router = APIRouter()


@router.get("/")
async def index():
    """控制面板页面。"""
    return FileResponse(_INDEX_PATH)


@router.get("/term")
async def term_page():
    """独立终端页（每 session 一个，手机优化，直连 ttyd WS）。

    通过 ?session=<id> 指定会话；页面自行 fetch /api/sessions 拿 port。
    未登录时页面会跳回 / 登录。
    """
    return FileResponse(_TERM_PATH)


@router.get("/files")
async def files_page():
    """独立文件管理页（大区域浏览 + 手机上传/下载）。?agent=<key>"""
    return FileResponse(_FILES_PATH)


@router.get("/{static_file}")
async def webui_static(static_file: str):
    """前端静态资源（xterm.js / css / fit addon）。"""
    if static_file not in _STATIC_FILES:
        raise HTTPException(status_code=404, detail="Not Found")
    return FileResponse(
        _WEBUI_DIR / static_file,
        media_type=_STATIC_FILES[static_file],
    )


@router.post("/api/login")
async def login(body: LoginBody, response: Response):
    """校验 token，种 cookie。"""
    if body.token != _TOKEN:
        raise HTTPException(status_code=401, detail="token 错误")
    response.set_cookie(
        _COOKIE_NAME,
        _TOKEN,
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 30,  # 30 天
    )
    return {"ok": True}


# ---- 状态与列表 ----


@router.get("/api/status", dependencies=[Depends(require_auth)])
async def api_status():
    """面板状态卡片 + 图表数据。"""
    stats = collect_stats()
    return {
        "gateway": "online",
        "cpu_percent": stats["cpu_percent"],
        "memory_used_gb": stats["memory_used_gb"],
        "memory_total_gb": stats["memory_total_gb"],
        "boot_time": stats["boot_time"],
        "agent_count": sum(1 for a in registry.list_agents() if not a.hidden),
        "session_count": sm.count(),
        "mode": get_user_mode(WEB_USER),
    }


@router.get("/api/agents", dependencies=[Depends(require_auth)])
async def api_agents():
    result = []
    for agent in registry.list_agents():
        d = agent.to_dict()
        d["running_count"] = proc_mgr.running_count(agent.key)
        result.append(d)
    return result


@router.get("/api/sessions", dependencies=[Depends(require_auth)])
async def api_sessions():
    # 先把进程实际退出状态同步到 Session（处理 crash）
    for s in sm.list_sessions():
        sm.sync_process_status(s.id)
    return [s.to_dict() for s in sm.list_sessions()]


# ---- 会话生命周期 ----


@router.post("/api/sessions", dependencies=[Depends(require_auth)])
async def api_create_session(body: CreateSessionBody):
    agent = registry.get_agent(body.agent_name)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent 不存在: {body.agent_name}")
    if not agent.is_online:
        raise HTTPException(status_code=409, detail=f"Agent '{agent.name}' 当前离线")
    if body.project and agent.projects and body.project not in agent.projects:
        raise HTTPException(status_code=400, detail=f"未知项目: {body.project}")

    session = sm.create_session(
        name=agent.name,
        agent=agent.name,
        user_id=WEB_USER,
        agent_key=body.agent_name,
        project=body.project or agent.default_project or None,
    )
    return session.to_dict()


@router.post("/api/sessions/{session_id}/stop", dependencies=[Depends(require_auth)])
async def api_stop_session(session_id: str):
    """停止进程但保留 Session（可继续看日志）。"""
    if sm.stop_session(session_id):
        session = sm.get_session(session_id)
        return {"ok": True, "session_id": session_id, "session": session.to_dict() if session else None}
    raise HTTPException(status_code=404, detail=f"Session 不存在: {session_id}")


@router.delete("/api/sessions/{session_id}", dependencies=[Depends(require_auth)])
async def api_destroy_session(session_id: str):
    """停止并彻底删除 Session。"""
    if sm.destroy_session(session_id):
        return {"ok": True, "session_id": session_id}
    raise HTTPException(status_code=404, detail=f"Session 不存在: {session_id}")


@router.post("/api/sessions/{session_id}/input", dependencies=[Depends(require_auth)])
async def api_session_input(session_id: str, body: InputBody):
    """向运行中的 Session 注入一行输入（远程回复/建议按钮用）。

    走常驻中继转发给 ttyd（不能直连 ttyd——那会创建独立会话）。
    """
    session = sm.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail=f"Session 不存在: {session_id}")
    if session.external or not session.pid or not session.port:
        raise HTTPException(status_code=409, detail="该会话无法注入输入（外部会话或进程未运行）")
    relay = relay_mgr.get(session_id)
    if relay is None or not relay.running:
        raise HTTPException(status_code=409, detail="终端中继未就绪")
    ok = await relay.send_input((body.text + "\r\n").encode("utf-8"))
    if not ok:
        sm.sync_process_status(session_id)
        raise HTTPException(status_code=409, detail="进程已退出，无法注入输入")
    return {"ok": True}


# ---- 用户态切换 ----


@router.post("/api/attach", dependencies=[Depends(require_auth)])
async def api_attach(body: AttachBody):
    reply = process_message(WEB_USER, f"attach {body.session_id}")
    return {
        "ok": True,
        "reply": reply,
        "mode": get_user_mode(WEB_USER),
    }


@router.post("/api/detach", dependencies=[Depends(require_auth)])
async def api_detach():
    reply = process_message(WEB_USER, "detach")
    return {
        "ok": True,
        "reply": reply,
        "mode": get_user_mode(WEB_USER),
    }


# ---- 终端 ----


@router.post("/api/command", dependencies=[Depends(require_auth)])
async def api_command(body: CommandBody):
    reply = process_message(WEB_USER, body.text)
    return {
        "ok": True,
        "reply": reply,
        "mode": get_user_mode(WEB_USER),
    }


# ---- 系统工具（VPN/Tailscale、内存释放、设备、本服务） ----


@router.get("/api/system", dependencies=[Depends(require_auth)])
async def api_system():
    """系统工具卡片数据：内存、网络适配器、Tailscale 状态、运行中会话进程、网关自身信息、通知开关。"""
    report = get_system_report(sm.list_sessions())
    report["notifications_enabled"] = get_notifications_enabled()
    return report


@router.post("/api/system/notify", dependencies=[Depends(require_auth)])
async def api_set_notify(body: NotifyBody):
    """通知主开关（面板「通知提醒」）：关闭一律不推，开启后按"你在不在看"推。"""
    return {"ok": True, "enabled": set_notifications_enabled(body.enabled)}


@router.post("/api/system/memfree", dependencies=[Depends(require_auth)])
async def api_memfree():
    """释放内存：EmptyWorkingSet 所有可访问进程。"""
    return free_memory()


@router.post("/api/system/tailscale/up", dependencies=[Depends(require_auth)])
async def api_tailscale_up():
    """连接 Tailscale（只提供连接，不提供断开——断开会让手机/电脑失联）。"""
    code, text = tailscale_action("up")
    return {"ok": code == 0, "detail": (text or "ok")[:500]}


@router.post("/api/system/vpn", dependencies=[Depends(require_auth)])
async def api_vpn_control(body: VpnBody):
    """连接/断开 Windows 内置 VPN 配置（普通用户操作，无需管理员）。"""
    if body.action not in ("connect", "disconnect"):
        raise HTTPException(status_code=400, detail="action 须为 connect/disconnect")
    return vpn_action(body.name, body.action == "connect")


@router.post("/api/system/gateway/stop", dependencies=[Depends(require_auth)])
async def api_gateway_stop():
    """停止网关服务本身（本服务卡片的「停止服务」按钮）。

    按 PID 用 taskkill /T 杀自己这棵进程树：网关 + 所有 ttyd Agent 会话
    + gotify 推送服务。后台延迟 1 秒执行，让响应先返回给网页。
    """
    pid = os.getpid()

    def _kill():
        time.sleep(1)
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            os._exit(0)

    threading.Thread(target=_kill, daemon=True).start()
    return {
        "ok": True,
        "pid": pid,
        "note": "网关 1 秒后停止（含 Agent 会话与 gotify），重新运行 start.bat 启动",
    }


@router.post("/api/system/gateway/restart", dependencies=[Depends(require_auth)])
async def api_gateway_restart():
    """重启网关服务（含所有 Agent 会话 / gotify）。

    流程：
    1. spawn 独立 restart_helper.py（DETACHED，不在被杀树内）——等旧网关
       释放端口后自动拉起新网关。
    2. 后台线程：杀各 ttyd 会话 + gotify → os._exit 只杀自己（不动 helper）。
    响应先返回，约 5 秒后服务恢复。
    """
    import sys as _sys

    from gateway.core.gotify_proc import get_gotify_manager

    # 读监听端口（默认 8080）
    port = 8080
    env_path = Path(__file__).parent.parent.parent / ".env"
    if env_path.exists():
        try:
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("PORT="):
                    try:
                        port = int(line.split("=", 1)[1].strip())
                    except ValueError:
                        pass
                    break
        except OSError:
            pass

    helper = Path(__file__).parent.parent / "core" / "restart_helper.py"
    _root = Path(__file__).parent.parent.parent
    try:
        subprocess.Popen(
            [_sys.executable, str(helper), str(port)],
            cwd=str(_root),
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0),
            close_fds=True,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"重启调度失败: {e}")

    def _do_restart():
        time.sleep(2)  # 让响应先回到浏览器
        # 杀 ttyd 会话（每个按 PID 树杀，不动 helper）
        for s in sm.list_sessions():
            if s.pid:
                try:
                    subprocess.run(
                        ["taskkill", "/PID", str(s.pid), "/T", "/F"],
                        capture_output=True,
                        timeout=8,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                except Exception:
                    pass
        # 杀 gotify
        gp = get_gotify_manager().get_pid()
        if gp:
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(gp), "/T", "/F"],
                    capture_output=True,
                    timeout=8,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except Exception:
                pass
        # 只杀自己（os._exit 不杀子进程 → restart_helper 存活继续拉起新网关）
        os._exit(0)

    threading.Thread(target=_do_restart, daemon=True).start()
    return {"ok": True, "note": "网关重启中，约 5 秒后恢复（含 Agent 会话与 gotify）"}


# ---- WebSocket 实时终端（经 gateway 中继，浏览器不直连 ttyd） ----


@router.websocket("/api/ws/session/{session_id}")
async def ws_session(ws: WebSocket, session_id: str):
    """Session 终端的网页通道。

    浏览器 → gateway 中继 → ttyd → agent。gateway 负责：
    重放最近输出（新客户端直接看到当前状态）、广播实时输出给多客户端、
    转发输入/resize。多端因此看到同一会话，断线重连后仍是同一会话。
    """
    if ws.cookies.get(_COOKIE_NAME) != _TOKEN:
        await ws.close(code=1008)
        return

    session = sm.get_session(session_id)
    if session is None or not session.port:
        await ws.close(code=1008, reason="会话不存在")
        return

    relay = relay_mgr.get_or_create(session_id, session.port)
    if not relay.running:
        await relay.start()

    # 会话已退出（agent exit / ttyd 结束）时仍可只读查看缓冲输出
    live = relay.running and session.running
    if not live and not relay.exited:
        await ws.close(code=1011, reason="终端不可用")
        return

    await ws.accept()

    # 重放最近输出，新客户端直接从当前状态看起
    replay = relay.recent()
    if replay:
        await ws.send_json({"type": "data", "data": replay})

    if not live:
        # 只读模式：没有实时输出，重放完即标记退出
        await ws.send_json({"type": "exit", "code": relay.exit_code, "readonly": True})
        try:
            await ws.close()
        except Exception:
            pass
        return

    async def on_output(text):
        if text is None:
            await ws.send_json({"type": "exit", "code": relay.exit_code})
        else:
            await ws.send_json({"type": "data", "data": text})

    relay.subscribe(on_output)
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            m = msg.get("type")
            if m == "input":
                await relay.send_input(str(msg.get("data", "")).encode("utf-8"))
            elif m == "resize":
                await relay.send_resize(
                    int(msg.get("cols") or 80), int(msg.get("rows") or 24))
            elif m == "hello":
                # 终端页上报设备类型：判断"你在不在看"只看手机
                relay.set_device(on_output, str(msg.get("device", "desktop")))
            elif m == "visibility":
                # 终端页上报前台/后台（切后台/关页面 → 通知判定依据）
                relay.set_visibility(on_output, bool(msg.get("visible", True)))
    except Exception:
        pass
    finally:
        relay.unsubscribe(on_output)


# ---- 文件浏览与预览 ----


@router.get("/api/agents/{key}/files", dependencies=[Depends(require_auth)])
async def api_agent_files(key: str, path: str = "", project: str = ""):
    agent = registry.get_agent(key)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent 不存在: {key}")

    root, full, roots = _resolve_file(agent, project, path)

    if root is None and not path:
        # 顶层：列出所有 file_root 目录（快速入口）
        items = []
        for r in roots:
            items.append({
                "name": r.name,
                "path": r.name,
                "type": "dir",
                "size": None,
                "mtime": None,
                "exists": r.exists(),
                "abs": str(r.resolve()),
            })
        first_parent = roots[0].resolve().parent if roots else None
        return {
            "path": "",
            "abs": None,
            "parent_abs": str(first_parent) if first_parent else None,
            "roots": True,
            "items": items,
        }

    if not full.exists():
        raise HTTPException(status_code=404, detail="路径不存在")
    if not full.is_dir():
        raise HTTPException(status_code=400, detail="不是目录")

    abs_mode = root is None
    items = []
    entries = sorted(full.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
    for entry in entries[:500]:
        if entry.name.startswith("."):
            continue
        is_dir = entry.is_dir()
        size = None
        if not is_dir:
            try:
                size = entry.stat().st_size
            except OSError:
                size = None
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            mtime = None
        item_path = (
            _abs_item_path(full, entry.name)
            if abs_mode
            else _item_path(root.name, root, full, entry.name)
        )
        items.append({
            "name": entry.name,
            "path": item_path,
            "type": "dir" if is_dir else "file",
            "size": size,
            "mtime": mtime,
            "exists": True,
        })

    current_abs = str(full.resolve())
    parent_abs = str(full.parent.resolve()) if full.parent != full else None
    return {
        "path": path,
        "abs": current_abs,
        "parent_abs": parent_abs,
        "roots": False,
        "items": items,
    }


def _parent_path(path: str) -> Optional[str]:
    if not path:
        return None
    parts = path.split("/")
    if len(parts) <= 1:
        return ""
    return "/".join(parts[:-1])


_TEXT_SUFFIXES = {
    ".txt", ".csv", ".log", ".md", ".json", ".yaml", ".yml",
    ".toml", ".py", ".cfg", ".ini", ".xml", ".html", ".js",
}


@router.get("/api/file", dependencies=[Depends(require_auth)])
async def api_file(agent: str = "", path: str = "", project: str = "", dl: int = 0):
    agent_info = registry.get_agent(agent)
    if not agent_info:
        raise HTTPException(status_code=404, detail=f"Agent 不存在: {agent}")
    if not path:
        raise HTTPException(status_code=400, detail="参数错误")

    root, full, _roots = _resolve_file(agent_info, project, path)
    if not full.is_file():
        raise HTTPException(status_code=400, detail="不是文件")

    mime, _ = mimetypes.guess_type(full.name)
    suffix = full.suffix.lower()

    # 强制下载 / 无法识别 MIME → 附件
    if dl or not mime:
        return FileResponse(full, filename=full.name)

    # 图片内联
    if mime.startswith("image/"):
        return FileResponse(full, media_type=mime)

    # 文本类内联渲染
    if mime.startswith("text/") or suffix in _TEXT_SUFFIXES or mime in (
        "application/json", "application/x-yaml", "application/xml",
    ):
        try:
            content = full.read_text(encoding="utf-8", errors="replace")
        except OSError:
            content = f"(读取失败: {full.name})"
        return Response(content, media_type="text/plain; charset=utf-8")

    # 其他（pdf/word/压缩包等）→ 附件下载
    return FileResponse(full, filename=full.name)


@router.post("/api/files/upload", dependencies=[Depends(require_auth)])
async def api_upload(
    agent: str = Form(""),
    project: str = Form(""),
    path: str = Form(""),
    file: UploadFile = File(...),
):
    """上传文件到指定目录（手机→电脑的数据传输）。目标必须是 file_root 内。"""
    agent_info = registry.get_agent(agent)
    if not agent_info:
        raise HTTPException(status_code=404, detail=f"Agent 不存在: {agent}")
    if not path:
        raise HTTPException(status_code=400, detail="未指定上传目录")

    root, full, _roots = _resolve_file(agent_info, project, path)
    if not full.is_dir():
        raise HTTPException(status_code=400, detail="目标不是目录")

    # 去掉可能的路径前缀，只保留文件名
    name = os.path.basename((file.filename or "").replace("\\", "/"))
    if not name or name in (".", ".."):
        raise HTTPException(status_code=400, detail="非法文件名")
    dest = (full / name).resolve()
    if root is not None and not _within(root, dest):
        raise HTTPException(status_code=403, detail="路径越界")

    size = 0
    try:
        with dest.open("wb") as f:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                f.write(chunk)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"写入失败: {e}")
    result_path = (
        _abs_item_path(dest.parent, name)
        if root is None
        else _item_path(root.name, root, dest.parent, name)
    )
    return {
        "ok": True,
        "name": name,
        "size": size,
        "path": result_path,
    }
