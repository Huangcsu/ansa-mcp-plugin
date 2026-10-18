"""Windowless process-thread timer; never binds to a disposable GUI handle."""

import ctypes
from ctypes import wintypes
import threading


class MainThreadTimer:
    def __init__(self, callback, interval_ms=100):
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError("ANSA executor must start on the main thread")
        self._callback = callback
        self._busy = False
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._proc_type = ctypes.WINFUNCTYPE(None, wintypes.HWND, wintypes.UINT, ctypes.c_size_t, wintypes.DWORD)
        self._user32.SetTimer.argtypes = (wintypes.HWND, ctypes.c_size_t, wintypes.UINT, self._proc_type)
        self._user32.SetTimer.restype = ctypes.c_size_t
        self._user32.KillTimer.argtypes = (wintypes.HWND, ctypes.c_size_t)
        self._user32.KillTimer.restype = wintypes.BOOL
        self._proc = self._proc_type(self._tick)
        # Windows 不持有 Python 引用，KillTimer 也不会清除已排队的消息。
        # ANSA 会在原生脚本退出时清理脚本引用；模块热重载同样可能使旧回调被 GC。
        # 固定在不会随开发包重载的 ctypes 模块中，保证 C 回调地址到进程退出都有效。
        retained = getattr(ctypes, "_ansa_mcp_plugin_timer_callbacks", None)
        if retained is None:
            retained = []
            ctypes._ansa_mcp_plugin_timer_callbacks = retained
        retained.append(self._proc)
        self._timer_id = self._user32.SetTimer(None, 0, interval_ms, self._proc)
        if not self._timer_id:
            raise OSError(ctypes.get_last_error(), "SetTimer failed")

    def _tick(self, hwnd, message, timer_id, tick):
        if not self._timer_id or timer_id != self._timer_id or self._busy:
            return
        self._busy = True
        try:
            self._callback()
        finally:
            self._busy = False

    def close(self):
        if self._timer_id:
            if not self._user32.KillTimer(None, self._timer_id):
                raise OSError(ctypes.get_last_error(), "Cannot confirm executor timer stopped")
            self._timer_id = 0
            self._callback = None
