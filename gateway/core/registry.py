"""Agent 注册表模块。

从 agents.json 加载所有已注册的 Agent，提供查询接口。
"""

import json
from pathlib import Path
from typing import Optional

# agents.json 路径：项目根目录
_REGISTRY_PATH = Path(__file__).parent.parent.parent / "agents.json"


class AgentInfo:
    """单个 Agent 的注册信息。

    除基础展示字段外，还包含启动配置（cmd/env/root/venv 等），
    由 AgentProcessManager 使用。注意 to_dict() 绝不输出 cmd/env，
    避免泄露 API key 等敏感信息。
    """

    def __init__(self, key: str, data: dict):
        self.key = key
        self.name: str = data.get("name", key)
        self.endpoint: str = data.get("endpoint", "")
        self.status: str = data.get("status", "unknown")
        self.type: str = data.get("type", "")
        self.root: str = data.get("root", "")
        self.venv: str = data.get("venv", "")
        self.cwd: str = data.get("cwd", "")
        self.cmd: list = data.get("cmd", [])
        self.env: dict = data.get("env", {})
        self.load_env_from: str = data.get("load_env_from", "")
        self.projects: list = data.get("projects", [])
        self.default_project: str = data.get("default_project", "")
        self.file_roots: list = data.get("file_roots", [])
        self.note: str = data.get("note", "")
        self.hidden: bool = bool(data.get("hidden", False))  # 不在 agent 列表展示（如 shell）

    @property
    def is_online(self) -> bool:
        return self.status == "online"

    def to_dict(self) -> dict:
        """返回 JSON 友好的字典表示。绝不包含 cmd/env/load_env_from。"""
        return {
            "key": self.key,
            "name": self.name,
            "endpoint": self.endpoint,
            "status": self.status,
            "is_online": self.is_online,
            "type": self.type,
            "projects": self.projects,
            "default_project": self.default_project,
            "file_roots": self.file_roots,
            "note": self.note,
            "hidden": self.hidden,
        }


class AgentRegistry:
    """Agent 注册表，启动时从 agents.json 加载。"""

    def __init__(self):
        self._agents: dict[str, AgentInfo] = {}
        self._load()

    def _load(self):
        """从 agents.json 加载所有 Agent。"""
        if not _REGISTRY_PATH.exists():
            return
        with open(_REGISTRY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        for key, info in data.items():
            self._agents[key] = AgentInfo(key, info)

    def reload(self):
        """重新加载 agents.json。"""
        self._agents.clear()
        self._load()

    def list_agents(self) -> list[AgentInfo]:
        """返回所有注册 Agent 列表。"""
        return list(self._agents.values())

    def get_agent(self, name: str) -> Optional[AgentInfo]:
        """通过名称获取 Agent 信息。"""
        return self._agents.get(name)

    def format_agent_list(self) -> str:
        """返回格式化的 Agent 列表文本。"""
        agents = self.list_agents()
        if not agents:
            return "Available Agents:\n\n  (无已注册 Agent)"

        lines = ["Available Agents:", ""]
        for agent in agents:
            icon = "[ON]" if agent.is_online else "[OFF]"
            lines.append(f"  {agent.key:<15} {icon} {agent.status}")
        return "\n".join(lines)

    def is_agent_online(self, name: str) -> bool:
        """检查指定 Agent 是否在线。"""
        agent = self.get_agent(name)
        return agent is not None and agent.is_online


# 全局单例
_registry: Optional[AgentRegistry] = None


def get_agent_registry() -> AgentRegistry:
    """获取全局 AgentRegistry 单例。"""
    global _registry
    if _registry is None:
        _registry = AgentRegistry()
    return _registry
