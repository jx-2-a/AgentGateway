"""轻量推送通知（提醒通道）。

从项目根 .env 读 ``NOTIFY_URL_TEMPLATE``，把 ``{title}``/``{content}``
占位符替换后后台线程发 HTTP GET。模板为空则禁用（仅网页可见）。

免费推荐 PushPlus（微信推送，无需额外 App）：
    NOTIFY_URL_TEMPLATE=https://www.pushplus.plus/send?token=YOUR_TOKEN&title={title}&content={content}
Bark（iPhone）：
    NOTIFY_URL_TEMPLATE=https://api.day.app/YOUR_KEY/{title}/{content}
任意支持 GET 的 webhook 同理。

发送是 best-effort：失败静默，不阻塞调用方（runtime_watch 线程里调用）。
"""

import os
import threading
import urllib.parse
import urllib.request
from pathlib import Path


def _read_template() -> str:
    """读 .env 的 NOTIFY_URL_TEMPLATE。"""
    tpl = os.getenv("NOTIFY_URL_TEMPLATE")
    if tpl:
        return tpl
    env_path = Path(__file__).resolve().parent.parent.parent / ".env"
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("NOTIFY_URL_TEMPLATE") and "=" in line:
                return line.partition("=")[2].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


_TEMPLATE = _read_template()


def is_enabled() -> bool:
    return bool(_TEMPLATE)


def _send_in_thread(url: str):
    try:
        with urllib.request.urlopen(url, timeout=8):
            pass
    except Exception:
        pass


def send(title: str, content: str = ""):
    """推送一条通知。未配置模板则 no-op。"""
    if not _TEMPLATE:
        return
    try:
        url = (
            _TEMPLATE
            .replace("{title}", urllib.parse.quote(title))
            .replace("{content}", urllib.parse.quote(content))
        )
    except Exception:
        return
    threading.Thread(target=_send_in_thread, args=(url,), daemon=True).start()
