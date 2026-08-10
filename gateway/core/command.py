"""命令解析模块。

定义所有 Gateway 命令枚举，并解析原始文本消息为 (Command, 参数)。
"""

from enum import Enum, auto
from typing import Tuple


class Command(Enum):
    HELP = auto()
    STATUS = auto()
    AGENTS = auto()
    SESSIONS = auto()
    START = auto()
    STOP = auto()
    ATTACH = auto()
    DETACH = auto()
    SHELL = auto()
    UNKNOWN = auto()


# 命令关键字映射：关键字 -> Command
_KEYWORD_MAP: dict[str, Command] = {
    "help": Command.HELP,
    "status": Command.STATUS,
    "agents": Command.AGENTS,
    "sessions": Command.SESSIONS,
    "start": Command.START,
    "stop": Command.STOP,
    "attach": Command.ATTACH,
    "detach": Command.DETACH,
    "shell": Command.SHELL,
}

# 即使在 attach/shell 模式下也始终由 Gateway 处理的命令
_ALWAYS_GATEWAY: set[Command] = {Command.DETACH, Command.HELP}


def parse(raw_text: str) -> Tuple[Command, str]:
    """解析原始文本消息，返回 (命令, 参数)。

    Args:
        raw_text: 用户输入的原始文本

    Returns:
        (Command 枚举值, 参数字符串)
    """
    text = raw_text.strip()
    if not text:
        return Command.UNKNOWN, ""

    parts = text.split(maxsplit=1)
    keyword = parts[0].lower()
    args = parts[1] if len(parts) > 1 else ""

    command = _KEYWORD_MAP.get(keyword, Command.UNKNOWN)
    return command, args


def is_always_gateway(command: Command) -> bool:
    """判断该命令是否始终由 Gateway 处理（即使在 attach/shell 模式下）。"""
    return command in _ALWAYS_GATEWAY


def get_help_text() -> str:
    """返回 help 信息文本。"""
    return (
        "Agent Gateway v0.1\n"
        "════════════════\n"
        "可用命令:\n"
        "  help       - 显示此帮助\n"
        "  status     - 系统状态\n"
        "  agents     - 列出所有 Agent\n"
        "  sessions   - 列出所有会话\n"
        "  start <agent>  - 创建新会话\n"
        "  stop <id>  - 关闭会话\n"
        "  attach <id>- 进入会话\n"
        "  detach     - 退出当前会话\n"
        "  shell      - 进入本地 Shell"
    )
