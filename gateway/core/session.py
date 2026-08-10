"""会话管理模块。

Session 是 Web 和 Agent 之间的核心抽象层：
    入口 → Session → Agent 子进程

同一个 Agent 可以有多个 Session，每个 Session 独立拉取一个真实子进程
并维护上下文。
"""

import asyncio
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from gateway.core.registry import get_agent_registry
from gateway.core.ttyd_proc import get_ttyd_process_manager
from gateway.core.ttyd_relay import get_ttyd_relay_manager

# Session 状态
_STATUS_STARTING = "starting"
_STATUS_RUNNING = "running"
_STATUS_STOPPED = "stopped"
_STATUS_EXITED = "exited"
_STATUS_ERROR = "error"


@dataclass
class Session:
    """一个 Agent 会话实例。"""

    id: str
    name: str
    agent: str
    user_id: str
    created_time: datetime
    status: str  # starting | running | stopped | exited | error
    context: dict = field(default_factory=dict)
    agent_key: str = ""
    project: Optional[str] = None
    pid: Optional[int] = None      # ttyd 进程 PID
    port: Optional[int] = None     # ttyd 终端端口（9000-9099），前端拼 ws://host:port
    running: bool = False
    exit_code: Optional[int] = None
    # ---- 运行时状态（agent 通过 runtime 文件上报，runtime_watch 轮询更新） ----
    runtime_status: str = ""       # "" | starting|ready|working|waiting_input|exited
    waiting_prompt: str = ""
    suggestions: list = field(default_factory=list)
    progress: str = ""
    runtime_updated: str = ""      # agent 最后上报的 updated_at
    runtime_file: str = ""         # agent 的运行时状态 JSON 文件路径
    external: bool = False         # True = gateway 重启后从 runtime 文件恢复的"外部运行中"

    def to_dict(self) -> dict:
        """返回 JSON 友好的字典表示。"""
        return {
            "id": self.id,
            "name": self.name,
            "agent": self.agent,
            "agent_key": self.agent_key,
            "project": self.project,
            "user_id": self.user_id,
            "created_time": self.created_time.strftime("%Y-%m-%d %H:%M:%S"),
            "status": self.status,
            "pid": self.pid,
            "port": self.port,
            "running": self.running,
            "exit_code": self.exit_code,
            "error": self.context.get("error"),
            "runtime_status": self.runtime_status,
            "waiting_prompt": self.waiting_prompt,
            "suggestions": self.suggestions,
            "progress": self.progress,
            "runtime_updated": self.runtime_updated,
            "external": self.external,
        }

    def summary(self, index: int) -> str:
        """返回单条 session 摘要，用于 sessions 列表展示。"""
        now = datetime.now()
        delta = now - self.created_time
        minutes_ago = int(delta.total_seconds() // 60)
        ago_str = f"{minutes_ago} min ago" if minutes_ago > 0 else "just now"

        proc_str = f"pid: {self.pid}" if self.pid else "pid: -"
        return (
            f"{index}.\n"
            f"  id:       {self.id}\n"
            f"  name:     {self.name}\n"
            f"  agent:    {self.agent}\n"
            f"  status:   {self.status}  ({proc_str})\n"
            f"  created:  {ago_str}\n"
        )


class SessionManager:
    """管理所有 Agent Session 的生命周期和用户绑定状态。"""

    def __init__(self):
        self._sessions: dict[str, Session] = {}
        # 用户绑定：{user_id: session_id}
        self._attachments: dict[str, str] = {}
        # ttyd 终端引擎（替代 WinPTY：每 session 一个端口 = 一个终端 URL）
        self._ttyd_mgr = get_ttyd_process_manager()
        # 全局自增编号：session id 用纯数字（用户要求，多开可分得清）
        self._session_seq = 0
        # poller 线程（runtime_watch）与 web 请求并发访问会话表的互斥锁
        self.lock = threading.Lock()

    # ---- Session CRUD ----

    def create_session(
        self,
        name: str,
        agent: str,
        user_id: str,
        agent_key: str = "",
        project: Optional[str] = None,
    ) -> Session:
        """创建一个新 Session，并真实拉取 Agent 子进程。

        Args:
            name: Session 显示名（通常为 Agent 显示名）
            agent: Agent 显示名
            user_id: 创建者
            agent_key: Agent 注册 key（agents.json 的 key）
            project: 项目名（WAL 等需要）
        """
        agent_key = agent_key or agent
        with self.lock:
            self._session_seq += 1
            session_id = str(self._session_seq)

        # 运行时状态文件：放 agent 自己的目录下（<root>/.runtime/runtime-<id>.json）
        runtime_file = self._runtime_file_path(agent_key, session_id)

        session = Session(
            id=session_id,
            name=name,
            agent=agent,
            user_id=user_id,
            created_time=datetime.now(),
            status=_STATUS_STARTING,
            agent_key=agent_key,
            project=project,
            runtime_file=runtime_file,
        )
        with self.lock:
            self._sessions[session_id] = session

        try:
            # ttyd 引擎：派生 ttyd → ttyd 再通过 ConPTY 派生 agent 子进程。
            # 终端输出由 ttyd 自己服务，gateway 不再读 PTY 缓冲。
            tp = self._ttyd_mgr.start(agent_key, project, runtime_file=runtime_file)
            session.pid = tp.pid
            session.port = tp.port
            session.running = True
            session.status = _STATUS_RUNNING
            # 常驻中继：保持 ttyd 会话存活（agent 后台常驻）+ 网页端访问入口。
            # ttyd 每客户端独立会话，浏览器不能直连，必须经中继。
            relay = get_ttyd_relay_manager().get_or_create(session_id, tp.port)
            try:
                asyncio.get_running_loop().create_task(relay.start())
            except RuntimeError:
                pass  # 无事件循环上下文（非 web 调用）时跳过，浏览器连时再补
        except ValueError as e:
            session.status = _STATUS_ERROR
            session.context["error"] = str(e)
        return session

    @staticmethod
    def _runtime_file_path(agent_key: str, session_id: str) -> str:
        """计算 agent 运行时状态文件的绝对路径。"""
        try:
            agent = get_agent_registry().get_agent(agent_key)
            root = agent.root if agent and agent.root else ""
        except Exception:
            root = ""
        if root:
            return str(Path(root) / ".runtime" / f"runtime-{session_id}.json")
        return ""

    def destroy_session(self, session_id: str) -> bool:
        """删除指定 Session（先停止其进程）。返回是否删除成功。"""
        session = self._sessions.get(session_id)
        if session is None:
            return False
        if session.pid:
            self._ttyd_mgr.stop(session.pid)
        get_ttyd_relay_manager().drop(session_id)
        del self._sessions[session_id]
        # 同时清理绑定到此 session 的用户
        for user_id, sid in list(self._attachments.items()):
            if sid == session_id:
                del self._attachments[user_id]
        return True

    def stop_session(self, session_id: str) -> bool:
        """停止 Session 的 ttyd 进程树（含 agent 子进程），但保留 Session。"""
        session = self._sessions.get(session_id)
        if session is None:
            return False
        if session.pid:
            self._ttyd_mgr.stop(session.pid)
        get_ttyd_relay_manager().drop(session_id)
        session.running = False
        session.status = _STATUS_STOPPED
        return True

    def sync_process_status(self, session_id: str):
        """把 ttyd 的实际退出状态同步到 Session（agent crash 时调用）。

        ttyd 在 agent 子进程退出后会自行退出，因此 ttyd 存活 ≈ agent 存活。
        外部会话（gateway 重启恢复的）没有 ttyd 句柄，由 runtime_watch 管，跳过。
        """
        session = self._sessions.get(session_id)
        if session is None or not session.pid or session.external:
            return
        if session.status in (_STATUS_STOPPED, _STATUS_ERROR):
            return
        if not self._ttyd_mgr.is_alive(session.pid):
            if session.running or session.status == _STATUS_RUNNING:
                tp = self._ttyd_mgr.get(session.pid)
                session.running = False
                session.status = _STATUS_EXITED
                session.exit_code = tp.exit_code if tp else None

    def get_session(self, session_id: str) -> Optional[Session]:
        """获取单个 Session。"""
        return self._sessions.get(session_id)

    def list_sessions(self) -> list[Session]:
        """返回所有 Session 列表，按创建时间倒序。"""
        return sorted(
            self._sessions.values(),
            key=lambda s: s.created_time,
            reverse=True,
        )

    def count(self) -> int:
        """返回 Session 总数。"""
        return len(self._sessions)

    def format_session_list(self) -> str:
        """返回格式化的 session 列表文本。"""
        sessions = self.list_sessions()
        if not sessions:
            return "Session List:\n\n  (无活跃会话)"

        lines = ["Session List:", ""]
        for i, session in enumerate(sessions, 1):
            lines.append(session.summary(i))
        return "\n".join(lines)

    # ---- 用户绑定 (attach/detach) ----

    def attach(self, user_id: str, session_id: str) -> bool:
        """将用户绑定到指定 Session。"""
        if session_id not in self._sessions:
            return False
        self._attachments[user_id] = session_id
        return True

    def detach(self, user_id: str) -> bool:
        """解除用户的 Session 绑定。"""
        if user_id in self._attachments:
            del self._attachments[user_id]
            return True
        return False

    def get_attachment(self, user_id: str) -> Optional[str]:
        """查询用户当前绑定的 Session ID，未绑定返回 None。"""
        return self._attachments.get(user_id)


# 全局单例
_session_manager: Optional[SessionManager] = None


def get_session_manager() -> SessionManager:
    """获取全局 SessionManager 单例。"""
    global _session_manager
    if _session_manager is None:
        _session_manager = SessionManager()
    return _session_manager
