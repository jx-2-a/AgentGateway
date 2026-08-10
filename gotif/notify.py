"""Gotify 手机通知推送 —— 支持库（web 后端的提醒通道）。

定位：给 web 后端 / 脚本提供手机通知能力。提醒"该干活了" + 一句简单概括，
支持简单 markdown 排版和点击跳转链接。主力交互在 web 面板，这里只负责推送。

用法（Python，web 后端需要时直接 import）::

    from gotif import notify, alert, info

    notify("该干活了", "- 完成 v2.1 部署\n- 回复用户 issue #12")
    alert("磁盘告警", "剩余 2%", url="http://100.104.123.123")

命令行::

    python notify.py "标题" "内容"
    python notify.py "告警" "磁盘满" --priority 8 --url "http://100.104.123.123"
    python notify.py -c                     # 只检查 Gotify 是否在线
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Optional

import requests

GOTIFY_URL = os.environ.get("GOTIFY_URL", "http://localhost").rstrip("/")
_DEFAULT_TOKEN_FILE = Path(__file__).with_name("token")
_TIMEOUT = 10


def get_token() -> str:
    """读取 Gotify token：优先环境变量 GOTIFY_TOKEN，其次同目录 token 文件。"""
    token = os.environ.get("GOTIFY_TOKEN")
    if token and token.strip():
        return token.strip()
    if _DEFAULT_TOKEN_FILE.exists():
        return _DEFAULT_TOKEN_FILE.read_text(encoding="utf-8").strip()
    raise RuntimeError("未找到 Gotify token：请设置环境变量 GOTIFY_TOKEN 或在同目录放置 token 文件")


def notify(
    title: str,
    message: str = "",
    priority: int = 5,
    url: Optional[str] = None,
    markdown: bool = True,
    token: Optional[str] = None,
) -> int:
    """发一条通知，返回 Gotify 消息 id。非 200 抛异常。

    - ``url``: 点通知跳转的链接（手机浏览器打开）。
    - ``markdown``: 默认开，正文支持 **加粗** / `代码` / - 列表 / > 引用 等简单 markdown。
      纯文本传 ``markdown=False``。
    - ``priority``: 0~10，默认 5。
    """
    if not 0 <= priority <= 10:
        raise ValueError(f"priority 必须在 0~10 之间，收到 {priority}")

    extras: dict = {}
    if markdown:
        extras["client::display"] = {"contentType": "text/markdown"}
    if url:
        extras["client::notification"] = {"click": {"url": url}}

    payload = {"title": title, "message": message, "priority": priority}
    if extras:
        payload["extras"] = extras

    resp = requests.post(
        f"{GOTIFY_URL}/message",
        params={"token": token or get_token()},
        json=payload,
        timeout=_TIMEOUT,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Gotify 发送失败：HTTP {resp.status_code} {resp.text}")
    return int(resp.json()["id"])


def info(title: str, message: str = "", **kw) -> int:
    """低打扰通知（priority 1）。可透传 url/markdown 等。"""
    return notify(title, message, priority=1, **kw)


def alert(title: str, message: str = "", **kw) -> int:
    """高优告警（priority 8）。可透传 url/markdown 等。"""
    return notify(title, message, priority=8, **kw)


def health() -> bool:
    """检查 Gotify 服务是否在线。"""
    try:
        return requests.get(f"{GOTIFY_URL}/health", timeout=_TIMEOUT).json().get("health") == "green"
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 命令行入口
# ---------------------------------------------------------------------------


def _main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="notify",
        description="Gotify 手机通知推送（支持库）",
    )
    parser.add_argument("title", nargs="?", default="", help="通知标题（-c 检查模式可省略）")
    parser.add_argument("message", nargs="?", default="", help="通知内容（可省略，此时只发标题）")
    parser.add_argument("-p", "--priority", type=int, default=5, help="优先级 0~10，默认 5")
    parser.add_argument("-u", "--url", help="点通知跳转的链接")
    parser.add_argument("--plain", action="store_true", help="纯文本，不用 markdown 渲染")
    parser.add_argument("-c", "--check", action="store_true", help="仅检查 Gotify 是否在线，不发消息")
    parser.add_argument("--token", help="临时指定 token（默认读环境变量 GOTIFY_TOKEN / token 文件）")
    args = parser.parse_args(argv)

    if args.check:
        ok = health()
        print("online" if ok else "offline")
        return 0 if ok else 1

    if not args.title:
        parser.error("缺少通知标题")

    try:
        mid = notify(
            args.title,
            args.message,
            priority=args.priority,
            url=args.url,
            markdown=not args.plain,
            token=args.token,
        )
        # Windows 控制台 GBK 会崩非 GBK 字符（emoji 等），这里保持 ASCII 输出
        print(f"OK: message #{mid} sent, priority {args.priority}")
        return 0
    except Exception as e:
        print(f"FAILED: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(_main())
