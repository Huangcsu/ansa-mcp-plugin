"""Authenticated client for the runtime-wide backend daemon."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import errno
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from typing import Any

from ansa_mcp_shared.identity import process_start_filetime, same_process_alive

from .memory import MemoryConflictError
from .scheduler import ContextMismatchError, TaskNotFoundError


_PROTOCOL = "ansa-mcp-plugin-rpc/1"
_BUILD = "0.1.3"
_MAX_FRAME_BYTES = 16 * 1024 * 1024
_ENDPOINT_NAME = "backend.endpoint.json"
_STARTUP_LOCK_NAME = "backend.start.lock"
_STARTING_NAME = "backend.starting.json"
_TOKEN_NAME = Path("config") / "token.txt"
_STARTUP_WAIT_SECONDS = 8.0
_STARTUP_LOCK_WAIT_SECONDS = 8.0
_RPC_TIMEOUT_SECONDS = 60.0
_ERROR_CODES = frozenset(("STARTING", "BUSY", "OFFLINE", "AUTH"))


class BackendUnavailableError(RuntimeError):
    """A shared daemon could not be reached or authenticated."""

    def __init__(self, code: str, message: str):
        if code not in _ERROR_CODES:
            raise ValueError("Unsupported backend availability code")
        super().__init__(message)
        self.code = code


class BackendOutcomeUnknownError(BackendUnavailableError):
    """A request may have reached the daemon but has no trustworthy reply."""

    def __init__(self, message: str, code: str = "OFFLINE"):
        super().__init__(code, message)


class RemoteBackendError(RuntimeError):
    """An exception type not in the RPC's explicit compatibility map."""

    def __init__(self, remote_type: str, message: str):
        super().__init__(message)
        self.remote_type = remote_type


def _reject_constant(value: str):
    raise ValueError("Non-finite JSON number: " + value)


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = sock.recv(size - len(chunks))
        if not chunk:
            raise EOFError("TCP connection closed inside a frame")
        chunks.extend(chunk)
    return bytes(chunks)


def _recv_frame(sock: socket.socket) -> dict[str, Any]:
    length = struct.unpack("!I", _recv_exact(sock, 4))[0]
    if length == 0 or length > _MAX_FRAME_BYTES:
        raise ValueError("Invalid shared backend frame length")
    value = json.loads(_recv_exact(sock, length).decode("utf-8"), parse_constant=_reject_constant)
    if not isinstance(value, dict):
        raise ValueError("Shared backend frame must contain a JSON object")
    return value


def _encode_frame(value: dict[str, Any]) -> bytes:
    data = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if not data or len(data) > _MAX_FRAME_BYTES:
        raise ValueError("Shared backend frame exceeds 16 MiB")
    return struct.pack("!I", len(data)) + data


def _send_frame(sock: socket.socket, value: dict[str, Any]) -> None:
    sock.sendall(_encode_frame(value))


def _remote_exception_types():
    from .lock import BackendBusyError

    return {
        "TaskNotFoundError": TaskNotFoundError,
        "MemoryConflictError": MemoryConflictError,
        "ContextMismatchError": ContextMismatchError,
        "BackendBusyError": BackendBusyError,
        "ValueError": ValueError,
        "TypeError": TypeError,
        "LookupError": LookupError,
        "KeyError": KeyError,
        "IndexError": IndexError,
        "OverflowError": OverflowError,
        "RuntimeError": RuntimeError,
        "OSError": OSError,
        "PermissionError": PermissionError,
        "ConnectionError": ConnectionError,
        "TimeoutError": TimeoutError,
    }


def _restore_error(value: Any) -> BaseException:
    if not isinstance(value, dict):
        return RemoteBackendError("RemoteError", "The backend returned an invalid error")
    remote_type = value.get("type")
    message = value.get("message")
    if not isinstance(remote_type, str) or not isinstance(message, str):
        return RemoteBackendError("RemoteError", "The backend returned an invalid error")
    if remote_type == "BackendUnavailableError":
        code = value.get("code")
        if code in _ERROR_CODES:
            return BackendUnavailableError(code, message)
    error_type = _remote_exception_types().get(remote_type)
    if error_type is not None:
        return error_type(message)
    return RemoteBackendError(remote_type, message)


def _error_payload(exc: BaseException, token: str | None = None) -> dict[str, Any]:
    message = str(exc) or type(exc).__name__
    if token:
        message = message.replace(token, "<redacted>")
    message = message[:4096]
    value = {"type": type(exc).__name__, "message": message}
    if isinstance(exc, BackendUnavailableError):
        value["code"] = exc.code
    return value


def _request(endpoint: dict[str, Any], token: str, method: str, args=(), kwargs=None,
             timeout: float = _RPC_TIMEOUT_SECONDS, protocol: str = _PROTOCOL):
    """Send one request on one socket. An uncertain result is never replayed."""
    request = {
        "protocol": protocol,
        "token": token,
        "method": method,
        "args": list(args),
        "kwargs": {} if kwargs is None else kwargs,
    }
    # Validate before opening the socket so local input errors are known not sent.
    frame = _encode_frame(request)
    may_have_sent = False
    try:
        with socket.create_connection(("127.0.0.1", endpoint["port"]), timeout=timeout) as conn:
            conn.settimeout(timeout)
            # sendall can fail after sending some or all of the request.
            may_have_sent = True
            conn.sendall(frame)
            response = _recv_frame(conn)
    except (OSError, EOFError, TimeoutError, ValueError) as exc:
        if may_have_sent:
            raise BackendOutcomeUnknownError("Shared backend reply unavailable: " + str(exc)) from exc
        raise BackendUnavailableError("OFFLINE", "Shared backend request failed: " + str(exc)) from exc

    if response.get("protocol") != _PROTOCOL:
        raise BackendOutcomeUnknownError("Shared backend protocol mismatch", code="AUTH")
    if response.get("ok") is True:
        if set(response) != {"protocol", "ok", "result"}:
            raise BackendOutcomeUnknownError("Shared backend response is malformed", code="AUTH")
        return response["result"]
    if response.get("ok") is False and set(response) == {"protocol", "ok", "error"}:
        error = response["error"]
        if isinstance(error, dict) and isinstance(error.get("type"), str) and isinstance(error.get("message"), str):
            raise _restore_error(error)
    raise BackendOutcomeUnknownError("Shared backend response is malformed", code="AUTH")


def _endpoint_path(runtime_home: Path) -> Path:
    return runtime_home / _ENDPOINT_NAME


def _starting_path(runtime_home: Path) -> Path:
    return runtime_home / _STARTING_NAME


def _load_private_json(path: Path, maximum: int = 65536):
    from .security import verify_private_path

    if not os.path.lexists(path):
        return None
    verify_private_path(path)
    with path.open("rb") as stream:
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise PermissionError("Private backend metadata exceeds its size limit")
    value = json.loads(data.decode("utf-8"), parse_constant=_reject_constant)
    if not isinstance(value, dict):
        raise ValueError("Private backend metadata must be a JSON object")
    verify_private_path(path)
    return value


def _process_is_live(pid: int, expected_start: str) -> bool:
    try:
        current_start = process_start_filetime(pid)
    except OSError as exc:
        if getattr(exc, "winerror", None) in (87, 1168) or exc.errno in (87, 1168, errno.ESRCH):
            return False
        raise BackendUnavailableError("AUTH", "Cannot verify backend process identity") from exc
    if current_start != expected_start:
        # A reused PID makes metadata stale; never send it the runtime token.
        # Starting a replacement still requires the independent backend lock.
        return False
    return same_process_alive(pid, expected_start)


def _read_endpoint(runtime_home: Path):
    path = _endpoint_path(runtime_home)
    try:
        value = _load_private_json(path)
    except (OSError, ValueError, TypeError) as exc:
        raise BackendUnavailableError("AUTH", "Backend endpoint failed private validation") from exc
    if value is None:
        return None
    required = {"pid", "process_start", "backend_id", "port", "protocol", "build"}
    if not required.issubset(value):
        raise BackendUnavailableError("AUTH", "Backend endpoint is incomplete")
    if (type(value["pid"]) is not int or value["pid"] <= 0
            or type(value["port"]) is not int or not 1 <= value["port"] <= 65535
            or not isinstance(value["process_start"], str) or not value["process_start"]
            or not isinstance(value["backend_id"], str) or not value["backend_id"]
            or not isinstance(value["build"], str) or not value["build"]):
        raise BackendUnavailableError("AUTH", "Backend endpoint fields are invalid")
    if value["protocol"] != _PROTOCOL:
        raise BackendUnavailableError("AUTH", "Backend endpoint protocol mismatch")
    if not _process_is_live(value["pid"], value["process_start"]):
        return None
    return value


def _read_starting(runtime_home: Path):
    path = _starting_path(runtime_home)
    try:
        value = _load_private_json(path)
    except (OSError, ValueError, TypeError) as exc:
        raise BackendUnavailableError("AUTH", "Backend startup marker failed private validation") from exc
    if value is None:
        return None
    pid, process_start = value.get("pid"), value.get("process_start")
    if type(pid) is not int or pid <= 0 or not isinstance(process_start, str) or not process_start:
        raise BackendUnavailableError("AUTH", "Backend startup marker is invalid")
    return value if _process_is_live(pid, process_start) else None


def _read_token(runtime_home: Path) -> str:
    from .security import verify_private_path

    path = runtime_home / _TOKEN_NAME
    try:
        verify_private_path(path)
        token = path.read_text(encoding="ascii").strip()
        verify_private_path(path)
    except (OSError, UnicodeError) as exc:
        raise BackendUnavailableError("AUTH", "Runtime authentication token failed private validation") from exc
    if len(token) != 64 or any(char not in "0123456789abcdef" for char in token):
        raise BackendUnavailableError("AUTH", "Runtime authentication token is invalid")
    return token


def _write_private_json(path: Path, value: dict[str, Any]) -> None:
    from .security import ensure_private_directory, ensure_safe_parent, verify_private_path

    ensure_safe_parent(path.parent)
    ensure_private_directory(path.parent)
    if os.path.lexists(path):
        verify_private_path(path)
    data = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            verify_private_path(temporary)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        verify_private_path(path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _append_startup_log(runtime_home: Path, message: str, token: str | None = None) -> None:
    from .security import ensure_private_directory, ensure_safe_parent, verify_private_path

    if token:
        message = message.replace(token, "<redacted>")
    log_dir = runtime_home / "logs"
    ensure_safe_parent(log_dir)
    ensure_private_directory(log_dir)
    log_path = log_dir / "backend-startup.log"
    if os.path.lexists(log_path):
        verify_private_path(log_path)
    stamp = datetime.now(timezone.utc).isoformat()
    with log_path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(f"[{stamp}] {message.rstrip()}\n")
        stream.flush()
        os.fsync(stream.fileno())
    verify_private_path(log_path)


@contextmanager
def _startup_lock(runtime_home: Path, is_closed=lambda: False):
    from .security import verify_private_path

    path = runtime_home / _STARTUP_LOCK_NAME
    stream = path.open("a+b")
    acquired = False
    try:
        verify_private_path(path)
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        deadline = time.monotonic() + _STARTUP_LOCK_WAIT_SECONDS
        if os.name == "nt":
            import msvcrt

            while True:
                if is_closed():
                    raise RuntimeError("MCP backend client is closed")
                stream.seek(0)
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    acquired = True
                    break
                except OSError as exc:
                    if (exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK)
                            and getattr(exc, "winerror", None) != 33):
                        raise
                    if time.monotonic() >= deadline:
                        raise BackendUnavailableError("STARTING", "Another frontend is starting the shared backend")
                    time.sleep(0.05)
        else:
            import fcntl

            while True:
                if is_closed():
                    raise RuntimeError("MCP backend client is closed")
                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise BackendUnavailableError("STARTING", "Another frontend is starting the shared backend")
                    time.sleep(0.05)
        yield
    finally:
        if acquired:
            if os.name == "nt":
                import msvcrt

                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()


def _backend_lock_busy(runtime_home: Path) -> bool:
    from .lock import BackendBusyError, BackendLock
    from .security import verify_private_path

    path = runtime_home / "backend.lock"
    if os.path.lexists(path):
        verify_private_path(path)
    try:
        lock = BackendLock(path)
    except BackendBusyError:
        verify_private_path(path)
        return True
    try:
        verify_private_path(path)
    finally:
        lock.close()
    return False


def _spawn_daemon(runtime_home: Path):
    options = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
               "stderr": subprocess.DEVNULL, "close_fds": True}
    package_root = str(Path(__file__).resolve().parents[2])
    bootstrap = ("import sys;sys.path.insert(0," + repr(package_root) + ");"
                 "from ansa_mcp_plugin.backend.daemon import main;raise SystemExit(main())")
    command = [sys.executable, "-B", "-c", bootstrap, "--runtime-home", str(runtime_home)]
    if os.name == "nt":
        # MCP clients place stdio processes in a kill-on-close Windows Job.
        # WMI launches under the calling user's identity outside that Job.
        # A detached Popen alone would still die when the last frontend closes.
        launch_script = (
            "$ErrorActionPreference='Stop';"
            "$startup=New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly "
            "-Property @{ShowWindow=[uint16]0};"
            "$r=Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
            "-Arguments @{CommandLine=$env:ANSA_MCP_DAEMON_COMMAND;"
            "CurrentDirectory=$env:ANSA_MCP_DAEMON_CWD;ProcessStartupInformation=$startup};"
            "$r | Select-Object ReturnValue,ProcessId | ConvertTo-Json -Compress"
        )
        env = dict(os.environ, ANSA_MCP_DAEMON_COMMAND=subprocess.list2cmdline(command),
                   ANSA_MCP_DAEMON_CWD=str(runtime_home))
        powershell = str(Path(os.environ.get('SystemRoot', r'C:\Windows')) /
                         'System32/WindowsPowerShell/v1.0/powershell.exe')
        launched = subprocess.run([powershell, '-NoProfile', '-NonInteractive', '-WindowStyle', 'Hidden',
                                   '-Command', launch_script], env=env, capture_output=True,
                                  timeout=6, creationflags=subprocess.CREATE_NO_WINDOW)
        if launched.returncode:
            raise OSError('Windows daemon launch failed: ' + launched.stderr.decode(errors='replace')[:500])
        result = json.loads(launched.stdout)
        if result.get('ReturnValue') != 0 or type(result.get('ProcessId')) is not int:
            raise OSError('Windows daemon launch was rejected: ' + str(result.get('ReturnValue')))
        return _DetachedProcess(result['ProcessId'])
    options["start_new_session"] = True
    return subprocess.Popen(command, **options)


class _DetachedProcess:
    """Minimal launch receipt without attaching a process to the client's Job."""

    def __init__(self, pid):
        self.pid = pid
        self.process_start = process_start_filetime(pid)

    def poll(self):
        return None if _process_is_live(self.pid, self.process_start) else 1


def _verify_ping(endpoint: dict[str, Any], token: str, timeout: float = _RPC_TIMEOUT_SECONDS) -> None:
    result = _request(endpoint, token, "daemon.ping", timeout=timeout)
    expected = {key: endpoint[key] for key in ("pid", "process_start", "backend_id", "port", "protocol", "build")}
    if result != expected:
        raise BackendUnavailableError("AUTH", "Backend identity differs from its private endpoint")


def _wait_for_exit(endpoint: dict[str, Any], timeout: float = _STARTUP_WAIT_SECONDS) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        try:
            if not _process_is_live(endpoint["pid"], endpoint["process_start"]):
                return True
        except BackendUnavailableError as exc:
            if "different creation time" in str(exc):
                return True
            raise
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def shutdown_existing(runtime_home, timeout: float = _STARTUP_WAIT_SECONDS):
    """Shutdown only a verified existing daemon; this helper never starts one."""
    home = Path(runtime_home).expanduser().absolute()
    from .security import ensure_safe_parent, verify_private_path

    ensure_safe_parent(home)
    if not os.path.lexists(home):
        return {"stopped": False, "reason": "offline"}
    verify_private_path(home)
    endpoint = _read_endpoint(home)
    if endpoint is None:
        return {"stopped": False, "reason": "offline"}
    token = _read_token(home)
    result = _request(endpoint, token, "daemon.shutdown", timeout=timeout)
    if not isinstance(result, dict) or result.get("shutdown") is not True:
        raise BackendUnavailableError("OFFLINE", "Backend did not confirm graceful shutdown")
    stopped = _wait_for_exit(endpoint, timeout=timeout)
    return {"stopped": stopped, "reason": None if stopped else "stopping"}


class _RemoteTasks:
    def __init__(self, owner):
        self._owner = owner

    def history_page(self, limit=30, state=None, before_sequence=None):
        return self._owner._call("tasks.history_page", limit=limit, state=state,
                                 before_sequence=before_sequence)


class _RemoteMemory:
    def __init__(self, owner):
        self._owner = owner

    def read(self, key=None, limit=30, after_key=None, before_note_sequence=None):
        return self._owner._call("memory.read", key=key, limit=limit, after_key=after_key,
                                 before_note_sequence=before_note_sequence)

    def write(self, operation_id, change):
        return self._owner._call("memory.write", operation_id=operation_id, change=change)


class _RemoteLogReader:
    def __init__(self, owner):
        self._owner = owner

    def read(self, max_chars=65536):
        return self._owner._call("log_reader.read", max_chars=max_chars)


class SharedService:
    """Service-compatible frontend proxy; close never stops the shared daemon."""

    def __init__(self, runtime_home):
        self.runtime_home = Path(runtime_home).expanduser().absolute()
        self.tasks = _RemoteTasks(self)
        self.memory = _RemoteMemory(self)
        self.log_reader = _RemoteLogReader(self)
        self._condition = threading.Condition()
        self._endpoint = None
        self._token = None
        self._failure = None
        self._retry_at = 0.0
        self._starting = False
        self._closed = False
        self._thread = None
        self._ensure_starting()

    def _ensure_starting(self):
        with self._condition:
            if self._closed or self._endpoint is not None or self._starting:
                return
            if time.monotonic() < self._retry_at:
                return
            self._starting = True
            self._failure = None
            self._thread = threading.Thread(target=self._startup_worker,
                                            name="ansa-mcp-shared-backend-discovery", daemon=True)
            try:
                self._thread.start()
            except BaseException as exc:
                self._starting = False
                self._failure = ("OFFLINE", "Could not start backend discovery thread: " + str(exc))
                self._retry_at = time.monotonic() + 0.25
                self._condition.notify_all()
                try:
                    _append_startup_log(self.runtime_home, traceback.format_exc())
                except Exception:
                    pass

    def _startup_worker(self):
        try:
            endpoint, token = self._discover_or_start()
        except BackendUnavailableError as exc:
            failure = (exc.code, str(exc))
            try:
                _append_startup_log(self.runtime_home, f"{exc.code}: {exc}")
            except Exception:
                pass
        except BaseException as exc:
            failure = ("OFFLINE", "Shared backend startup failed: " + str(exc))
            try:
                _append_startup_log(self.runtime_home, traceback.format_exc())
            except Exception:
                pass
        else:
            failure = None
        with self._condition:
            if not self._closed and failure is None:
                self._endpoint, self._token = endpoint, token
                self._failure = None
                self._retry_at = 0.0
            elif not self._closed:
                self._endpoint = self._token = None
                self._failure = failure
                self._retry_at = time.monotonic() + 0.25
            self._starting = False
            self._condition.notify_all()

    def _discover_or_start(self):
        from .security import ensure_private_directory, ensure_safe_parent

        ensure_safe_parent(self.runtime_home)
        ensure_private_directory(self.runtime_home)
        with _startup_lock(self.runtime_home, lambda: self._closed):
            endpoint = _read_endpoint(self.runtime_home)
            if endpoint is not None:
                token = _read_token(self.runtime_home)
                _verify_ping(endpoint, token, timeout=2.0)
                return endpoint, token

            marker = _read_starting(self.runtime_home)
            if marker is not None:
                return self._wait_for_existing_start(marker)

            if _backend_lock_busy(self.runtime_home):
                raise BackendUnavailableError("BUSY", "Another Service owns this runtime backend.lock")

            try:
                process = _spawn_daemon(self.runtime_home)
                process_start = process_start_filetime(process.pid)
                _write_private_json(_starting_path(self.runtime_home),
                                    {"pid": process.pid, "process_start": process_start})
            except BaseException as exc:
                raise BackendUnavailableError("OFFLINE", "Could not launch shared backend daemon: " + str(exc)) from exc
            return self._wait_for_child(process, process_start)

    def _wait_for_existing_start(self, marker):
        deadline = time.monotonic() + _STARTUP_WAIT_SECONDS
        while True:
            if self._closed:
                raise RuntimeError("MCP backend client is closed")
            endpoint = _read_endpoint(self.runtime_home)
            if endpoint is not None:
                token = _read_token(self.runtime_home)
                _verify_ping(endpoint, token, timeout=2.0)
                return endpoint, token
            if not _process_is_live(marker["pid"], marker["process_start"]):
                if _backend_lock_busy(self.runtime_home):
                    raise BackendUnavailableError("BUSY", "Another Service owns this runtime backend.lock")
                raise BackendUnavailableError("OFFLINE", "The other backend startup process exited")
            if time.monotonic() >= deadline:
                raise BackendUnavailableError("STARTING", "Shared backend daemon is still starting")
            time.sleep(0.05)

    def _wait_for_child(self, process, process_start):
        deadline = time.monotonic() + _STARTUP_WAIT_SECONDS
        marker = {"pid": process.pid, "process_start": process_start}
        while True:
            if self._closed:
                raise RuntimeError("MCP backend client is closed")
            endpoint = _read_endpoint(self.runtime_home)
            if endpoint is not None:
                token = _read_token(self.runtime_home)
                _verify_ping(endpoint, token, timeout=2.0)
                return endpoint, token
            exit_code = process.poll()
            if exit_code is not None:
                if _backend_lock_busy(self.runtime_home):
                    raise BackendUnavailableError("BUSY", "Another Service owns this runtime backend.lock")
                raise BackendUnavailableError("OFFLINE", f"Shared backend daemon exited during startup ({exit_code})")
            if time.monotonic() >= deadline:
                raise BackendUnavailableError("STARTING", "Shared backend daemon is still starting")
            time.sleep(0.05)

    def _wait_ready(self):
        deadline = time.monotonic() + _STARTUP_WAIT_SECONDS
        while True:
            with self._condition:
                if self._closed:
                    raise RuntimeError("MCP backend client is closed")
                endpoint, token = self._endpoint, self._token
                if endpoint is None:
                    if not self._starting:
                        if self._failure is not None and time.monotonic() < self._retry_at:
                            code, message = self._failure
                            raise BackendUnavailableError(code, message)
                        self._ensure_starting()
                    if self._endpoint is None and self._failure is not None and not self._starting:
                        code, message = self._failure
                        raise BackendUnavailableError(code, message)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise BackendUnavailableError("STARTING", "Shared backend was not ready within 8 seconds")
                    self._condition.wait(min(remaining, 0.1))
                    continue
            try:
                live = _process_is_live(endpoint["pid"], endpoint["process_start"])
            except BackendUnavailableError:
                raise
            if live:
                return endpoint, token
            with self._condition:
                if self._endpoint == endpoint:
                    self._endpoint = self._token = None
                    self._failure = None
            if time.monotonic() >= deadline:
                raise BackendUnavailableError("STARTING", "Shared backend was not ready within 8 seconds")

    def _call(self, method, *args, **kwargs):
        endpoint, token = self._wait_ready()
        try:
            return _request(endpoint, token, method, args=args, kwargs=kwargs)
        except BackendUnavailableError as exc:
            if exc.code == "OFFLINE":
                with self._condition:
                    if self._endpoint == endpoint:
                        self._endpoint = self._token = None
                        self._failure = None
                        self._retry_at = 0.0
            raise

    def status(self):
        return self._call("status")

    def model_snapshot(self, expected_target):
        return self._call("model_snapshot", expected_target=expected_target)

    def panel_show(self, expected_target):
        return self._call("panel_show", expected_target=expected_target)

    def submit_task(self, tool, params, operation_id, expected_target, expected_model, ttl_seconds=300):
        return self._call("submit_task", tool=tool, params=params, operation_id=operation_id,
                          expected_target=expected_target, expected_model=expected_model,
                          ttl_seconds=ttl_seconds)

    def query_task(self, task_id=None, operation_id=None):
        return self._call("query_task", task_id=task_id, operation_id=operation_id)

    def cancel_task(self, task_id):
        return self._call("cancel_task", task_id=task_id)

    def shutdown(self):
        """Gracefully stop an idle daemon; a later call may start/discover it again."""
        endpoint, token = self._wait_ready()
        result = _request(endpoint, token, "daemon.shutdown")
        if not isinstance(result, dict) or result.get("shutdown") is not True:
            raise BackendUnavailableError("OFFLINE", "Backend did not confirm graceful shutdown")
        _wait_for_exit(endpoint)
        with self._condition:
            if self._endpoint == endpoint:
                self._endpoint = self._token = None
                self._failure = None
                self._retry_at = 0.0
            self._condition.notify_all()
        return result

    def close(self):
        """Close only this frontend's client state; the daemon remains running."""
        with self._condition:
            self._closed = True
            self._endpoint = self._token = None
            self._condition.notify_all()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=0.5)
