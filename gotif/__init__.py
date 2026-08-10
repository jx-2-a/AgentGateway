"""Gotify 手机通知推送包（支持库）。

web 后端 / 脚本需要时直接调用::

    from gotif import notify, alert, info

    notify("该干活了", "- 完成 v2.1 部署\n- 回复用户 issue #12")
    alert("磁盘告警", "剩余 2%", url="http://100.104.123.123")
"""

from .notify import alert, get_token, health, info, notify

__all__ = ["notify", "info", "alert", "health", "get_token"]
