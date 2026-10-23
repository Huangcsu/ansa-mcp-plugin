"""Long-lived, authenticated RPC owner for one private runtime directory."""

from __future__ import annotations

import argparse
import hmac
import json
import os
from pathlib import Path
import socketserver
import sys
import threading
import traceback

from .shared_client import (
    BackendUnavailableError,
    _PROTOCOL,
    _BUILD,
    _error_payload,
    _read_token,
    _recv_frame,
    _send_frame,
    _write_private_json,
    shutdown_existing,
)


_RPC_METHODS = frozenset({
    "status",
    "model_snapshot",
    "panel_show",
    "submit_task",
    "query_task",
    "cancel_task",
    "tasks.history_page",
    "memory.read",
    "memory.write",
    "log_reader.read",
})
_CONTROL_METHODS = frozenset({"daemon.ping", "daemon.shutdown"})


class _DaemonState:
    def __init__(self, service, endpoint):
        self.service = service
        self.endpoint = endpoint
        self.token = service.token
        self.admission_lock = threading.RLock()
        self.accepting = True
        self.shutdown_requested = threading.Event()

    def dispatch(self, method, args, kwargs):
        if method == "daemon.ping":
            if args or kwargs:
                raise ValueError("daemon.ping does not accept arguments")
            return {key: self.endpoint[key] for key in
                    ("pid", "process_start", "backend_id", "port", "protocol", "build")}

        if method == "daemon.shutdown":
            if args or kwargs:
                raise ValueError("daemon.shutdown does not accept arguments")
            with self.admission_lock:
                if not self.accepting:
                    raise BackendUnavailableError("BUSY", "Backend shutdown is already in progress")
                pending = self.service.tasks.pending()
                if pending:
                    raise BackendUnavailableError("BUSY", "Backend has pending tasks and cannot stop safely")
                self.accepting = False
                return {"shutdown": True, "pending": 0}

        if method not in _RPC_METHODS:
            raise ValueError("Unsupported shared backend method")
        with self.admission_lock:
            if not self.accepting:
                raise BackendUnavailableError("BUSY", "Backend shutdown is in progress")
            methods = {
                "status": self.service.status,
                "model_snapshot": self.service.model_snapshot,
                "panel_show": self.service.panel_show,
                "submit_task": self.service.submit_task,
                "query_task": self.service.query_task,
                "cancel_task": self.service.cancel_task,
                "tasks.history_page": self.service.tasks.history_page,
                "memory.read": self.service.memory.read,
                "memory.write": self.service.memory.write,
                "log_reader.read": self.service.log_reader.read,
            }
            result = methods[method](*args, **kwargs)
            if method == "tasks.history_page":
                # Keep request/script bodies and potentially large results inside
                # the daemon; the public list tool needs only these summaries.
                return {
                    "tasks": [{
                        **{key: record[key] for key in
                           ("task_id", "operation_id", "state", "dispatch", "cancel_pending")},
                        "task": {key: record["task"][key] for key in ("tool", "target", "model")},
                    } for record in result["tasks"]],
                    "has_more": result["has_more"],
                    "next_before_sequence": result["next_before_sequence"],
                }
            return result


def _handler_type(state):
    class _Handler(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.settimeout(30.0)
            should_stop = False
            try:
                request = _recv_frame(self.request)
            except (EOFError, OSError, UnicodeError, ValueError):
                return
            try:
                if set(request) != {"protocol", "token", "method", "args", "kwargs"}:
                    raise BackendUnavailableError("AUTH", "Shared backend request is malformed")
                if request["protocol"] != _PROTOCOL:
                    raise BackendUnavailableError("AUTH", "Shared backend protocol mismatch")
                supplied = request["token"]
                if not isinstance(supplied, str) or not hmac.compare_digest(supplied, state.token):
                    raise BackendUnavailableError("AUTH", "Shared backend authentication failed")
                method, args, kwargs = request["method"], request["args"], request["kwargs"]
                if not isinstance(method, str) or not isinstance(args, list) or not isinstance(kwargs, dict):
                    raise BackendUnavailableError("AUTH", "Shared backend request fields are invalid")
                result = state.dispatch(method, args, kwargs)
                response = {"protocol": _PROTOCOL, "ok": True, "result": result}
                should_stop = method == "daemon.shutdown" and result.get("shutdown") is True
            except Exception as exc:
                response = {"protocol": _PROTOCOL, "ok": False,
                            "error": _error_payload(exc, state.token)}
            try:
                _send_frame(self.request, response)
            except (OSError, ValueError):
                pass
            finally:
                if should_stop:
                    state.shutdown_requested.set()

    return _Handler


class _ThreadingTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = False
    daemon_threads = True
    block_on_close = False
    request_queue_size = 64

    def __init__(self, *args, **kwargs):
        self._slots = threading.BoundedSemaphore(32)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def _startup_failure(runtime_home: Path, exc: BaseException, service=None):
    from .shared_client import _append_startup_log

    token = getattr(service, "token", None)
    if token is None:
        try:
            token = _read_token(runtime_home)
        except Exception:
            token = None
    try:
        _append_startup_log(runtime_home, traceback.format_exc(), token=token)
    except Exception:
        pass


def _serve(runtime_home) -> int:
    from ansa_mcp_shared.identity import process_start_filetime

    from .security import ensure_private_directory, ensure_safe_parent
    from .service import Service

    home = Path(runtime_home).expanduser().absolute()
    service = None
    server = None
    serve_thread = None
    try:
        ensure_safe_parent(home)
        ensure_private_directory(home)
        service = Service(home)
        server = _ThreadingTCPServer(("127.0.0.1", 0), _handler_type)
        endpoint = {
            "pid": os.getpid(),
            "process_start": process_start_filetime(),
            "backend_id": service.backend_id,
            "port": int(server.server_address[1]),
            "protocol": _PROTOCOL,
            "build": _BUILD,
        }
        state = _DaemonState(service, endpoint)
        server.RequestHandlerClass = _handler_type(state)
        serve_thread = threading.Thread(target=server.serve_forever,
                                        name="ansa-mcp-backend-rpc", daemon=True)
        serve_thread.start()
        _write_private_json(home / "backend.endpoint.json", endpoint)
        state.shutdown_requested.wait()
        server.shutdown()
        serve_thread.join(timeout=10.0)
        server.server_close()
        server = None
        service.close()
        service = None
        return 0
    except BaseException as exc:
        _startup_failure(home, exc, service)
        return 1
    finally:
        if server is not None:
            try:
                server.server_close()
            except Exception:
                pass
        if service is not None:
            try:
                service.close()
            except Exception as exc:
                _startup_failure(home, exc, service)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run or safely stop the ANSA MCP shared backend")
    parser.add_argument("--runtime-home", required=True)
    parser.add_argument("--shutdown", action="store_true",
                        help="stop an existing daemon only when it has no pending tasks")
    args = parser.parse_args(argv)
    try:
        if args.shutdown:
            result = shutdown_existing(args.runtime_home)
            if result.get("stopped"):
                print("Shared backend stopped.")
                return 0
            if result.get("reason") == "offline":
                print("No active shared backend daemon.")
                return 0
            print("Shared backend accepted shutdown and is still stopping.")
            return 0
        return _serve(args.runtime_home)
    except BackendUnavailableError as exc:
        print(f"{exc.code}: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"OFFLINE: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
