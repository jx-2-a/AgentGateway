"""Agent 进程管理模块（WinPTY 真终端版）。

每个 Agent 子进程跑在 WinPTY 伪终端下（pywinpty）：
- Agent 拿到真终端（isatty()==True），rich 彩色/光标动画/Ctrl+C/箭头键
  全部可用（如 LearnLove 的 msvcrt 模式也能工作）。
- 输出**原样保留 ANSI 与 CRLF**，切成原始 chunk 存进有界缓冲供 xterm.js
  渲染，不再切行/清洗。
- 每个 WS 客户端持独立 index 拉增量，多客户端（本地+手机）共享同一终端。

关键决策（2026-08-10 全部实测）：
1. **用 PtyProcess（socket 中转层）+ WinPTY backend**：
   - 底层 PTY 直读（raw PTY 类）在"reader 轮询 read 的同时主线程写输入"
     场景会竞态——子进程收到输入并输出后，reader 收不到后续输出。
     实测连发 5 条输入只有前几条回显。
   - PtyProcess 内部有独立线程把 C 层输出经 TCP socket 中转，读 socket
     与写 C 层解耦，实测连发 5 条输入全部回显（5/5）。
   - ConPTY backend 经 socket 层丢输入（实测 0/5），必须用 WinPTY。
2. **VT 引导注入（本模块核心修复）**：WinPTY 会把子进程"裸写进控制台
   缓冲区"的 ESC 字符(0x1B)转成 '?'（\x1b[31m → ?[31m），网页终端出现
   乱码/排版错乱。注入 _vt_bootstrap/sitecustomize.py 让子进程 Python
   启动即开 ENABLE_VIRTUAL_TERMINAL_PROCESSING，conhost 消费转义序列后
   winpty-agent 按屏幕缓冲区属性输出干净的 \x1b[...m 序列。实测
   \x1b[0;31m 这类输出正常、无 '?' 垃圾、中文正常。
3. **所有 C 层写调用加锁串行化**：write/terminate 与内部读线程并发时
   偶发竞态，用 ap.pty_lock 串行，降低风险。

已知限制（winpty-rs issue #84）：进程"打印最后一行后立即退出"时，那行
输出可能被 EOF 截断丢失；reader 死后 drain 尽量捞回，但不能 100%。
"""

import codecs
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from gateway.core.registry import get_agent_registry

# pywinpty 3.x 的模块名是 winpty（pywinpty 是兼容别名）
try:
    from winpty import PtyProcess, Backend
except ImportError:  # pragma: no cover
    from pywinpty import PtyProcess, Backend

# 强制子进程 utf-8（ConPTY 下 Python 走 WriteConsoleW，UTF-8 正常）
_FORCE_ENV = {
    "PYTHONIOENCODING": "utf-8",
    "PYTHONUTF8": "1",
    "PYTHONUNBUFFERED": "1",
}

# VT 引导目录：含 sitecustomize.py，注入子进程 PYTHONPATH 首位，
# 让子进程 Python 在启动时开启 ENABLE_VIRTUAL_TERMINAL_PROCESSING。
# 否则 WinPTY backend 会把裸写进控制台缓冲区的 ESC 字符转成 '?'，
# 网页终端出现 ?[31m 这类乱码。详见 _vt_bootstrap/sitecustomize.py。
_VT_BOOTSTRAP_DIR = str(Path(__file__).resolve().parent / "_vt_bootstrap")

# 缓冲上限：原始字符数（含 ANSI），超限从头部裁剪
_MAX_BUF_CHARS = 256 * 1024

# 每次 proc.read 的字符数
_READ_CHUNK = 4096

# Windows 下隐藏子进程控制台窗口
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# 子进程启动时会发终端能力查询（如 \x1b[c DA、\x1b[1t 窗口状态），
# 真终端会回响应。没人回它就等 ~3 秒超时才继续 → 输出整体延迟 3 秒。
# 这里按真实终端行为回应，并把查询序列从转发给 xterm.js 的输出里剥掉
# （避免 xterm.js 再回一次导致双重响应）。
# 注意：只对 \x1b[c（DA 设备属性查询）回应——它是子进程真正阻塞等响应的，
# 回 \x1b[?1;2c 后立即解除 3 秒等待且被子进程正确消费。
# 其他查询（\x1b[1t 等）只剥不回应：回应会原样泄进子进程输入流，
# 被 input() 当成用户输入，污染后续交互。
_QUERY_RESPONSES = (
    ("\x1b[?1004h", ""),      # 启用焦点上报，无需响应
    ("\x1b[?9001h", ""),      # 启用 win32 输入
    ("\x1b[1t", ""),          # 窗口状态查询
    ("\x1b[c", "\x1b[?1;2c"), # DA 设备属性查询 → 回 VT100 应答
    ("\x1b[6n", ""),          # 光标位置查询
    ("\x1b[18t", ""),         # 文本区尺寸查询
)


@dataclass
class AgentProcess:
    """一个跑在 WinPTY 下的 Agent 子进程句柄。"""

    pid: int
    agent_key: str
    project: Optional[str]
    cmd: list
    cwd: str
    env: dict
    proc: object  # winpty.PtyProcess
    # ---- 原始输出缓冲（reader 线程写，WS 读） ----
    buf: list = field(default_factory=list)  # 原始 chunk 列表（含 ANSI）
    buf_len: int = 0                          # 当前缓冲总字符数
    trimmed: int = 0                          # 已从头部裁剪的累计字符数
    # ---- 生命周期 ----
    exited: bool = False
    exit_code: Optional[int] = None
    stopping: bool = False                    # 用户主动 stop 标记（跳过推送）
    started_at: datetime = field(default_factory=datetime.now)
    lock: threading.Lock = field(default_factory=threading.Lock)
    on_exit: Optional[Callable[["AgentProcess"], None]] = None
    # C 层写操作互斥锁（write/terminate 与内部读线程并发时偶发竞态）
    pty_lock: threading.Lock = field(default_factory=threading.Lock)
    runtime_file: str = ""  # agent 上报运行时状态的 JSON 文件路径


@dataclass
class OutputChunk:
    """WS 增量拉取的返回结果（原始数据，含 ANSI）。"""

    new_index: int
    data: str
    exited: bool
    exit_code: Optional[int]


class AgentProcessManager:
    """管理所有 WinPTY Agent 子进程。"""

    def __init__(self):
        self._processes: dict[int, AgentProcess] = {}
        self._lock = threading.Lock()

    # ---- 内部工具 ----

    @staticmethod
    def _sub(agent, value, project: Optional[str]):
        """把 {root}/{venv}/{project} 占位符替换成实际路径。"""
        project = project or ""
        if isinstance(value, str):
            return (
                value.replace("{root}", agent.root)
                .replace("{venv}", agent.venv)
                .replace("{project}", project)
            )
        if isinstance(value, list):
            return [AgentProcessManager._sub(agent, v, project) for v in value]
        if isinstance(value, dict):
            return {k: AgentProcessManager._sub(agent, v, project) for k, v in value.items()}
        return value

    @staticmethod
    def _load_dotenv(path: str) -> dict:
        """解析 .env 文件为 dict（python-dotenv 不可用时手动解析）。"""
        try:
            from dotenv import dotenv_values

            values = dotenv_values(path)
            return {k: v for k, v in values.items() if v is not None}
        except ImportError:
            pass
        result = {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                for raw in f:
                    line = raw.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, value = line.partition("=")
                    result[key.strip()] = value.strip().strip('"').strip("'")
        except OSError:
            pass
        return result

    @staticmethod
    def _coerce_str(raw, decoder) -> str:
        """pywinpty read() 的返回值统一成 str（兼容 str/bytes/None）。"""
        if raw is None:
            return ""
        if isinstance(raw, str):
            return raw
        if isinstance(raw, bytes):
            return decoder.decode(raw)
        return str(raw)

    def _strip_queries(self, ap: AgentProcess, text: str) -> str:
        """回应终端能力查询并把查询序列从输出中剥掉。

        返回剥掉查询后的文本；每个查询对应的响应写回 PTY 输入。
        """
        if "\x1b" not in text:
            return text
        cleaned = text
        for query, response in _QUERY_RESPONSES:
            if query in cleaned:
                cleaned = cleaned.replace(query, "")
                if response:
                    try:
                        with ap.pty_lock:
                            ap.proc.write(response)
                    except Exception:
                        pass
        return cleaned

    def _push_chunk(self, ap: AgentProcess, text: str):
        """追加一个原始输出 chunk 到缓冲，超限从头部裁剪。"""
        if not text:
            return
        text = self._strip_queries(ap, text)
        if not text:
            return
        with ap.lock:
            ap.buf.append(text)
            ap.buf_len += len(text)
            while ap.buf_len > _MAX_BUF_CHARS and ap.buf:
                head = ap.buf.pop(0)
                ap.buf_len -= len(head)
                ap.trimmed += len(head)

    def _reader(self, ap: AgentProcess):
        """后台读线程：WinPTY 阻塞读 + drain 尾部 + 置终态 + 触发 on_exit。"""
        proc = ap.proc
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        idle = 0
        try:
            while True:
                if not proc.isalive():
                    break
                try:
                    raw = proc.read(_READ_CHUNK)  # 有数据即返回，空则短暂等待
                except Exception:
                    break
                text = self._coerce_str(raw, decoder)
                if text:
                    self._push_chunk(ap, text)
                    idle = 0
                else:
                    idle += 1
                    time.sleep(0.02)
                    if idle > 200:  # 兜底：~4s 无数据，重置计数继续等
                        idle = 0
        except Exception:
            pass

        # 退出后 drain：尽量捞回 EOF 截断的尾输出（winpty-rs issue #84）
        try:
            for _ in range(200):
                raw = proc.read(_READ_CHUNK)
                text = self._coerce_str(raw, decoder)
                if not text:
                    break
                self._push_chunk(ap, text)
        except Exception:
            pass

        # 终态
        code = None
        try:
            code = proc.exitstatus if hasattr(proc, "exitstatus") else None
        except Exception:
            code = None
        with ap.lock:
            ap.exited = True
            ap.exit_code = code

        hook = ap.on_exit
        if hook is not None:
            try:
                hook(ap)
            except Exception:
                pass

    # ---- 生命周期 ----

    def start(
        self,
        agent_key: str,
        project: Optional[str] = None,
        rows: int = 30,
        cols: int = 120,
        on_exit: Optional[Callable[[AgentProcess], None]] = None,
        runtime_file: str = "",
    ) -> AgentProcess:
        """按 agents.json 配置在一个 WinPTY 下拉取 Agent 子进程。

        runtime_file 非空时注入 AGENT_RUNTIME_FILE 环境变量，agent 通过
        它写运行时状态文件（见 gateway/core/agent_runtime.py）。
        """
        registry = get_agent_registry()
        agent = registry.get_agent(agent_key)
        if agent is None:
            raise ValueError(f"Agent 不存在: {agent_key}")
        if not agent.cmd:
            raise ValueError(f"Agent '{agent_key}' 未配置启动命令 (cmd)")

        cmd = self._sub(agent, agent.cmd, project)
        cwd = self._sub(agent, agent.cwd or agent.root, project)

        env = os.environ.copy()
        env.update(_FORCE_ENV)
        env.update(self._sub(agent, agent.env or {}, project))
        if agent.load_env_from:
            dotenv_path = self._sub(agent, agent.load_env_from, project)
            if os.path.isfile(dotenv_path):
                for k, v in self._load_dotenv(dotenv_path).items():
                    env.setdefault(k, v)

        # VT 引导注入：保证子进程 Python 启动即开 VT 模式，
        # 避免 WinPTY 把 ESC 变 '?' 造成网页终端乱码。
        existing_pp = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (
            _VT_BOOTSTRAP_DIR + os.pathsep + existing_pp
            if existing_pp else _VT_BOOTSTRAP_DIR
        )

        # 运行时状态文件注入：agent 通过它向 gateway 上报 waiting/working 等
        if runtime_file:
            env["AGENT_RUNTIME_FILE"] = runtime_file

        try:
            proc = PtyProcess.spawn(
                cmd,
                cwd=cwd,
                env=env,
                dimensions=(rows, cols),  # rows 在前
                # 用老版 WinPTY backend（非 ConPTY）：实测 ConPTY 经 socket
                # 层丢输入（发 5 条收 0 条），WinPTY 输入输出都可靠。
                backend=Backend.WinPTY,
            )
        except Exception as e:
            raise ValueError(f"启动失败: {e}") from e

        ap = AgentProcess(
            pid=proc.pid,
            agent_key=agent_key,
            project=project,
            cmd=cmd,
            cwd=cwd,
            env=env,
            proc=proc,
            on_exit=on_exit,
            runtime_file=runtime_file,
        )
        with self._lock:
            self._processes[proc.pid] = ap
        threading.Thread(target=self._reader, args=(ap,), daemon=True).start()
        return ap

    def release(self, pid: int):
        """从进程表中移除一个进程记录（session 删除时调用）。"""
        with self._lock:
            self._processes.pop(pid, None)

    def get(self, pid: int) -> Optional[AgentProcess]:
        return self._processes.get(pid)

    # ---- 交互 ----

    def send_raw(self, pid: int, text: str) -> bool:
        """向 WinPTY 写原始文本（含 ANSI 转义、中文）。"""
        ap = self._processes.get(pid)
        if ap is None or ap.exited:
            return False
        try:
            with ap.pty_lock:
                ap.proc.write(text)
            return True
        except Exception:
            return False

    def send_line(self, pid: int, text: str) -> bool:
        """发送一行（WinPTY 下回车是 \\r）。"""
        return self.send_raw(pid, text + "\r")

    def resize(self, pid: int, cols, rows) -> bool:
        """调整 WinPTY 窗口尺寸。"""
        ap = self._processes.get(pid)
        if ap is None or ap.exited:
            return False
        try:
            cols, rows = int(cols), int(rows)
            if cols < 1 or rows < 1:
                return False
            with ap.pty_lock:
                ap.proc.setwinsize(rows, cols)  # rows 在前
            return True
        except Exception:
            return False

    # ---- 输出读取 ----

    def current_index(self, pid: int) -> int:
        ap = self._processes.get(pid)
        if ap is None:
            return 0
        with ap.lock:
            return ap.trimmed + ap.buf_len

    def trimmed_index(self, pid: int) -> int:
        """裁剪边界（WS 重放窗口的下限）。"""
        ap = self._processes.get(pid)
        if ap is None:
            return 0
        with ap.lock:
            return ap.trimmed

    def read_from(self, pid: int, from_index: int) -> OutputChunk:
        """返回从 from_index 开始的增量原始数据，以及进程是否已退出。"""
        ap = self._processes.get(pid)
        if ap is None:
            return OutputChunk(from_index, "", True, None)
        with ap.lock:
            rel = from_index - ap.trimmed
            if rel < 0:
                rel = 0  # 客户端落后于裁剪边界 → 钳到 0
            rel = min(rel, len(ap.buf))
            data = "".join(ap.buf[rel:])
            new_index = ap.trimmed + ap.buf_len
            exited = ap.exited
            exit_code = ap.exit_code
        return OutputChunk(new_index, data, exited, exit_code)

    # ---- 停止 ----

    def stop(self, pid: int, graceful_timeout: float = 0.0) -> bool:
        """停止进程。graceful_timeout>0 时先发 exit 等进程自己退出。"""
        ap = self._processes.get(pid)
        if ap is None:
            return False
        with ap.lock:
            ap.stopping = True  # 让 on_exit 钩子跳过推送

        if not ap.exited and graceful_timeout > 0:
            self.send_line(pid, "exit")
            deadline = time.monotonic() + graceful_timeout
            while not ap.exited and time.monotonic() < deadline:
                time.sleep(0.1)
            if ap.exited:
                return True

        if not ap.exited:
            try:
                with ap.pty_lock:
                    ap.proc.terminate(force=True)  # WinPTY 强杀
            except Exception:
                pass
            # 防残留进程树
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    timeout=10,
                    creationflags=_CREATE_NO_WINDOW,
                )
            except (OSError, subprocess.SubprocessError):
                pass

        # 看门狗：等 reader 收尾，超时强制置终态（防 reader 阻塞卡死）
        deadline = time.monotonic() + 3.0
        while not ap.exited and time.monotonic() < deadline:
            time.sleep(0.05)
        if not ap.exited:
            with ap.lock:
                ap.exited = True
                ap.exit_code = None
        return True

    # ---- 统计 ----

    def list_pids(self, agent_key: Optional[str] = None) -> list:
        with self._lock:
            return [
                pid
                for pid, ap in self._processes.items()
                if agent_key is None or ap.agent_key == agent_key
            ]

    def running_count(self, agent_key: str) -> int:
        with self._lock:
            return sum(
                1
                for ap in self._processes.values()
                if ap.agent_key == agent_key and not ap.exited
            )

    def recent_lines(self, pid: int, n: int = 20) -> list:
        """取最近 n 行（去 ANSI），供推送小结使用。"""
        ap = self._processes.get(pid)
        if ap is None:
            return []
        with ap.lock:
            data = "".join(ap.buf)
        clean = _ANSI_RE.sub("", data)
        lines = [l.rstrip() for l in clean.splitlines() if l.strip()]
        return lines[-n:]


# ANSI 清洗（仅 recent_lines 用，reader 保持原始）
_ANSI_RE = __import__("re").compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"
    r"|\x1b\][^\x07]*(\x07|\x1b\\)"
    r"|\x1b[@-Z\\-_]"
)


# 全局单例
_process_manager: Optional[AgentProcessManager] = None


def get_agent_process_manager() -> AgentProcessManager:
    """获取全局 AgentProcessManager 单例。"""
    global _process_manager
    if _process_manager is None:
        _process_manager = AgentProcessManager()
    return _process_manager
