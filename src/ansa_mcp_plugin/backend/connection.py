"""Authenticated request/reply channel to one registered ANSA instance."""

import queue
import select
import socket
import threading
import uuid

from ansa_mcp_shared.framing import recv_frame, send_frame
from ansa_mcp_shared.protocol import request, validate


class BridgeConnection:
    def __init__(self, registration, token, on_bind, backend_id=None, connect_timeout=2, start_reader=True,
                 auth_timeout=5):
        self.registration = registration
        self.sock = None
        self.closed = True
        self._closing = False
        self.failure_stage = None
        self.failure_reason = None
        try:
            self.sock = socket.create_connection(("127.0.0.1", registration["port"]), timeout=connect_timeout)
        except Exception as exc:
            self._set_failure_stage(exc, "connect")
            raise
        hello = request("hello", str(uuid.uuid4()), token=token,
                        instance_id=registration["instance_id"], process_start=registration["process_start"], backend_id=backend_id)
        try:
            self.sock.settimeout(auth_timeout)
            send_frame(self.sock, hello)
            response = validate(recv_frame(self.sock))
            if response["kind"] != "reply" or response["request_id"] != hello["request_id"] or not response.get("ok"):
                raise PermissionError("ANSA bridge authentication failed")
            if (response["data"]["instance_id"], response["data"]["session_id"]) != (registration["instance_id"], registration["session_id"]):
                raise RuntimeError("ANSA bridge session changed")
            self.sock.settimeout(5)
        except Exception as exc:
            self.failure_stage = "authentication"
            self.failure_reason = self._reason(exc)
            self._close_socket()
            self._set_failure_stage(exc, "authentication")
            raise
        self.pending = {}
        self.lock = threading.Lock()
        self.send_lock = threading.Lock()
        self.on_bind = on_bind
        self.closed = False
        self.thread = None
        if start_reader:
            self.start_reader()

    def start_reader(self):
        if self.thread is None and not self.closed:
            self.thread = threading.Thread(target=self._reader, name="ansa-mcp-client-reader", daemon=True)
            try:
                self.thread.start()
            except Exception as exc:
                self.failure_stage = "reader_start"
                self.failure_reason = self._reason(exc)
                self.close()
                self._set_failure_stage(exc, "reader_start")
                raise

    def _reader(self):
        failure = None
        try:
            while not self.closed:
                readable, _, _ = select.select([self.sock], [], [], 0.25)
                if not readable:
                    continue
                message = validate(recv_frame(self.sock))
                if message["kind"] == "bind_requested":
                    threading.Thread(target=self.on_bind, args=(self.registration["instance_id"],), daemon=True).start()
                elif message["kind"] == "reply":
                    with self.lock:
                        receiver = self.pending.get(message["request_id"])
                    if receiver:
                        try:
                            receiver.put_nowait(message)
                        except queue.Full:
                            pass
        except Exception as exc:
            failure = exc
        finally:
            self.closed = True
            if failure is not None and not self._closing:
                self.failure_stage = "reader"
                self.failure_reason = self._reason(failure)
            self._close_socket()
            error = self.failure_reason or "Bridge disconnected"
            with self.lock:
                for receiver in self.pending.values():
                    try:
                        receiver.put_nowait({"ok": False, "error": error})
                    except queue.Full:
                        pass

    def call(self, kind, **fields):
        if self.closed:
            raise ConnectionError(self.failure_reason or "ANSA bridge disconnected")
        message = request(kind, str(uuid.uuid4()), **fields)
        receiver = queue.Queue(maxsize=1)
        with self.lock:
            self.pending[message["request_id"]] = receiver
        try:
            with self.send_lock:
                send_frame(self.sock, message)
            try:
                response = receiver.get(timeout=8)
            except queue.Empty as exc:
                raise TimeoutError("ANSA main-thread reply not confirmed") from exc
            if not response.get("ok"):
                raise RuntimeError(response.get("error") or "ANSA bridge request failed")
            return response.get("data")
        finally:
            with self.lock:
                self.pending.pop(message["request_id"], None)

    def close(self):
        self._closing = True
        self.closed = True
        self._close_socket()

    def _close_socket(self):
        if self.sock is None:
            return
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass

    @staticmethod
    def _reason(exc):
        return str(exc).strip() or type(exc).__name__

    @staticmethod
    def _set_failure_stage(exc, stage):
        try:
            exc.connection_stage = stage
        except Exception:
            pass
