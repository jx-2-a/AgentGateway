"""系统状态采集模块。"""

import psutil
from datetime import datetime

# boot_time 是常量，只取一次，避免每次请求都调一次 psutil
_BOOT_TIME = datetime.fromtimestamp(psutil.boot_time()).strftime("%Y-%m-%d %H:%M")


def collect_stats() -> dict:
    """采集原始系统统计数据（非阻塞）。

    注意 cpu_percent 用 interval=None（默认）：立即返回自上次调用以来的
    使用率，不会像 interval=1 那样每次请求都阻塞 1 秒——前端哪怕 1 秒一刷
    也不会卡住服务。首次调用返回 0.0，第二次起就是真实值。

    Returns:
        {
            "cpu_percent": float,
            "memory_used_gb": float,
            "memory_total_gb": float,
            "boot_time": "YYYY-MM-DD HH:MM",
        }
    """
    memory = psutil.virtual_memory()
    return {
        "cpu_percent": psutil.cpu_percent(),
        "memory_used_gb": round(memory.used / (1024**3), 1),
        "memory_total_gb": round(memory.total / (1024**3), 1),
        "boot_time": _BOOT_TIME,
    }


def get_system_status(session_count: int = 0, agent_count: int = 0) -> str:
    """采集系统状态并返回格式化的状态字符串。

    Args:
        session_count: 当前活跃 Session 数量
        agent_count: 已注册 Agent 数量

    Returns:
        格式化的多行状态文本
    """
    stats = collect_stats()
    lines = [
        "════════════════",
        "  Gateway: online",
        "════════════════",
        f"  CPU:        {stats['cpu_percent']}%",
        f"  Memory:     {stats['memory_used_gb']}/{stats['memory_total_gb']}GB",
        f"  Agents:     {agent_count} registered",
        f"  Sessions:   {session_count}",
        f"  Uptime:     {stats['boot_time']}",
        "════════════════",
    ]
    return "\n".join(lines)
