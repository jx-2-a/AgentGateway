# -*- coding: utf-8 -*-
"""Agent 子进程的 VT 模式引导（由 gateway 通过 PYTHONPATH 注入）。

为什么需要它：
- 终端用 WinPTY backend（ConPTY 在 pywinpty 3.0.5 下丢输入，实测发 5 条收 0 条）。
- WinPTY 的 agent 会把子进程"按字面写进控制台缓冲区"的 ESC 字符（0x1B）
  转成 '?'，导致 \x1b[31m 变成 ?[31m —— xterm.js 端把它当普通文本显示，
  终端里就出现一堆乱码 + 排版错乱。
- 提前开启 ENABLE_VIRTUAL_TERMINAL_PROCESSING 后，conhost 会消费这些
  转义序列更新屏幕缓冲区，winpty-agent 再按缓冲区属性输出干净的
  \x1b[0;31m 这类序列，不再有 '?' 垃圾。
- rich / prompt_toolkit / click 等库自己也会开 VT，本文件对它们无副作用；
  只兜住"裸 print + ANSI 转义"这种不开 VT 的输出路径。

在解释器启动早期（site 导入时）执行，只做一次 console mode 设置，失败静默。
"""
import ctypes

_ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004


def _enable_vt():
    try:
        k32 = ctypes.windll.kernel32
    except Exception:
        return
    for std_handle in (-11, -12, -10):  # stdout, stderr, stdin
        try:
            handle = k32.GetStdHandle(std_handle)
            if not handle or handle in (-1, 0):
                continue
            mode = ctypes.c_uint32(0)
            if not k32.GetConsoleMode(handle, ctypes.byref(mode)):
                continue
            k32.SetConsoleMode(handle, mode.value | _ENABLE_VIRTUAL_TERMINAL_PROCESSING)
        except Exception:
            continue


_enable_vt()
