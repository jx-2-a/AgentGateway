"""Gotify 支持库自检：发 3 条测试通知，手机应收到——基础 / markdown简报 / 高优告警。"""
from notify import alert, notify

# 1. 基础
notify("测试通知", "Gotify 连接成功！")
print("basic -> ok")

# 2. markdown 简报 + 点击跳转（Tailscale 上的 Gotify 网页端）
notify(
    "今日待办",
    "该干活了：\n\n"
    "- **完成 v2.1 部署**\n"
    "- 回复用户 issue #12\n"
    "- 更新文档\n\n"
    "> 详情见网页端",
    priority=7,
    url="http://100.104.123.123",
)
print("brief -> ok")

# 3. 高优告警
alert("告警", "服务器负载过高！", url="http://100.104.123.123")
print("alert -> ok")
