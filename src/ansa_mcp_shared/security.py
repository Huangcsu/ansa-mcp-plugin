"""Windows ACL verification used independently by both runtime ends."""

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import re
import subprocess


def _run(*args):
    # whoami/icacls emit a Windows code page even when Python uses UTF-8 mode.
    # SID matching is ASCII; localized diagnostics must not abort ACL checks.
    result = subprocess.run(args, capture_output=True, timeout=20)
    encoding = "oem" if os.name == "nt" else "utf-8"
    stdout = result.stdout.decode(encoding, errors="replace")
    stderr = result.stderr.decode(encoding, errors="replace")
    if result.returncode:
        raise PermissionError("Cannot secure runtime ACL: " + (stderr or stdout).strip())
    return stdout


def _current_sid():
    match = re.search(r"S-1-5-\d+(?:-\d+)+", _run("whoami", "/user"))
    if not match:
        raise PermissionError("Cannot identify the current Windows account")
    return match.group()


def _acl(path):
    if os.name != "nt":
        raise OSError("Windows ACL enforcement is required")
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi.GetNamedSecurityInfoW.argtypes = (wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
    advapi.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi.ConvertSidToStringSidW.argtypes = (ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR))
    advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
    advapi.GetAce.argtypes = (ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p))
    advapi.GetAce.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = (ctypes.c_void_p,)
    kernel.LocalFree.restype = ctypes.c_void_p
    owner, dacl, descriptor = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    result = advapi.GetNamedSecurityInfoW(str(path), 1, 0x5, ctypes.byref(owner), None,
                                        ctypes.byref(dacl), None, ctypes.byref(descriptor))
    if result:
        raise OSError(result, "Cannot read runtime ACL: " + str(path))

    def sid_string(pointer):
        value = wintypes.LPWSTR()
        if not advapi.ConvertSidToStringSidW(pointer, ctypes.byref(value)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return value.value
        finally:
            kernel.LocalFree(ctypes.cast(value, ctypes.c_void_p))

    try:
        if not dacl.value:
            raise PermissionError("Runtime has an unrestricted DACL: " + str(path))
        count = ctypes.c_uint16.from_address(dacl.value + 4).value
        entries = []
        for index in range(count):
            ace = ctypes.c_void_p()
            if not advapi.GetAce(dacl, index, ctypes.byref(ace)):
                raise ctypes.WinError(ctypes.get_last_error())
            kind = ctypes.c_ubyte.from_address(ace.value).value
            flags = ctypes.c_ubyte.from_address(ace.value + 1).value
            if kind not in (0, 1):
                raise PermissionError("Unsupported runtime ACL entry: " + str(path))
            mask = ctypes.c_uint32.from_address(ace.value + 4).value
            entries.append((kind, flags, mask, sid_string(ace.value + 8)))
        return sid_string(owner), entries
    finally:
        kernel.LocalFree(descriptor)


def reject_reparse_path(path):
    path = Path(os.path.abspath(path))
    for entry in (path, *path.parents):
        if entry.exists() and entry.stat(follow_symlinks=False).st_file_attributes & 0x400:
            raise PermissionError("Runtime path contains a reparse point: " + str(entry))


def ensure_safe_parent(path):
    reject_reparse_path(path)
    allowed = {_current_sid(), "S-1-5-18", "S-1-5-32-544",
               "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"}  # TrustedInstaller
    for parent in Path(path).absolute().parents:
        if not parent.exists():
            continue
        owner, entries = _acl(parent)
        if owner not in allowed:
            raise PermissionError("Runtime ancestor has an untrusted owner: " + str(parent))
        for kind, flags, mask, sid in entries:
            if kind != 0 or flags & 0x8 or sid in allowed:
                continue
            if mask & (0x10000 | 0x40 | 0x40000 | 0x80000 | 0x40000000 | 0x10000000):
                raise PermissionError("Runtime ancestor permits replacement of state: " + str(parent))


def verify_private_path(path):
    reject_reparse_path(path)
    sid = _current_sid()
    allowed = {sid, "S-1-5-18", "S-1-5-32-544"}
    owner, entries = _acl(path)
    if owner != sid or not entries:
        raise PermissionError("Runtime owner or ACL is invalid: " + str(path))
    granted = set()
    for kind, flags, mask, principal in entries:
        if kind != 0 or principal not in allowed or mask & 0x1F01FF != 0x1F01FF or flags & 0x8:
            raise PermissionError("Runtime contains an untrusted or insufficient ACL: " + str(path))
        granted.add(principal)
    if granted != allowed:
        raise PermissionError("Runtime ACL omits a required account: " + str(path))


def ensure_private_directory(path):
    reject_reparse_path(path)
    path = Path(path).absolute()
    path.mkdir(parents=True, exist_ok=True)
    sid = _current_sid()
    _run("icacls", str(path), "/inheritance:r")
    _run("icacls", str(path), "/grant:r", "*" + sid + ":(OI)(CI)F",
         "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F")
    _run("icacls", str(path), "/setowner", "*" + sid)
    verify_private_path(path)


def verify_runtime(runtime_home):
    ensure_safe_parent(runtime_home)
    for relative in ("", "config", "instances", "config/token.txt"):
        verify_private_path(Path(runtime_home) / relative)
