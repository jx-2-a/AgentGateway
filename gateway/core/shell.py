"""本地 Shell 管理模块。

提供受限的本地 Shell 执行能力。通过白名单/黑名单机制限制可执行命令，
输出截断和超时保护防止滥用。

注意：这是 Gateway 中权限最高的模块，需要谨慎配置。
"""

import subprocess
import shlex
from typing import Tuple


# ============================================================================
# 命令安全策略
# ============================================================================

# 白名单：前缀匹配（命令必须以这些前缀开头才允许执行）
_ALLOWED_COMMANDS: list[str] = [
    "ls",
    "dir",
    "pwd",
    "whoami",
    "hostname",
    "cat",
    "echo",
    "head",
    "tail",
    "wc",
    "find",
    "du",
    "df",
    "ps",
    "top",
    "git",
    "python --version",
    "python -V",
    "pip list",
    "pip show",
    "tree",
    "date",
    "time",
    "uptime",
    "uname",
    "tasklist",
    "systeminfo",
]

# 黑名单：完整命令或路径（即使匹配白名单前缀也拒绝）
_BLOCKED_COMMANDS: list[str] = [
    "rm",
    "del",
    "format",
    "shutdown",
    "reboot",
    "restart",
    "dd",
    "mkfs",
    "chmod",
    "chown",
    "sudo",
    "su",
    "kill",
    "taskkill",
    "reg",
    "regedit",
    "net",
    "netsh",
    "diskpart",
    "fsutil",
    "bcdedit",
    "wmic",
    "schtasks",
    "sc",
    "attrib",
    "cacls",
    "icacls",
    "takeown",
    "rundll32",
]

# 输出最大字符数（消息长度限制考虑）
_MAX_OUTPUT_CHARS = 2000

# 命令执行超时（秒）
_EXEC_TIMEOUT = 30


class ShellManager:
    """受限的本地 Shell 执行器。"""

    def is_allowed(self, command: str) -> Tuple[bool, str]:
        """检查命令是否允许执行。

        Returns:
            (是否允许, 拒绝原因)
        """
        cmd_lower = command.lower().strip()
        if not cmd_lower:
            return False, "空命令"

        # 尝试解析命令的第一个词
        try:
            parts = shlex.split(command)
        except ValueError:
            parts = command.split()
        if not parts:
            return False, "无法解析命令"

        base_cmd = parts[0].lower()

        # 检查黑名单（完整命令名匹配）
        for blocked in _BLOCKED_COMMANDS:
            if base_cmd == blocked:
                return False, f"禁止命令: {blocked}"

        # 检查是否为危险路径
        for blocked in _BLOCKED_COMMANDS:
            if command.lower().startswith(blocked):
                return False, f"禁止命令: {blocked}"

        # 检查白名单前缀
        for allowed in _ALLOWED_COMMANDS:
            if cmd_lower.startswith(allowed):
                return True, ""

        return False, f"不在白名单中: {base_cmd}"

    def execute(self, command: str) -> str:
        """执行 shell 命令并返回结果。

        Args:
            command: 要执行的 shell 命令

        Returns:
            命令输出（截断到 _MAX_OUTPUT_CHARS）
        """
        allowed, reason = self.is_allowed(command)
        if not allowed:
            return f"[Shell] {reason}"

        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=_EXEC_TIMEOUT,
                cwd=None,  # 当前工作目录
            )

            output = result.stdout
            if result.stderr:
                output += result.stderr

            if not output.strip():
                output = "(无输出)"

            # 截断过长输出
            if len(output) > _MAX_OUTPUT_CHARS:
                output = output[:_MAX_OUTPUT_CHARS] + "\n... (输出已截断)"

            if result.returncode != 0:
                output += f"\n[返回码: {result.returncode}]"

            return output

        except subprocess.TimeoutExpired:
            return f"[Shell] 命令超时 ({_EXEC_TIMEOUT}s)"
        except Exception as e:
            return f"[Shell] 执行错误: {e}"


# 全局单例
_shell_manager: ShellManager | None = None


def get_shell_manager() -> ShellManager:
    """获取全局 ShellManager 单例。"""
    global _shell_manager
    if _shell_manager is None:
        _shell_manager = ShellManager()
    return _shell_manager
