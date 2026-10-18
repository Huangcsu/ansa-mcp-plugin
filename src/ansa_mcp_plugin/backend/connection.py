"""Authenticated request/reply channel to one registered ANSA instance."""

import queue
import select
import socket
import threading
import uuid

from ansa_mcp_shared.framing import recv_frame, send_frame
from ansa_mcp_shared.protocol import request, validate


class BridgeConnection:
    def __init__(self, registration, token, on_bind, backend_id=None, connect_timeout=5, start_reader=True):
        self.registration = registration
        self.sock = socket.create_connection(("127.0.0.1", registration["port"]), timeout=connect_timeout)
        self.sock.settimeout(connect_timeout)
        hello = request("hello", str(uuid.uuid4()), token=token,
                        instance_id=registration["instance_id"], process_start=registration["process_start"], backend_id=backend_id)
        try:
            send_frame(self.sock, hello)
            response = validate(recv_frame(self.sock))
            if response["kind"] != "reply" or response["request_id"] != hello["request_id"] or not response.get("ok"):
                raise PermissionError("ANSA bridge authentication failed")
            if (response["data"]["instance_id"], response["data"]["session_id"]) != (registration["instance_id"], registration["session_id"]):
                raise RuntimeError("ANSA bridge session changed")
        except Exception:
            self.sock.close()
            raise
        self.pending = {}
        self.lock = threading.Lock()
        self.send_lock = threading.Lock()
        self.on_bind = on_bind
        self.closed = False
        self.sock.settimeout(5)
        self.thread = None
        if start_reader:
            self.start_reader()

    def start_reader(self):
        if self.thread is None and not self.closed:
            self.thread = threading.Thread(target=self._reader, name="ansa-mcp-client-reader", daemon=True)
            try:
                self.thread.start()
            except Exception:
                self.close()
                raise

    def _reader(self):
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
        except (OSError, EOFError, ValueError):
            pass
        finally:
            self.closed = True
            with self.lock:
                for receiver in self.pending.values():
                    try:
                        receiver.put_nowait({"ok": False, "error": "Bridge disconnected"})
                    except queue.Full:
                        pass

    def call(self, kind, **fields):
        if self.closed:
            raise ConnectionError("ANSA bridge disconnected")
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
        self.closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()
