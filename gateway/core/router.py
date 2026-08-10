"""共享消息路由器。

所有终端适配器（目前只有网页控制台）共用同一个 process_message()。

用户状态机:
    [Gateway]  <->  [Attached to Session]  <->  [Shell Mode]

处理顺序:
    1. always-gateway 命令 (detach/help) — 任何模式下都由 Gateway 处理
    2. Shell 模式 — 消息作为 shell 命令执行
    3. Attach 模式 — 消息转发给 Agent
    4. 普通 Gateway 命令分发
    5. UNKNOWN — 返回 None (静默)
"""

from typing import Optional

from gateway.core.command import (
    Command,
    parse,
    is_always_gateway,
    get_help_text,
)
from gateway.core.runtime import get_system_status
from gateway.core.session import get_session_manager
from gateway.core.registry import get_agent_registry
from gateway.core.shell import get_shell_manager
from gateway.core.agent_proc import get_agent_process_manager


# 全局单例
sm = get_session_manager()
registry = get_agent_registry()
shell_mgr = get_shell_manager()
proc_mgr = get_agent_process_manager()

# 当前处于 shell 模式的 user_id 集合
_shell_users: set[str] = set()


# ============================================================================
# 命令分发
# ============================================================================


def _dispatch(command: Command, user_id: str, args: str) -> str:
    """按命令枚举分发到对应 handler，返回回复文本。"""
    if command == Command.HELP:
        return get_help_text()

    if command == Command.STATUS:
        return get_system_status(
            session_count=sm.count(),
            agent_count=sum(1 for a in registry.list_agents() if not a.hidden),
        )

    if command == Command.AGENTS:
        return registry.format_agent_list()

    if command == Command.SESSIONS:
        return sm.format_session_list()

    if command == Command.START:
        return _handle_start(user_id, args)

    if command == Command.STOP:
        return _handle_stop(args)

    if command == Command.ATTACH:
        return _handle_attach(user_id, args)

    if command == Command.DETACH:
        return _handle_detach(user_id)

    if command == Command.SHELL:
        return _handle_shell(user_id)

    return "[Gateway] 未知命令"


# ============================================================================
# 各命令 handler
# ============================================================================


def _handle_start(user_id: str, args: str) -> str:
    parts = args.split(None, 1) if args else []
    agent_name = parts[0] if parts else ""
    project = parts[1] if len(parts) > 1 else None

    if not agent_name:
        return (
            "[Gateway] 用法: start <agent_name> [project]\n"
            "用 agents 查看可用的 Agent。"
        )

    agent = registry.get_agent(agent_name)
    if not agent:
        return (
            f"[Gateway] Agent 不存在: {agent_name}\n"
            "用 agents 查看可用的 Agent。"
        )

    if not agent.is_online:
        return (
            f"[Gateway] Agent '{agent.name}' 当前离线。\n"
            f"Endpoint: {agent.endpoint}"
        )

    if not project and agent.default_project:
        project = agent.default_project

    session = sm.create_session(
        name=agent.name,
        agent=agent.name,
        user_id=user_id,
        agent_key=agent_name,
        project=project,
    )

    if session.status == "error":
        return (
            f"[Gateway] Agent 启动失败: {session.agent}\n"
            f"原因: {session.context.get('error', '未知错误')}"
        )

    return (
        f"启动: {session.agent}\n"
        f"════════════════\n"
        f"  Session:  {session.id}\n"
        f"  Agent:    {session.agent}"
        + (f"\n  Project:  {session.project}" if session.project else "")
        + f"\n  PID:      {session.pid}"
        + f"\n  Status:   {session.status}"
        + "\n"
        f"════════════════\n"
        f"用 attach {session.id} 进入会话。"
    )


def _handle_stop(session_id: str) -> str:
    if not session_id:
        return (
            "[Gateway] 用法: stop <session_id>\n"
            "用 sessions 查看活跃会话。"
        )

    if sm.destroy_session(session_id):
        return f"[Gateway] Session 已关闭: {session_id}"

    return (
        f"[Gateway] Session 不存在: {session_id}\n"
        "用 sessions 查看活跃会话。"
    )


def _handle_attach(user_id: str, session_id: str) -> str:
    if not session_id:
        return (
            "[Gateway] 用法: attach <session_id>\n"
            "先用 sessions 查看可用会话。"
        )

    # 特殊：attach shell → 进入 shell 模式
    if session_id.lower() == "shell":
        _shell_users.add(user_id)
        sm.detach(user_id)
        return (
            "════════════════\n"
            "  Shell Mode\n"
            "════════════════\n"
            "已进入本地 Shell。\n"
            "输入 shell 命令执行。\n"
            "输入 detach 退出。"
        )

    if not sm.attach(user_id, session_id):
        return (
            f"[Gateway] Session 不存在: {session_id}\n"
            "用 sessions 查看可用会话。"
        )

    session = sm.get_session(session_id)
    return (
        f"════════════════\n"
        f"已连接到: {session.name}\n"
        f"Agent:     {session.agent}\n"
        f"════════════════\n"
        f"现在发送的消息将直接转发给 Agent。\n"
        f"输入 detach 退出。"
    )


def _handle_detach(user_id: str) -> str:
    was_shell = user_id in _shell_users
    was_attached = sm.get_attachment(user_id) is not None

    _shell_users.discard(user_id)
    sm.detach(user_id)

    if was_shell:
        return "[Gateway] 已退出 Shell 模式，返回 Gateway。"
    if was_attached:
        return "[Gateway] 已断开 Session，返回 Gateway。"
    return "[Gateway] 当前未连接到任何 Session。"


def _handle_shell(user_id: str) -> str:
    sm.detach(user_id)
    _shell_users.add(user_id)
    return (
        "════════════════\n"
        "  Shell Mode\n"
        "════════════════\n"
        "已进入本地 Shell。\n"
        "输入 shell 命令执行。\n"
        "输入 exit 或 detach 退出。"
    )


def _forward_to_agent(user_id: str, text: str) -> str:
    """Attach 模式下把消息转发给真实 Agent 子进程的 stdin。"""
    session_id = sm.get_attachment(user_id)
    if not session_id:
        return "[Gateway] 当前未连接到任何 Session。"
    session = sm.get_session(session_id)
    if not session:
        sm.detach(user_id)
        return "[Gateway] 当前 Session 已失效，已自动 detach。"

    if session.pid and proc_mgr.send_line(session.pid, text):
        return (
            f"[{session.agent}] 消息已发送 ✓\n"
            f"(实时输出请看该 Agent 的网页终端)"
        )

    sm.sync_process_status(session_id)
    return (
        f"[{session.agent}]\n"
        f"Agent 进程当前不可用 (status={session.status})。\n"
        "用 start <agent> 重新启动。"
    )


# ============================================================================
# 对外主入口
# ============================================================================


def process_message(user_id: str, text: str) -> Optional[str]:
    """处理一条用户消息，返回回复文本；无需回复时返回 None。

    Args:
        user_id: 用户标识（网页统一为 "web"）
        text: 消息原文

    Returns:
        回复文本；当消息不是 Gateway 可处理内容时返回 None
    """
    raw = text.strip()
    if not raw:
        return None

    command, args = parse(raw)

    # 1. always-gateway 命令 (detach/help) — 任何模式下都由 Gateway 处理
    if is_always_gateway(command):
        return _dispatch(command, user_id, args)

    # 2. Shell 模式 — 消息作为 shell 命令执行
    if user_id in _shell_users:
        if raw.lower() in ("exit", "quit"):
            _shell_users.discard(user_id)
            return "[Gateway] 已退出 Shell 模式，返回 Gateway。"
        return shell_mgr.execute(raw)

    # 3. Attach 模式 — 转发给 Agent
    if sm.get_attachment(user_id):
        return _forward_to_agent(user_id, raw)

    # 4. 普通 Gateway 命令分发
    if command == Command.UNKNOWN:
        return None

    return _dispatch(command, user_id, args)


def get_user_mode(user_id: str) -> dict:
    """返回用户当前模式，供面板展示。"""
    if user_id in _shell_users:
        return {"mode": "shell"}
    session_id = sm.get_attachment(user_id)
    if session_id:
        return {"mode": "attached", "session_id": session_id}
    return {"mode": "gateway"}
