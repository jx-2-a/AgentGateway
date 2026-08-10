# Agent Gateway

个人 **Agent 网关** —— 一个 Web 控制面板，把各种 AI Agent 子进程（科研助手、写作、Claude Code、本地 Shell 等）以「**一个会话一个网址**」的方式跑起来，手机 / 桌面随时接入同一个终端。

> 从「QQ 机器人」演进而来：终端从 QQ 迁移到 Web，网关变成了**个人 Agent OS 的控制终端**。网页控制台是唯一的交互入口。

## 特性

- **真终端（ttyd + ConPTY）**：每个会话独立端口，直接驱动 Windows 原生 ConPTY，无乱码、输入不丢。
- **常驻中继，会话不丢**：浏览器断线 / 刷新都不会终止后台 Agent，多端实时同步看到同一个终端；进程退出后仍可回看完整历史输出。
- **手机通知（不依赖 agent 改动）**：网关分析终端输出，检测到 agent「停下等你输入 / 进程退出」时经 Gotify 推送，点通知直达对应会话终端页。
  - **面板「通知提醒」开关**：关闭一律不推；开启后按「你在不在看」判定。
  - **只看手机**：手机在前台看终端页 = 不打扰；手机切后台 / 离开终端页（桌面开着不算）→ 响铃推送（priority 8）。
- **Gotify 服务自动拉起**：网关启动时自动拉起并守护 Gotify 推送服务，崩了自动重启。
- **系统工具卡片**：Tailscale / 内置 VPN 连接状态与开关、一键释放内存、运行中 Agent 的 CPU/内存占用、本服务信息。
- **独立文件管理页（数据传输）**：按 Agent 的项目目录浏览，手机⇄电脑双向传输（上传 / 下载），图片 / 文本内联预览。
- **会话管理**：多 Agent 多项目，启动 / 停止 / 删除。

## 架构

```
浏览器 (index.html / term.html / files.html)
    │  JSON over WebSocket（走 gateway，带鉴权）
    ▼
gateway (FastAPI)
    ├─ Session 管理：会话生命周期、用户绑定
    ├─ TtydRelay 常驻中继：对每个 ttyd 会话保持连接、缓冲输出、多端广播
    ├─ 注意力识别：分析中继输出（提示符模式 + 输出停顿）→ 判定"等你输入"
    ├─ 运行时监控：轮询 agent 运行时文件 + 注意力识别 → Gotify 推送
    ├─ 系统工具：Tailscale / 内置 VPN、内存释放、Agent 进程资源
    ├─ 文件服务：浏览 / 上传 / 下载 / 预览（含路径越界防护）
    └─ Gotify 守护：自动拉起 + 重启推送服务
    │
    ▼
ttyd (每会话独立进程、独立端口，驱动 ConPTY)
    ▼
Agent 子进程（科研 / WAL写作 / Claude Code / …）
```

- **Agent 注册**：`agents.json` 声明每个 Agent 的启动命令、工作目录、环境变量、项目列表、文件目录。
- **终端链路**：浏览器 → gateway WebSocket（JSON）→ 中继 → ttyd → Agent。ttyd 本身不对外暴露，全部经 gateway 鉴权中转。
- **提醒链路**：Agent 终端输出 → 中继 → 注意力识别（或 agent 主动上报运行时文件）→ gotif → Gotify(:80) → 手机 App。

## 项目结构

```
AgentGateway/
├── bot.py                 # 入口（uvicorn 起 FastAPI）
├── agents.json            # Agent 注册表（启动命令 / 工作目录 / 环境变量）
├── requirements.txt       # Python 依赖
├── .env.example           # 配置模板（复制为 .env）
├── gateway/
│   ├── core/
│   │   ├── ttyd_proc.py   #   按 agents.json 用 ttyd 拉起 Agent 子进程
│   │   ├── ttyd_relay.py  #   常驻中继（多端广播 + 断线保活 + 输出分析）
│   │   ├── session.py     #   会话生命周期管理
│   │   ├── runtime_watch.py # 注意力识别 + 运行时状态轮询 + Gotify 推送
│   │   ├── gotify_proc.py #   Gotify 服务进程守护（自动拉起 / 重启）
│   │   └── system_tools.py #  VPN / Tailscale / 内存 / Agent 进程资源
│   └── adapters/
│       ├── web.py         # FastAPI 路由（鉴权 / 会话 / 文件 / 终端 WS / 系统工具）
│       └── webui/         # index.html 面板 + term.html 终端页 + files.html 文件页
├── gotif/                 # 本地通知支持库（token 不入库，见 gotif/token）
└── ...
```

## 快速开始

```bash
# 1. 安装依赖（Python 3.12+）
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt

# 2. 配置
cp .env.example .env            # 填写 GATEWAY_TOKEN / TTYD_PATH / GOTIFY_PATH 等

# 3. 启动（后台）
start.bat                       # 最小化控制台后台启动，日志写 gateway.log
```

访问 `http://<host>:8080`，用 `.env` 里的 `GATEWAY_TOKEN` 登录，即可从面板启动任意 Agent 并打开其终端。

**停止**：面板「本服务 → 停止服务」按 PID 停止整棵进程树（含 Agent 会话 / gotify）；或运行 `stop.bat`（按端口 8080 杀）。网关正常退出不主动杀 gotify，任务计划式强杀时 gotify 会随之停止、下次启动自动拉起。

**依赖**：ttyd（终端引擎，Windows 可用 `ttyd.win32.exe`，启动参数 `-W -w <cwd>` 不可缺）；Gotify server（可选，手机通知用，网关自动拉起，`GOTIFY_PATH` 指定可执行文件）。

**提醒调参**（`.env`）：`ATTENTION_QUIET` 输出停几秒算「在等输入」、`ATTENTION_ACTIVITY` 输出活动多久算「干过活」（过滤启动横幅）、`ATTENTION_GRACE` 会话建立多久内不推。

## 隐私说明

- `.env`、`gotif/token`、`work.md`、`.claude/`、`.venv` 等已由 `.gitignore` 排除，**不会进入仓库**。
- Agent 的 API key 等敏感配置存放在各 Agent 自身目录的配置文件中（不在本仓库），启动时由 `agents.json` 引用绝对路径加载。
- 推送 `agents.json` 前请确认其中的启动命令/路径无敏感信息（它会被公开或随仓库分发）。
