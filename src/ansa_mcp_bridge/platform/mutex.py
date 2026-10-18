"""Single process executor owner even when runtimes differ."""

import ctypes
from ctypes import wintypes
import os


class ProcessMutex:
    def __init__(self):
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
        kernel.CreateMutexW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.ReleaseMutex.argtypes = (wintypes.HANDLE,)
        self._kernel = kernel
        self._handle = kernel.CreateMutexW(None, True, "Local\\ANSA_MCP_PLUGIN_EXECUTOR_" + str(os.getpid()))
        if not self._handle:
            raise OSError(ctypes.get_last_error(), "CreateMutexW failed")
        if ctypes.get_last_error() == 183:
            kernel.CloseHandle(self._handle)
            self._handle = None
            raise RuntimeError("ANSA process already has a plugin executor")

    def close(self):
        if self._handle:
            self._kernel.ReleaseMutex(self._handle)
            self._kernel.CloseHandle(self._handle)
            self._handle = None
