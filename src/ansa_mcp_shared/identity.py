"""Windows process creation identity, independent of PID reuse and runtime directory."""

import ctypes
from ctypes import wintypes
import os


def process_start_filetime(pid=None):
    if os.name != "nt":
        raise OSError("The first release supports Windows only")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = (wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.OpenProcess(0x1000, False, os.getpid() if pid is None else pid)
    if not handle:
        raise OSError(ctypes.get_last_error(), "Cannot inspect target process")
    try:
        creation = ctypes.c_uint64()
        exit_time = ctypes.c_uint64()
        kernel_time = ctypes.c_uint64()
        user_time = ctypes.c_uint64()
        if not kernel.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exit_time), ctypes.byref(kernel_time), ctypes.byref(user_time)):
            raise OSError(ctypes.get_last_error(), "Cannot read process start time")
        return str(creation.value)
    finally:
        kernel.CloseHandle(handle)


def same_process_alive(pid, process_start):
    """Return false only on verified exit/PID reuse; access failures remain errors."""
    if not isinstance(pid, int) or not process_start:
        raise ValueError("Persisted process identity is missing")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        if error == 87:  # ERROR_INVALID_PARAMETER: PID no longer exists
            return False
        raise OSError(error, "Cannot verify old ANSA process exit")
    try:
        code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
            raise ctypes.WinError(ctypes.get_last_error())
        return code.value == 259 and process_start_filetime(pid) == process_start
    finally:
        kernel.CloseHandle(handle)
