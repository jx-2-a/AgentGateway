"""Agent 运行时信息上报模块（协议 v1）。

gateway 启动 agent 时注入环境变量 ``AGENT_RUNTIME_FILE=<绝对路径>``，
本模块负责把 agent 的运行状态原子地写进那个 JSON 文件，供 gateway
轮询/重连。**未设置该环境变量时全部 no-op**——agent 独立运行不受影响。

状态机：starting → ready → working ⇄ waiting_input → exited

用法（在 agent 的 REPL 里）::

    import agent_runtime as rt
    rt.mark_ready(project=project, mode=mode)      # 启动完成
    # 每次执行前
    rt.mark_working(progress="...")
    # 每次等用户输入前
    with rt.waiting(prompt="请确认？", suggestions=["继续", "停止"]):
        line = input(prompt)
    # 退出时
    rt.mark_exited()

写文件是原子的（写 .tmp 再 os.replace），多线程安全（模块级锁）。
"""

import json
import os
import threading
import time

_PATH = os.environ.get("AGENT_RUNTIME_FILE", "").strip()
_lock = threading.Lock()


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _write(data: dict):
    if not _PATH:
        return
    try:
        d = os.path.dirname(_PATH)
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        tmp = _PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, _PATH)
    except Exception:
        pass  # 上报失败不影响 agent 本身


def mark(status: str, **fields):
    """写一条状态记录。status: starting|ready|working|waiting_input|exited。"""
    with _lock:
        data = {
            "schema": 1,
            "pid": os.getpid(),
            "status": status,
            "updated_at": _now(),
            "exit_code": None,
        }
        data.update(fields)
        _write(data)


def mark_ready(project: str = "", mode: str = ""):
    mark("ready", project=project, mode=mode)


def mark_working(progress: str = "", project: str = "", mode: str = ""):
    mark("working", progress=progress, project=project, mode=mode)


def mark_waiting(prompt: str = "", suggestions=None, project: str = "", mode: str = ""):
    mark(
        "waiting_input",
        waiting_prompt=prompt,
        suggestions=suggestions or [],
        waiting_since=_now(),
        project=project,
        mode=mode,
    )


def mark_exited(exit_code: int = 0):
    mark("exited", exit_code=exit_code)


class waiting:
    """上下文管理器：进入块时标记 waiting_input，退出块时标记 working。

    ::

        with rt.waiting(prompt="请确认？", suggestions=["继续"]):
            line = input(prompt)
    """

    def __init__(self, prompt: str = "", suggestions=None):
        self.prompt = prompt
        self.suggestions = suggestions

    def __enter__(self):
        mark_waiting(prompt=self.prompt, suggestions=self.suggestions)
        return self

    def __exit__(self, *exc):
        mark_working()
        return False
