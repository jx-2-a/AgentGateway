"""运行时状态轮询 + 推送 + 启动 rediscovery。

后台 daemon 线程每 ~1s：
1. 轮询每个带 runtime_file 的 Session，读 agent 上报的 JSON，更新
   Session 的 runtime_status/waiting_prompt/progress/suggestions 等。
2. 状态「进入 waiting_input / 变为 exited」时触发一次通知（去重，避免
   长时间等待反复轰炸）。agent 重新进入 waiting_input 会再通知。
3. 首次运行时做 rediscovery：扫描各注册 agent 的 <root>/.runtime/
   runtime-*.json，pid 存活（psutil.pid_exists）的生成只读"外部运行中"
   会话项——gateway 重启后凭文件重新看到还在跑的 agent。

外部会话（gateway 重启恢复的）没有 PTY，无法注入输入/看终端，但状态
照常显示与推送。这是 v1 的已知限制（完整控制要命令收件箱，见计划 V2）。

线程懒启动（仿 get_session_manager 单例模式），web.py 导入时调
get_runtime_watcher() 确保它跑起来。
"""

import json
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import psutil

from gotif import alert, notify
from gateway.core import notifier
from gateway.core.registry import get_agent_registry
from gateway.core.session import get_session_manager
from gateway.core.ttyd_relay import get_ttyd_relay_manager

# agent 上报的合法状态
_VALID_RUNTIME_STATUS = {"starting", "ready", "working", "waiting_input", "exited"}
# 需要推送通知的状态
_NOTIFY_STATUS = {"waiting_input", "exited"}

_POLL_INTERVAL = 1.0

# ---------------------------------------------------------------------------
# 通知主开关（系统面板「通知提醒」开关，持久化到 data/notify.json）
# ---------------------------------------------------------------------------
# 关闭时一律不推任何手机通知；开启后按「你在不在看」判定推送。
_NOTIFY_SETTING_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "notify.json"

_notifications_enabled: Optional[bool] = None  # None = 尚未加载


def get_notifications_enabled() -> bool:
    """通知主开关当前值（默认关闭；面板开启后持久化）。"""
    global _notifications_enabled
    if _notifications_enabled is None:
        try:
            data = json.loads(_NOTIFY_SETTING_PATH.read_text(encoding="utf-8"))
            _notifications_enabled = bool(data.get("enabled", False))
        except (OSError, ValueError):
            _notifications_enabled = False
    return _notifications_enabled


def set_notifications_enabled(v: bool) -> bool:
    """设置并持久化通知主开关。"""
    global _notifications_enabled
    _notifications_enabled = bool(v)
    try:
        _NOTIFY_SETTING_PATH.parent.mkdir(parents=True, exist_ok=True)
        _NOTIFY_SETTING_PATH.write_text(
            json.dumps({"enabled": _notifications_enabled}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass
    return _notifications_enabled

# ---------------------------------------------------------------------------
# 终端输出注意力识别
# ---------------------------------------------------------------------------
# 网关的 ttyd 中继能看到 agent 的全部终端输出。当「输出停止 + 最后一行像
# 输入提示符」时，说明 agent 停下等用户输入/检验 → 推 gotif。
#
# 判定一条"最后一行"是不是等待输入的提示符。已确认各 agent 的提示符格式：
#   emisinver: 你 > / sh > / (y/N)     learnlove: 你 > / 咨询 >
#   wal: You >                          claude: 裸 >       powershell: PS ...>
# 共同的强信号是行尾的 `>` 箭头；再加一批显式等待关键词。
_WAIT_RE = re.compile(
    r">\s*$"                                  # 行尾箭头提示符
    r"|\(\s*[yYnN]\s*/\s*[yYnN]\s*\)"         # (y/N) 确认
    r"|请输入|请确认|请核对|请回复|请选择|请检查|请验证|请提供"
    r"|需要你|等你回复|等待用户|等待指示|等你确认|等你输入"
    r"|输入「?继续」?"
)
# 排除：纯 shell 不是 agent，不参与"等你"提醒
_NO_ATTENTION_AGENTS = {"shell"}


def _read_env(key: str, default: str = "") -> str:
    """读项目根 .env（os.environ 优先）。"""
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
                    # 去掉行内注释（# 后面的说明文字）
                    if "#" in v:
                        v = v.split("#", 1)[0]
                    return v.strip()
    except OSError:
        pass
    return default


_PUBLIC_BASE = _read_env("GATEWAY_PUBLIC_URL", "http://100.104.123.123:8080").rstrip("/")


class RuntimeWatcher:
    def __init__(self):
        self._session_mgr = get_session_manager()
        self._registry = get_agent_registry()
        self._notified: dict[str, str] = {}  # session_id -> 已通知的状态
        self._rediscovered: set[str] = set()  # 已恢复的 runtime 文件路径
        self._stop = threading.Event()
        # ---- 终端输出注意力识别状态（session_id -> 检测状态） ----
        self._attn: dict[str, dict] = {}
        self._attn_quiet = float(_read_env("ATTENTION_QUIET", "8"))
        self._attn_activity = float(_read_env("ATTENTION_ACTIVITY", "15"))
        self._attn_grace = float(_read_env("ATTENTION_GRACE", "10"))

    # ---- 对外 ----

    def start(self):
        if not getattr(self, "_thread", None) or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()

    # ---- 主循环 ----

    def _loop(self):
        first = True
        while not self._stop.is_set():
            try:
                if first:
                    self._rediscover_external()
                    first = False
                self._poll()
            except Exception:
                pass
            self._stop.wait(_POLL_INTERVAL)

    # ---- 轮询活跃会话 ----

    def _poll(self):
        sm = self._session_mgr
        now = time.monotonic()
        sessions = list(sm.list_sessions())
        alive_ids = {s.id for s in sessions}
        for session in sessions:
            if session.runtime_file:
                state = self._read_runtime_file(session.runtime_file)
                if state:
                    with sm.lock:
                        self._apply_state(session, state)
            self._check_attention(session, now)
        # 清理已销毁会话的注意力状态
        if self._attn:
            for sid in list(self._attn):
                if sid not in alive_ids:
                    self._attn.pop(sid, None)

    def _apply_state(self, session, state: dict):
        """把 agent 上报的 state 落到 Session，并触发去重通知。"""
        status = state.get("status", "")
        if status not in _VALID_RUNTIME_STATUS:
            status = ""
        session.runtime_status = status
        session.waiting_prompt = state.get("waiting_prompt") or ""
        session.progress = state.get("progress") or ""
        session.runtime_updated = state.get("updated_at") or ""
        session.suggestions = state.get("suggestions") or []
        # 通知（去重：只在该状态首次出现时推一次；重新进入会再推）
        if status in _NOTIFY_STATUS and self._notified.get(session.id) != status:
            self._notified[session.id] = status
            if get_notifications_enabled():
                self._notify(session, status)

    def _notify(self, session, status: str):
        title = ""
        content = ""
        if status == "waiting_input":
            title = f"[Agent 需要你输入] {session.name}"
            content = session.waiting_prompt or "正在等你回复"
            if session.suggestions:
                content += "\n建议: " + " / ".join(session.suggestions)
        elif status == "exited":
            title = f"[Agent 已完成] {session.name}"
            content = f"进程退出 code={session.exit_code}"

        # gotif 直达链接：点通知直接打开该任务的手机终端页
        url = ""
        if session.port:
            url = f"{_PUBLIC_BASE}/term?session={session.id}"
        try:
            if status == "waiting_input":
                alert(title, content, url=url)   # priority 8：铃声+震动+悬浮窗
            else:
                notify(title, content, url=url)  # priority 5
        except Exception:
            pass
        # 旧 notifier 保留（模板为空时 no-op）
        notifier.send(title, content)

    # ---- 终端输出注意力识别（不依赖 agent 上报，靠中继看到的输出） ----

    def _check_attention(self, session, now: float):
        """检测 agent 是否停下等用户输入 / 进程是否已退出，并去重推送。

        判定逻辑（一条会话的"工作周期"只推一次）：
        1. 有浏览器在盯终端页（active_subscribers>0）→ 你自己在看，不打扰。
        2. 输出活动持续 <ATTENTION_ACTIVITY 秒 → 只是启动横幅/短互动，不算干活。
        3. 输出停止 <ATTENTION_QUIET 秒 → 还在工作/思考，不算"在等输入"。
        4. 最后一行不像输入提示符（你 > / (y/N) / 请确认...）→ 不是等待态。
        5. 会话刚建（<ATTENTION_GRACE 秒）→ 启动噪音，跳过。
        """
        if not session.port or session.external:
            return
        if session.agent_key in _NO_ATTENTION_AGENTS:
            return
        relay = get_ttyd_relay_manager().get(session.id)
        if relay is None:
            return

        st = self._attn.setdefault(session.id, {
            "first": 0.0,          # 本轮工作的首次输出时间
            "last": 0.0,           # 最近一次输出时间
            "notified": False,     # 当前工作周期是否已推送过
            "exit_notified": False,
            "created": now,        # 首次观察到的时间（近似会话创建）
        })

        # --- 进程已退出（agent 自己结束）→ 低优提醒 ---
        if relay.exited and not st["exit_notified"] and now - st["created"] > 20:
            st["exit_notified"] = True
            if get_notifications_enabled():
                self._notify_exit(session, relay)
            return

        # --- 等待用户输入 ---
        last = relay.last_output_at
        if not last:
            return
        if st["last"] == 0.0:
            st["first"] = last
            st["last"] = last
        elif last > st["last"]:
            if st["notified"]:
                st["first"] = last  # 上一次已推送 → 新输出开启新一轮工作
                st["notified"] = False
            st["last"] = last

        span = st["last"] - st["first"]
        if span < self._attn_activity:      # 输出活动太短，不算干过活
            return
        if now - last < self._attn_quiet:   # 还在输出，不算"停下等"
            return
        if now - st["created"] < self._attn_grace:
            return

        lines = relay.recent_lines(4)
        if not lines or not _WAIT_RE.search(lines[-1]):
            return
        # agent 自己上报过 waiting_input（runtime 路径），不重复推
        if session.runtime_status == "waiting_input":
            return
        # 通知主开关：关闭就不推（等待态持续存在，开启后下个 tick 会补推）
        if not get_notifications_enabled():
            return
        # 判断"你在不在看"只看手机：手机在前台盯终端页 → 不打扰；
        # 手机切后台 / 离开终端页 / 没开终端页（桌面开着不算）→ 推
        if relay.active_phone_watchers() > 0:
            return
        if st["notified"]:
            return

        st["notified"] = True
        self._notify_waiting(session, lines)

    def _notify_waiting(self, session, lines: list):
        """agent 停在输入提示等用户 → 高优告警（铃声+震动+直达终端）。"""
        ctx = [l.strip() for l in lines if l.strip()][-3:]
        content = "\n".join(ctx) or "agent 停在输入提示，等你回复"
        title = f"[Agent 需要你] {session.name}"
        url = f"{_PUBLIC_BASE}/term?session={session.id}" if session.port else ""
        print(f"[提醒] 等待输入 → {session.name} (session={session.id}) {content[:80]!r}")
        try:
            alert(title, content, url=url)
        except Exception as e:
            print(f"[提醒] gotif 发送失败: {e}")
        notifier.send(title, content)

    def _notify_exit(self, session, relay):
        """agent 进程自行退出 → 低优提醒（不响铃）。"""
        title = f"[Agent 已完成] {session.name}"
        content = "进程已退出"
        if relay.exit_code is not None:
            content += f" code={relay.exit_code}"
        url = f"{_PUBLIC_BASE}/term?session={session.id}" if session.port else ""
        print(f"[提醒] 进程退出 → {session.name} (session={session.id})")
        try:
            notify(title, content, url=url)
        except Exception as e:
            print(f"[提醒] gotif 发送失败: {e}")
        notifier.send(title, content)

    # ---- runtime 文件读取 ----

    @staticmethod
    def _read_runtime_file(path: str) -> Optional[dict]:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None

    # ---- 启动 rediscovery：找回 gateway 重启前还在跑的 agent ----

    def _rediscover_external(self):
        sm = self._session_mgr
        known_files = {
            s.runtime_file for s in sm.list_sessions() if s.runtime_file
        }
        for agent in self._registry.list_agents():
            if not agent.root:
                continue
            runtime_dir = Path(agent.root) / ".runtime"
            if not runtime_dir.is_dir():
                continue
            for f in sorted(runtime_dir.glob("runtime-*.json")):
                if str(f) in known_files or str(f) in self._rediscovered:
                    continue
                self._rediscovered.add(str(f))
                state = self._read_runtime_file(str(f))
                if not state:
                    continue
                pid = state.get("pid")
                if not pid or not psutil.pid_exists(pid):
                    continue
                self._register_external(agent.key, agent.name, f, state)

    def _register_external(self, agent_key: str, name: str, path: Path, state: dict):
        """为 rediscovery 到的存活 agent 生成只读会话项。"""
        from gateway.core.session import Session

        session_id = path.stem[len("runtime-"):]
        # 避免重复（万一并发）
        if session_id in {s.id for s in self._session_mgr.list_sessions()}:
            return
        session = Session(
            id=session_id,
            name=name,
            agent=name,
            user_id="web",
            created_time=datetime.now(),
            status="running",
            agent_key=agent_key,
            project=state.get("project") or None,
            pid=state.get("pid"),
            running=True,
            runtime_file=str(path),
            external=True,
        )
        self._apply_state(session, state)
        with self._session_mgr.lock:
            self._session_mgr._sessions[session_id] = session


# 全局单例
_watcher: Optional[RuntimeWatcher] = None


def get_runtime_watcher() -> RuntimeWatcher:
    """获取全局 RuntimeWatcher 单例并确保线程在跑。"""
    global _watcher
    if _watcher is None:
        _watcher = RuntimeWatcher()
        _watcher.start()
    return _watcher
