"""Gotify 推送服务进程管理 —— 网关拉起并守护（手机通知的服务器端）。

手机通知链路：gateway(runtime_watch) → gotif 库 → Gotify server(:80)
→ Android App（铃声+震动+悬浮窗）。Gotify server 是独立进程，不是
ttyd 那种每会话一个；全局唯一、常驻。

本模块负责：网关启动时确保 Gotify 在跑，崩了自动重启（限次防循环）。
- 已在跑（/health 返回 green）→ 什么都不做（可能是用户手动拉起的，复用）。
- 没在跑 → 用子进程拉起，cwd=安装目录（默认配置在这里找 ./data 数据库，
  token / 应用 全部保留）。
- gateway 正常退出不主动杀 gotify（手机通知不该因网关重启而断）；
  用 taskkill /T 强杀网关进程树时 gotify 会一起死，下次网关启动自动拉起。
"""

import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

# Windows 下隐藏子进程控制台窗口（gotify 是 web 服务，不需要控制台）
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# 崩溃自动重启上限（超过则放弃，避免循环拉起）
_MAX_RESTARTS = 5


def _read_env(key: str, default: str = "") -> str:
    """读项目根 .env（os.environ 优先，去行内注释）。"""
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
                    if "#" in v:
                        v = v.split("#", 1)[0]
                    return v.strip()
    except OSError:
        pass
    return default


class GotifyProcManager:
    """管理 Gotify server 子进程（单例）。"""

    def __init__(self):
        self._exe = _read_env(
            "GOTIFY_PATH", r"D:\OpenSourcePro\Gotify\gotify-windows-amd64.exe"
        )
        self._url = _read_env("GOTIFY_URL", "http://localhost").rstrip("/")
        self._dir = str(Path(self._exe).resolve().parent)
        self._proc: Optional[subprocess.Popen] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._restart_count = 0

    # ---- 对外 ----

    def ensure_running(self):
        """网关启动时调用：Gotify 没在跑就拉起（已在跑则复用）。"""
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return
            if self.is_up():
                print(f"[Gotify] 已在运行（{self._url}），复用")
                return
            log_path = Path(self._dir) / "gotify.log"
            try:
                logf = open(log_path, "ab")
            except OSError:
                logf = None
            try:
                self._proc = subprocess.Popen(
                    [self._exe, "serve"],
                    cwd=self._dir,
                    creationflags=_CREATE_NO_WINDOW,
                    stdout=logf,
                    stderr=subprocess.STDOUT,
                )
            except Exception as e:
                print(f"[Gotify] 启动失败: {e}")
                return
            print(
                f"[Gotify] 已启动 pid={self._proc.pid} "
                f"(cwd={self._dir}, 日志={log_path.name})"
            )
        # 看门狗：进程崩了自动重启
        threading.Thread(target=self._watch, args=(logf,), daemon=True).start()

    def is_up(self) -> bool:
        """Gotify /health 是否 green。"""
        import requests

        try:
            r = requests.get(f"{self._url}/health", timeout=3)
            return r.json().get("health") == "green"
        except Exception:
            return False

    def get_pid(self) -> Optional[int]:
        return self._proc.pid if self._proc else None

    # ---- 内部 ----

    def _watch(self, logf):
        """等待进程退出；非主动停止则限次重启。"""
        try:
            code = self._proc.wait()
        except Exception:
            code = None
        try:
            if logf is not None:
                logf.close()
        except Exception:
            pass
        self._restart_count += 1
        if not self._stop.is_set() and self._restart_count <= _MAX_RESTARTS:
            print(f"[Gotify] 进程退出 code={code}，5 秒后自动重启")
            time.sleep(5)
            self.ensure_running()
        elif not self._stop.is_set():
            print(
                f"[Gotify] 进程退出 code={code}，已重启 {_MAX_RESTARTS} 次，放弃自动重启"
            )


# 全局单例
_gotify_manager: Optional[GotifyProcManager] = None


def get_gotify_manager() -> GotifyProcManager:
    """获取全局 GotifyProcManager 单例。"""
    global _gotify_manager
    if _gotify_manager is None:
        _gotify_manager = GotifyProcManager()
    return _gotify_manager
