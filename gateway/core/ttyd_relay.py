"""ttyd 会话中继 —— gateway 作为常驻 WS 客户端，每个 agent 会话一条。

背景（2026-08-10 实测）：ttyd 1.7.6 在 Windows 上「每个 WS 客户端创建
**独立**会话 + 独立子进程，客户端断开会话即销毁」——不是共享终端。所以
不能让浏览器直连 ttyd，否则刷新/断线/多端会各自为政、agent 还会随断线而死。

本模块：gateway 在创建 session 后立即持有一条**常驻 WS 中继**连到 ttyd：
- 保持 ttyd 会话存活 → agent 后台常驻（浏览器全关也不死）
- 接收 agent 全部输出 → 增量 UTF-8 解码成文本 → 缓冲 + 广播给浏览器
- 浏览器输入 / resize → 经中继转发给 ttyd

浏览器连的是 gateway 的 /api/ws/session/{id}（见 web.py），不直连 ttyd。
中继协议即 ttyd 二进制帧协议，见 memory/ttyd-ws-protocol。
"""

import asyncio
import codecs
import json
import re
import threading
import time
from typing import Callable, Optional

import websockets

# 重放给新浏览器的最近输出上限（字符）。覆盖启动横幅 + 近期滚屏，足够看到
# 当前状态，又不至于一次发太多。
_REPLAY_CHARS = 256 * 1024

# ANSI 转义清洗（给注意力识别的去 ANSI 行读取用）
_ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"
    r"|\x1b\][^\x07]*(\x07|\x1b\\)"
    r"|\x1b[@-Z\\-_]"
)

_INPUT_TYPE = 0x30   # '0' = INPUT
_RESIZE_TYPE = 0x31  # '1' = RESIZE_TERMINAL


class _Subscriber:
    """一个浏览器订阅者：独立队列 + 发送任务，慢客户端不阻塞中继读取。

    中继 reader 只把输出文本 put_nowait 进队列（非阻塞），发送任务单独
    按顺序 drain 并回调浏览器。队列积压太多时合并成一个大块发送，既保内容
    又不失控。
    """

    _MAX_QUEUE = 64

    def __init__(self, cb: Callable):
        self.cb = cb
        self.queue: asyncio.Queue = asyncio.Queue()
        self.task = asyncio.create_task(self._run())

    async def _run(self):
        while True:
            item = await self.queue.get()
            if item is None:
                break
            # 队列积压：合并 pending 项成一块（保顺序保内容，减少往返）
            try:
                while self.queue.qsize() > 0 and self.queue.qsize() < self._MAX_QUEUE:
                    item += await self.queue.get()
            except Exception:
                pass
            try:
                await self.cb(item)
            except Exception:
                pass

    def push(self, text):
        self.queue.put_nowait(text)

    def close(self):
        self.queue.put_nowait(None)


class TtydRelay:
    """一个 agent 会话的 ttyd 常驻中继。"""

    def __init__(self, session_id: str, port: int):
        self.session_id = session_id
        self.port = port
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._buf = ""            # 已解码输出文本（可能裁剪到 _REPLAY_CHARS）
        self._subs: list[_Subscriber] = []
        self.running = False
        self.exited = False
        self.exit_code: Optional[int] = None
        self.last_output_at = 0.0  # 最近一次收到输出的时间戳（time.monotonic，0=还没输出）
        self._reader_task: Optional[asyncio.Task] = None
        self._start_lock = asyncio.Lock()

    # ---- 生命周期 ----

    async def start(self):
        """连接 ttyd 并开始接收（幂等）。失败则标记 exited。"""
        if self.running or self.exited:
            return
        async with self._start_lock:
            if self.running or self.exited:
                return
            try:
                self._ws = await asyncio.wait_for(
                    websockets.connect(
                        f"ws://127.0.0.1:{self.port}/ws", subprotocols=["tty"]
                    ),
                    timeout=10,
                )
            except Exception:
                self.exited = True
                return
            # init 帧（第一帧，无前缀 JSON）
            init = json.dumps({"AuthToken": "", "columns": 100, "rows": 30})
            await self._ws.send(init.encode("utf-8"))
            self.running = True
            self._reader_task = asyncio.create_task(self._read_loop())

    async def _read_loop(self):
        try:
            while True:
                raw = await self._ws.recv()
                if not raw:
                    continue
                typ = raw[0:1]
                if typ == b"0":  # 输出
                    text = self._decoder.decode(raw[1:])
                    if text:
                        self.last_output_at = time.monotonic()
                        self._buf += text
                        if len(self._buf) > _REPLAY_CHARS * 3:
                            self._buf = self._buf[-_REPLAY_CHARS:]
                        # 非阻塞推给各订阅者（慢端不卡中继）
                        for sub in list(self._subs):
                            try:
                                sub.push(text)
                            except Exception:
                                pass
        except Exception:
            pass
        finally:
            self.running = False
            self.exited = True
            # 通知订阅者进程已退出（None 信号）
            for sub in list(self._subs):
                try:
                    sub.push(None)
                except Exception:
                    pass

    async def close(self):
        self.exited = True
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
        if self._reader_task is not None:
            self._reader_task.cancel()

    # ---- 订阅（浏览器客户端） ----

    def subscribe(self, cb: Callable):
        """注册浏览器订阅者回调 cb(text)；text 为 None 表示进程退出。"""
        self._subs.append(_Subscriber(cb))

    def unsubscribe(self, cb: Callable):
        for sub in list(self._subs):
            if sub.cb is cb:
                self._subs.remove(sub)
                sub.close()
                break

    def recent(self) -> str:
        """返回最近 _REPLAY_CHARS 字符（给新客户端重放）。"""
        return self._buf[-_REPLAY_CHARS:]

    # ---- 注意力识别（runtime_watch 轮询用，跨线程读 _buf 只读安全） ----

    def active_subscribers(self) -> int:
        """当前在盯这个终端页的浏览器订阅者数量。0 = 没人看。"""
        return len(self._subs)

    def recent_lines(self, n: int = 5) -> list:
        """返回最近 n 行去 ANSI 的可见文本（供「等待输入」识别）。

        取缓冲尾部最近 4KB 清洗后切行——提示符一定是最后输出的那几行，
        不会被更早的大块日志顶掉。
        """
        buf = self._buf[-4096:]
        clean = _ANSI_RE.sub("", buf)
        lines = clean.splitlines()
        if not lines:
            return []
        return lines[-n:]

    # ---- 转发 ----

    async def send_input(self, data: bytes) -> bool:
        if not self.running or self._ws is None:
            return False
        frame = bytes([_INPUT_TYPE]) + data
        try:
            await self._ws.send(frame)
            return True
        except Exception:
            return False

    async def send_resize(self, cols: int, rows: int):
        if not self.running or self._ws is None:
            return
        payload = json.dumps({"columns": cols, "rows": rows}).encode("utf-8")
        try:
            await self._ws.send(bytes([_RESIZE_TYPE]) + payload)
        except Exception:
            pass


class TtydRelayManager:
    """session_id → TtydRelay。"""

    def __init__(self):
        self._relays: dict[str, TtydRelay] = {}
        self._lock = threading.Lock()

    def get(self, session_id: str) -> Optional[TtydRelay]:
        return self._relays.get(session_id)

    def get_or_create(self, session_id: str, port: int) -> TtydRelay:
        with self._lock:
            relay = self._relays.get(session_id)
            if relay is None:
                relay = TtydRelay(session_id, port)
                self._relays[session_id] = relay
            return relay

    def drop(self, session_id: str):
        """会话销毁/停止时移除中继并关闭连接。"""
        with self._lock:
            relay = self._relays.pop(session_id, None)
        if relay is not None:
            try:
                asyncio.get_running_loop().create_task(relay.close())
            except RuntimeError:
                pass


# 全局单例
_relay_manager: Optional[TtydRelayManager] = None


def get_ttyd_relay_manager() -> TtydRelayManager:
    global _relay_manager
    if _relay_manager is None:
        _relay_manager = TtydRelayManager()
    return _relay_manager
