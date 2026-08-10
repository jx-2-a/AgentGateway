"""ttyd 终端进程管理模块 —— Gateway 的 web 终端引擎（替代 WinPTY）。

每个 Agent 子进程由 ttyd 拉起。ttyd 直接驱动 ConPTY（CreatePseudoConsole +
命名管道），没有 pywinpty 那层 socket 中转——治好了之前记录的乱码/丢输入。

关键行为（2026-08-10 实测确认的 flag 语义）：
1. **每 Session 一个端口（9000-9099）= 一个 URL**。手机/桌面各自拼
   http://<自己看到的host>:<port>，Tailscale 内网直连。
2. **不加 -o/-q**：ttyd 默认允许无限多客户端共享同一终端，客户端全部
   断开后子进程继续在后台跑，随时重开 URL 都能接回同一个 shell。
3. **必加 -W**：ttyd 默认只读，不加任何输入都发不进 agent。
4. 进程输出由 ttyd 自己服务并渲染，Gateway 不再读 PTY 缓冲、不再做
   WS 增量重放（那套是之前乱码/掉线的根源）。

复用了 agent_proc.py 的 _sub/_load_dotenv/_FORCE_ENV，同一份 agents.json
配置两套引擎可并存；WinPTY 旧代码 Phase 2 再删。
"""

import os
import socket
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from gateway.core.agent_proc import AgentProcessManager, _FORCE_ENV, _CREATE_NO_WINDOW
from gateway.core.registry import get_agent_registry

# 端口池
_PORT_RANGE = range(9000, 9100)

_DEFAULT_TTYD = "ttyd"


def _read_env(key: str, default: str = "") -> str:
    """读项目根 .env（简单 key=value 解析，兼容 os.environ 优先）。"""
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


def _port_free(port: int) -> bool:
    """端口是否空闲（bind 测试）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


@dataclass
class TtydProcess:
    """一个由 ttyd 拉起的 Agent 子进程句柄。"""

    pid: int
    port: int
    agent_key: str
    project: Optional[str]
    cmd: list
    cwd: str
    env: dict
    runtime_file: str
    proc: subprocess.Popen
    started_at: datetime = field(default_factory=datetime.now)
    exit_code: Optional[int] = None

    @property
    def url(self) -> str:
        """终端页 URL 的端口部分（host 由调用方拼，手机/桌面 host 不同）。"""
        return f"http://localhost:{self.port}"


class TtydProcessManager:
    """管理所有 ttyd Agent 子进程。"""

    def __init__(self):
        self._procs: dict[int, TtydProcess] = {}
        self._lock = threading.Lock()
        self._used_ports: set[int] = set()
        self._ttyd = _read_env("TTYD_PATH", _DEFAULT_TTYD)

    # ---- 端口 ----

    def _alloc_port(self) -> int:
        with self._lock:
            for port in _PORT_RANGE:
                if port in self._used_ports:
                    continue
                if _port_free(port):
                    self._used_ports.add(port)
                    return port
        raise ValueError(f"无可用端口（{_PORT_RANGE.start}-{_PORT_RANGE.stop - 1} 已用尽）")

    def _free_port(self, port: int):
        with self._lock:
            self._used_ports.discard(port)

    # ---- 生命周期 ----

    def start(
        self,
        agent_key: str,
        project: Optional[str] = None,
        runtime_file: str = "",
    ) -> TtydProcess:
        """按 agents.json 配置用 ttyd 拉起 Agent 子进程。

        runtime_file 非空时注入 AGENT_RUNTIME_FILE 环境变量（agent 通过
        它写运行时状态文件，见 gateway/core/agent_runtime.py）。
        """
        registry = get_agent_registry()
        agent = registry.get_agent(agent_key)
        if agent is None:
            raise ValueError(f"Agent 不存在: {agent_key}")
        if not agent.cmd:
            raise ValueError(f"Agent '{agent_key}' 未配置启动命令 (cmd)")

        cmd = AgentProcessManager._sub(agent, agent.cmd, project)
        cwd = AgentProcessManager._sub(agent, agent.cwd or agent.root, project)

        env = os.environ.copy()
        env.update(_FORCE_ENV)  # PYTHONUTF8 / PYTHONIOENCODING / PYTHONUNBUFFERED
        env.update(AgentProcessManager._sub(agent, agent.env or {}, project))
        if agent.load_env_from:
            dotenv_path = AgentProcessManager._sub(agent, agent.load_env_from, project)
            if os.path.isfile(dotenv_path):
                for k, v in AgentProcessManager._load_dotenv(dotenv_path).items():
                    env.setdefault(k, v)
        if runtime_file:
            env["AGENT_RUNTIME_FILE"] = runtime_file

        port = self._alloc_port()
        auth = _read_env("TTYD_AUTH", "")
        cmdline = [self._ttyd, "-p", str(port)]
        if auth:
            cmdline += ["-c", auth]
        # 必须显式 -w：ttyd 的 ConPTY 派生依赖子进程工作目录，缺失会
        # CreateProcessW error 267（目录无效，2026-08-10 真实环境实测）。
        cmdline += ["-W", "-w", cwd] + cmd

        try:
            # 注意：不能用 CREATE_NO_WINDOW！ttyd 在 Windows 上必须持有真实
            # 控制台才能通过 ConPTY 派生子进程（2026-08-10 实测：脱离控制台的
            # ttyd 建了 conhost 但子进程从不执行）。gateway 在真实终端里跑时
            # ttyd 直接继承其控制台；若 gateway 无控制台，ttyd 会自带一个窗口。
            proc = subprocess.Popen(
                cmdline,
                cwd=cwd,
                env=env,
            )
        except Exception as e:
            self._free_port(port)
            raise ValueError(f"启动失败: {e}") from e

        tp = TtydProcess(
            pid=proc.pid,
            port=port,
            agent_key=agent_key,
            project=project,
            cmd=cmd,
            cwd=cwd,
            env=env,
            runtime_file=runtime_file,
            proc=proc,
        )
        with self._lock:
            self._procs[proc.pid] = tp
        # 监视线程：agent 退出 → ttyd 退出，记录 exit_code
        threading.Thread(target=self._watch, args=(tp,), daemon=True).start()
        return tp

    @staticmethod
    def _watch(tp: TtydProcess):
        try:
            code = tp.proc.wait()
        except Exception:
            code = None
        tp.exit_code = code

    def stop(self, pid: int) -> bool:
        """杀 ttyd 进程树（含 agent 子进程）+ 释放端口。"""
        tp = self._procs.get(pid)
        if tp is None:
            return False
        if tp.proc.poll() is None:
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    timeout=10,
                    creationflags=_CREATE_NO_WINDOW,
                )
            except (OSError, subprocess.SubprocessError):
                pass
        self._free_port(tp.port)
        with self._lock:
            self._procs.pop(pid, None)
        return True

    def get(self, pid: int) -> Optional[TtydProcess]:
        return self._procs.get(pid)

    def is_alive(self, pid: int) -> bool:
        tp = self._procs.get(pid)
        if tp is None:
            return False
        return tp.proc.poll() is None

    def running_count(self, agent_key: str) -> int:
        with self._lock:
            return sum(
                1
                for tp in self._procs.values()
                if tp.agent_key == agent_key and tp.proc.poll() is None
            )


# 全局单例
_ttyd_manager: Optional[TtydProcessManager] = None


def get_ttyd_process_manager() -> TtydProcessManager:
    """获取全局 TtydProcessManager 单例。"""
    global _ttyd_manager
    if _ttyd_manager is None:
        _ttyd_manager = TtydProcessManager()
    return _ttyd_manager
