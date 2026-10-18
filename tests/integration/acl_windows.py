"""Verify OS denial with a restricted token; never read or modify secret data."""

import argparse
import ctypes
from ctypes import wintypes
import json
from pathlib import Path

from ansa_mcp_plugin.backend.service import Service
from ansa_mcp_shared.security import verify_runtime


class SidAndAttributes(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


def restricted_access_checks(runtime_home):
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.LocalFree.argtypes = (ctypes.c_void_p,)
    kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    kernel.CreateFileW.restype = wintypes.HANDLE
    advapi.OpenProcessToken.argtypes = (wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE))
    advapi.ConvertStringSidToSidW.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p))
    advapi.CreateRestrictedToken.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(SidAndAttributes), ctypes.POINTER(wintypes.HANDLE))
    advapi.ImpersonateLoggedOnUser.argtypes = (wintypes.HANDLE,)
    token, restricted, sid = wintypes.HANDLE(), wintypes.HANDLE(), ctypes.c_void_p()
    checks = []
    try:
        if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0xA, ctypes.byref(token)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not advapi.ConvertStringSidToSidW("S-1-5-19", ctypes.byref(sid)):
            raise ctypes.WinError(ctypes.get_last_error())
        restriction = SidAndAttributes(sid, 0)
        if not advapi.CreateRestrictedToken(token, 1, 0, None, 0, None, 1,
                                             ctypes.byref(restriction), ctypes.byref(restricted)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not advapi.ImpersonateLoggedOnUser(restricted):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            for label, path, access, flags in (
                ("token_read", runtime_home / "config/token.txt", 0x80000000, 0),
                ("state_delete", runtime_home / "state.sqlite3", 0x10000, 0),
                ("parent_delete_child", runtime_home, 0x40, 0x02000000),
            ):
                handle = kernel.CreateFileW(str(path), access, 7, None, 3, flags, None)
                if handle != ctypes.c_void_p(-1).value:
                    kernel.CloseHandle(handle)
                    raise AssertionError("Restricted identity unexpectedly obtained " + label)
                error = ctypes.get_last_error()
                assert error == 5, (label, error)
                checks.append({"check": label, "denied": True, "winerror": error})
        finally:
            if not advapi.RevertToSelf():
                raise ctypes.WinError(ctypes.get_last_error())
    finally:
        for handle in (restricted, token):
            if handle:
                kernel.CloseHandle(handle)
        if sid:
            kernel.LocalFree(sid)
    return checks


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-home", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    service = Service(args.runtime_home)
    service.close()
    verify_runtime(args.runtime_home)
    result = {"ok": True, "acl_verified": True,
              "identity_test": "Windows restricted token with an ungranted restricting SID",
              "checks": restricted_access_checks(args.runtime_home)}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
