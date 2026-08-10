"""Agent Gateway Web 入口（纯 FastAPI）。

只跑网页控制面板：真终端（ConPTY）、文件浏览、会话管理。
启动: python bot.py   （HOST / PORT 从 .env 读取，默认 0.0.0.0:8080）
"""

from pathlib import Path

import uvicorn
from fastapi import FastAPI

from gateway.adapters.web import router as web_router

app = FastAPI(title="Agent Gateway")
app.include_router(web_router)


def _read_env(key: str, default: str) -> str:
    """从项目根 .env 读配置（简单 key=value 解析）。"""
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                if k.strip() == key:
                    return v.strip()
    return default


if __name__ == "__main__":
    host = _read_env("HOST", "0.0.0.0")
    port = int(_read_env("PORT", "8080"))
    uvicorn.run(app, host=host, port=port)
