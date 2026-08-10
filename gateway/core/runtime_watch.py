"""运行时状态轮询 + 推送 + 启动 rediscovery。

后台 daemon 线程每 ~1s：
1. 轮询每个带 runtime_file 的 Session，读 agent 上报的 JSON，更新
   Session 的 runtime_status/waiting_prompt/progress/suggestions 等。
2. 状态「进入 waiting_input / 变为 exited」时触发一次通知（去重，避免
   长时间等待反复轰炸）。agent 重新进入 waiting_input 会再通知。
3. 首次运行时做 rediscovery：扫描各注册 agent 的 <root>/.runtime/
   runtime-*.json，pid 存活（psutil.pid_exists）的生成只读"外部运行中"
   会话项——gateway 重启后凭文件重新看到还在跑的 agent。

外部会话（gateway 重启恢复的）没有 PTY，无法注入输入/看终端，但状态
照常显示与推送。这是 v1 的已知限制（完整控制要命令收件箱，见计划 V2）。

线程懒启动（仿 get_session_manager 单例模式），web.py 导入时调
get_runtime_watcher() 确保它跑起来。
"""

import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import psutil

from gotif import alert, notify
from gateway.core import notifier
from gateway.core.registry import get_agent_registry
from gateway.core.session import get_session_manager

# agent 上报的合法状态
_VALID_RUNTIME_STATUS = {"starting", "ready", "working", "waiting_input", "exited"}
# 需要推送通知的状态
_NOTIFY_STATUS = {"waiting_input", "exited"}

_POLL_INTERVAL = 1.0


def _read_env(key: str, default: str = "") -> str:
    """读项目根 .env（os.environ 优先）。"""
    val = os.getenv(key)
    if val:
        return val
    env_path = Path(__file__).resolve().parent.parent.parent / ".env"
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                if k.strip() == key:
                    return v.strip()
    except OSError:
        pass
    return default


_PUBLIC_BASE = _read_env("GATEWAY_PUBLIC_URL", "http://100.104.123.123:8080").rstrip("/")


class RuntimeWatcher:
    def __init__(self):
        self._session_mgr = get_session_manager()
        self._registry = get_agent_registry()
        self._notified: dict[str, str] = {}  # session_id -> 已通知的状态
        self._rediscovered: set[str] = set()  # 已恢复的 runtime 文件路径
        self._stop = threading.Event()

    # ---- 对外 ----

    def start(self):
        if not getattr(self, "_thread", None) or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()

    # ---- 主循环 ----

    def _loop(self):
        first = True
        while not self._stop.is_set():
            try:
                if first:
                    self._rediscover_external()
                    first = False
                self._poll()
            except Exception:
                pass
            self._stop.wait(_POLL_INTERVAL)

    # ---- 轮询活跃会话 ----

    def _poll(self):
        sm = self._session_mgr
        for session in list(sm.list_sessions()):
            if not session.runtime_file:
                continue
            state = self._read_runtime_file(session.runtime_file)
            if not state:
                continue
            with sm.lock:
                self._apply_state(session, state)

    def _apply_state(self, session, state: dict):
        """把 agent 上报的 state 落到 Session，并触发去重通知。"""
        status = state.get("status", "")
        if status not in _VALID_RUNTIME_STATUS:
            status = ""
        session.runtime_status = status
        session.waiting_prompt = state.get("waiting_prompt") or ""
        session.progress = state.get("progress") or ""
        session.runtime_updated = state.get("updated_at") or ""
        session.suggestions = state.get("suggestions") or []
        # 通知（去重：只在该状态首次出现时推一次；重新进入会再推）
        if status in _NOTIFY_STATUS and self._notified.get(session.id) != status:
            self._notified[session.id] = status
            self._notify(session, status)

    def _notify(self, session, status: str):
        title = ""
        content = ""
        if status == "waiting_input":
            title = f"[Agent 需要你输入] {session.name}"
            content = session.waiting_prompt or "正在等你回复"
            if session.suggestions:
                content += "\n建议: " + " / ".join(session.suggestions)
        elif status == "exited":
            title = f"[Agent 已完成] {session.name}"
            content = f"进程退出 code={session.exit_code}"

        # gotif 直达链接：点通知直接打开该任务的手机终端页
        url = ""
        if session.port:
            url = f"{_PUBLIC_BASE}/term?session={session.id}"
        try:
            if status == "waiting_input":
                alert(title, content, url=url)   # priority 8：铃声+震动+悬浮窗
            else:
                notify(title, content, url=url)  # priority 5
        except Exception:
            pass
        # 旧 notifier 保留（模板为空时 no-op）
        notifier.send(title, content)

    # ---- runtime 文件读取 ----

    @staticmethod
    def _read_runtime_file(path: str) -> Optional[dict]:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None

    # ---- 启动 rediscovery：找回 gateway 重启前还在跑的 agent ----

    def _rediscover_external(self):
        sm = self._session_mgr
        known_files = {
            s.runtime_file for s in sm.list_sessions() if s.runtime_file
        }
        for agent in self._registry.list_agents():
            if not agent.root:
                continue
            runtime_dir = Path(agent.root) / ".runtime"
            if not runtime_dir.is_dir():
                continue
            for f in sorted(runtime_dir.glob("runtime-*.json")):
                if str(f) in known_files or str(f) in self._rediscovered:
                    continue
                self._rediscovered.add(str(f))
                state = self._read_runtime_file(str(f))
                if not state:
                    continue
                pid = state.get("pid")
                if not pid or not psutil.pid_exists(pid):
                    continue
                self._register_external(agent.key, agent.name, f, state)

    def _register_external(self, agent_key: str, name: str, path: Path, state: dict):
        """为 rediscovery 到的存活 agent 生成只读会话项。"""
        from gateway.core.session import Session

        session_id = path.stem[len("runtime-"):]
        # 避免重复（万一并发）
        if session_id in {s.id for s in self._session_mgr.list_sessions()}:
            return
        session = Session(
            id=session_id,
            name=name,
            agent=name,
            user_id="web",
            created_time=datetime.now(),
            status="running",
            agent_key=agent_key,
            project=state.get("project") or None,
            pid=state.get("pid"),
            running=True,
            runtime_file=str(path),
            external=True,
        )
        self._apply_state(session, state)
        with self._session_mgr.lock:
            self._session_mgr._sessions[session_id] = session


# 全局单例
_watcher: Optional[RuntimeWatcher] = None


def get_runtime_watcher() -> RuntimeWatcher:
    """获取全局 RuntimeWatcher 单例并确保线程在跑。"""
    global _watcher
    if _watcher is None:
        _watcher = RuntimeWatcher()
        _watcher.start()
    return _watcher
