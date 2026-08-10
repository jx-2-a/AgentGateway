"""网关重启助手 —— 独立进程：等旧网关释放端口后拉起新网关。

由 /api/system/gateway/restart 处理函数 spawn（DETACHED_PROCESS），
流程：
1. 轮询等待端口（默认 8080，或 .env PORT）释放 —— 旧网关退出。
2. 用 `cmd /c start /min gateway_run.bat` 拉起新网关（最小化窗口后台）。

为什么需要独立进程：重启 = 旧网关要杀自己 + 杀会话/gotify；若在同一个
进程里做，进程没了就没人拉起新的了。本助手用 DETACHED 方式 spawn，且
旧网关只 os._exit 自己（不 /T 杀整树），所以本进程能活到拉起新网关。
"""

import socket
import subprocess
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
_port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
_TIMEOUT = 30


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


def main():
    # 等旧网关退出（端口释放）
    waited = 0
    while waited < _TIMEOUT:
        if _port_free(_port):
            break
        time.sleep(1)
        waited += 1
    # 拉起新网关：隐藏控制台后台跑（launch_gateway.vbs），日志写 gateway.log。
    # 不要用 `cmd /c start /min`——从无控制台进程拉起时 /min 不生效会出黑框。
    subprocess.Popen(
        ["wscript.exe", str(_ROOT / "launch_gateway.vbs")],
        cwd=str(_ROOT),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


if __name__ == "__main__":
    main()
