"""只读检查当前 ANSA 进程的前台窗口是否被原生模态框占用。"""

import ctypes
import os


def _foreground_window_info():
    win_dll = getattr(ctypes, "WinDLL", None)
    if win_dll is None:
        return None
    from ctypes import wintypes

    user32 = win_dll("user32", use_last_error=True)
    user32.GetForegroundWindow.argtypes = ()
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = (
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    )
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    user32.GetClassNameW.restype = ctypes.c_int

    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    pid = wintypes.DWORD()
    if not user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)):
        return None
    title = ctypes.create_unicode_buffer(512)
    class_name = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(hwnd, title, len(title))
    user32.GetClassNameW(hwnd, class_name, len(class_name))
    return pid.value, title.value, class_name.value


def _is_ansa_window(title, class_name):
    """只把明确的 ANSA 主窗口或插件面板视为可执行窗口。"""
    title = title.strip().casefold()
    class_name = class_name.strip().casefold()
    if title in {"untitled", "untitled.ansa"}:
        return True
    return (
        "ansa" in title
        or "beta cae" in title
        or title.endswith(".ansa")
        or "ansa" in class_name
    )


def foreground_modal_busy():
    """前台属于当前进程且窗口身份不明确时保守判定为 busy。"""
    try:
        info = _foreground_window_info()
        if info is None:
            return False
        pid, title, class_name = info
        if pid != os.getpid():
            return False
        if class_name.strip().casefold() == "#32770":
            return True
        if not title.strip():
            return False
        return not _is_ansa_window(title, class_name)
    except Exception:
        # 无 Win32 能力或探测失败时放行，不能中断主线程定时器。
        return False
