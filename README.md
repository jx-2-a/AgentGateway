# Agent Gateway

个人 **Agent 网关** —— 一个 Web 控制面板，把各种 AI Agent 子进程（科研助手、写作、Claude Code、本地 Shell 等）以「**一个会话一个网址**」的方式跑起来，手机 / 桌面随时接入同一个终端。

> 从「QQ 机器人」演进而来：终端从 QQ 迁移到 Web，网关变成了**个人 Agent OS 的控制终端**。网页控制台是唯一的交互入口。

## 特性

- **真终端（ttyd + ConPTY）**：每个会话独立端口，直接驱动 Windows 原生 ConPTY，无乱码、输入不丢。
- **常驻中继，会话不丢**：浏览器断线 / 刷新都不会终止后台 Agent，多端实时同步看到同一个终端。
- **只读回放**：进程退出后仍可回看完整历史输出。
- **手机通知**：Agent 等待输入或进程退出时，经 Gotify 推送，点通知直达对应会话。
- **文件浏览**：按 Agent 的项目根目录浏览 / 预览文件。
- **会话管理**：启动 / 停止 / 删除，命令行面板，一键本地 Shell。

## 架构

```
浏览器 (index.html / term.html)
    │  JSON over WebSocket（走 gateway，带鉴权）
    ▼
gateway (FastAPI)
    ├─ Session 管理：会话生命周期、用户绑定
    ├─ TtydRelay 常驻中继：对每个 ttyd 会话保持连接、缓冲输出、多端广播
    ├─ 运行时监控：轮询 agent 运行时文件 → Gotify 推送
    └─ 文件浏览 / 命令面板
    │
    ▼
ttyd (每会话独立进程、独立端口，驱动 ConPTY)
    ▼
Agent 子进程（科研 / WAL写作 / Claude Code / …）
```

- **Agent 注册**：`agents.json` 声明每个 Agent 的启动命令、工作目录、环境变量、项目列表。
- **终端链路**：浏览器 → gateway WebSocket（JSON）→ 中继 → ttyd → Agent。ttyd 本身不对外暴露，全部经 gateway 鉴权中转。

## 项目结构

```
AgentGateway/
├── bot.py                 # 入口（uvicorn 起 FastAPI）
├── agents.json            # Agent 注册表（启动命令 / 工作目录 / 环境变量）
├── gateway/
│   ├── core/              # 核心逻辑：session / registry / ttyd 引擎与中继 / 运行时监控
│   │   ├── ttyd_proc.py   #   按 agents.json 用 ttyd 拉起 Agent 子进程
│   │   ├── ttyd_relay.py  #   常驻中继（多端广播 + 断线保活）
│   │   ├── session.py     #   会话生命周期管理
│   │   └── runtime_watch.py # 运行时状态轮询 + Gotify 推送
│   └── adapters/
│       ├── web.py         # FastAPI 路由（鉴权 / 会话 / 文件 / 终端 WS）
│       └── webui/         # 前端：index.html 面板 + term.html 终端页 + xterm.js
├── gotif/                 # 本地通知支持库（token 不入库，见 .env / gotif/token）
└── .env.example           # 配置模板
```

## 快速开始

```bash
# 1. 安装依赖（Python 3.12+）
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt

# 2. 配置
cp .env.example .env            # 填写 GATEWAY_TOKEN / TTYD_PATH 等

# 3. 启动
python bot.py                   # 默认 0.0.0.0:8080
```

访问 `http://<host>:8080`，用 `.env` 里的 `GATEWAY_TOKEN` 登录，即可从面板启动任意 Agent 并打开其终端。

**依赖**：ttyd（终端引擎，Windows 可用 `ttyd.win32.exe`，启动参数 `-W -w <cwd>` 不可缺）。

## 隐私说明

- `.env`、`gotif/token`、`work.md`、`.claude/`、`.venv` 等已由 `.gitignore` 排除，**不会进入仓库**。
- Agent 的 API key 等敏感配置存放在各 Agent 自身目录的配置文件中（不在本仓库），启动时由 `agents.json` 引用绝对路径加载。
- 推送 `agents.json` 前请确认其中的启动命令/路径无敏感信息（它会被公开或随仓库分发）。
