"""Loopback listener. Network threads never call ANSA APIs."""

import hmac
import json
import os
from pathlib import Path
import queue
import select
import socket
import threading
import time

from ansa_mcp_shared.framing import recv_frame, send_frame
from ansa_mcp_shared.protocol import validate

CONTROL_KINDS = frozenset({"probe", "grant", "revoke", "authorization", "model_snapshot", "panel_show"})
_STARTUP_PROBE_MAX_ATTEMPTS = 3
_STARTUP_PROBE_BACKOFF = (0.25, 0.5)


class BridgeTransport:
    def __init__(self, state, executor, token):
        self.state, self.executor, self.token = state, executor, token
        self.outgoing = queue.Queue()
        self._client = None
        self._client_lock = threading.Lock()
        self._stop = threading.Event()
        self._startup_probe_lock = threading.Lock()
        self._connection_generation = 0
        self._active_connection_generation = None
        self._active_backend_id = None
        self._startup_probe_round = None
        self._startup_probe_in_flight = False
        self._startup_probe_timer = None
        self._published = False
        self._thread = threading.Thread(target=self._accept_loop, name="ansa-mcp-transport", daemon=True)
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
            self.listener.bind(("127.0.0.1", 0))
            self.listener.listen(2)
            self.listener.settimeout(.5)
            self.port = self.listener.getsockname()[1]
            self._publish_registration()
            self._published = True
            self._thread.start()
        except BaseException as original:
            # The caller cannot close an object whose constructor did not return.
            # Clean up our publication and socket here, preserving the cause.
            for cleanup in (self.close, lambda: (self.state.instance_dir / "connection.tmp").unlink(missing_ok=True)):
                try:
                    cleanup()
                except BaseException as error:
                    original.add_note(f"Transport initialization cleanup also failed: {error!r}")
            if self._thread.is_alive():
                self._thread.join(timeout=5)
            raise

    def _publish_registration(self):
        path = self.state.instance_dir / "connection.json"
        value = {"version": 1, "host": "127.0.0.1", "port": self.port,
                 "pid": self.state.pid, "process_start": self.state.process_start,
                 "instance_id": self.state.instance_id, "session_id": self.state.session_id}
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(value), encoding="utf-8")
        os.replace(temp, path)

    def request_bind(self):
        if not self.state.connected.is_set():
            raise ConnectionError("MCP 后端未连接；请先在 MCP 客户端启动后端，再点击连接")
        self.outgoing.put_nowait({"version": 1, "kind": "bind_requested", "request_id": os.urandom(8).hex(),
                                 "instance_id": self.state.instance_id})

    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                client, addr = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with client:
                with self._client_lock:
                    self._client = client
                client.settimeout(8)
                try:
                    self._serve(client)
                except (OSError, EOFError, ValueError):
                    pass
                finally:
                    self._disconnect_backend()
                    with self._client_lock:
                        self._client = None

    @staticmethod
    def _probe_status(state="idle", backend_id=None, started_at=None, attempt=0, error=None):
        return {
            "state": state,
            "backend_id": backend_id,
            "started_at": started_at,
            "attempt": attempt,
            "error": error,
        }

    def _activate_backend(self, backend_id):
        with self._startup_probe_lock:
            self._connection_generation += 1
            generation = self._connection_generation
            started_at = time.monotonic()
            timer = self._startup_probe_timer
            self._startup_probe_timer = None
            if timer is not None:
                timer.cancel()
            self._active_connection_generation = generation
            self._active_backend_id = backend_id
            self._startup_probe_round = {
                "generation": generation,
                "backend_id": backend_id,
                "started_at": started_at,
                "attempt": 1,
            }
            self.state.connected_backend_id = backend_id
            self.state.backend_status = {}
            self.state.connected.set()
            self.state.startup_probe = self._probe_status(
                "pending", backend_id, started_at, 1, None
            )
            return generation

    def _reserve_startup_probe_locked(self):
        probe_round = self._startup_probe_round
        if (
            self._stop.is_set()
            or probe_round is None
            or self._startup_probe_in_flight
            or self._startup_probe_timer is not None
            or self._active_connection_generation != probe_round["generation"]
            or not self.state.connected.is_set()
            or self.state.connected_backend_id != probe_round["backend_id"]
        ):
            return None
        self._startup_probe_in_flight = True
        attempt = probe_round["attempt"]
        status = "pending" if attempt == 1 else "retrying"
        self.state.startup_probe = self._probe_status(
            status,
            probe_round["backend_id"],
            probe_round["started_at"],
            attempt,
            self.state.startup_probe.get("error"),
        )
        return (
            probe_round["generation"],
            probe_round["backend_id"],
            probe_round["started_at"],
            attempt,
        )

    def _schedule_startup_probe(self, generation):
        with self._startup_probe_lock:
            if generation != self._active_connection_generation:
                return
            probe = self._reserve_startup_probe_locked()
        if probe is not None:
            self._submit_startup_probe(probe)

    def _submit_startup_probe(self, probe):
        try:
            self.executor.submit(
                "probe", {},
                lambda value, error, context=probe: self._startup_probe_done(context, error),
            )
        except Exception as exc:
            self._startup_probe_done(probe, str(exc))

    def _startup_probe_done(self, probe, error):
        generation, backend_id, started_at, attempt = probe
        timer = None
        next_probe = None
        with self._startup_probe_lock:
            self._startup_probe_in_flight = False
            current = (
                generation == self._active_connection_generation
                and backend_id == self._active_backend_id
                and self.state.connected.is_set()
                and backend_id == self.state.connected_backend_id
                and not self._stop.is_set()
            )
            if current and error is None:
                self.state.startup_probe = self._probe_status(
                    "ready", backend_id, started_at, attempt, None
                )
            elif current and attempt < _STARTUP_PROBE_MAX_ATTEMPTS:
                next_attempt = attempt + 1
                self._startup_probe_round["attempt"] = next_attempt
                error_text = str(error or "Startup probe failed")
                self.state.startup_probe = self._probe_status(
                    "retrying", backend_id, started_at, next_attempt, error_text
                )
                delay = _STARTUP_PROBE_BACKOFF[attempt - 1]
                timer = threading.Timer(
                    delay, self._retry_startup_probe, args=(generation,)
                )
                timer.daemon = True
                self._startup_probe_timer = timer
            elif current:
                self.state.startup_probe = self._probe_status(
                    "failed", backend_id, started_at, attempt,
                    str(error or "Startup probe failed"),
                )
            else:
                next_probe = self._reserve_startup_probe_locked()
        if timer is not None:
            timer.start()
        if next_probe is not None:
            self._submit_startup_probe(next_probe)

    def _retry_startup_probe(self, generation):
        with self._startup_probe_lock:
            probe_round = self._startup_probe_round
            if (
                probe_round is None
                or probe_round["generation"] != generation
                or self._active_connection_generation != generation
            ):
                return
            self._startup_probe_timer = None
            probe = self._reserve_startup_probe_locked()
        if probe is not None:
            self._submit_startup_probe(probe)

    def _disconnect_backend(self):
        with self._startup_probe_lock:
            connected = getattr(self.state, "connected", None)
            if connected is not None:
                connected.clear()
            if hasattr(self.state, "connected_backend_id"):
                self.state.connected_backend_id = None
            timer = self._startup_probe_timer
            self._startup_probe_timer = None
            if timer is not None:
                timer.cancel()
            if self._active_connection_generation is not None:
                self._connection_generation += 1
                self._active_connection_generation = None
                self._active_backend_id = None
                self._startup_probe_round = None
                self.state.startup_probe = self._probe_status()

    def _serve(self, client):
        hello = validate(recv_frame(client))
        if hello["kind"] != "hello" or not hmac.compare_digest(str(hello.get("token", "")), self.token):
            raise ValueError("Authentication failed")
        if hello.get("instance_id") != self.state.instance_id or hello.get("process_start") != self.state.process_start:
            raise ValueError("Instance identity mismatch")
        generation = self._activate_backend(hello.get("backend_id"))
        send_frame(client, {"version": 1, "kind": "reply", "request_id": hello["request_id"],
                            "ok": True, "data": {"instance_id": self.state.instance_id,
                                                 "session_id": self.state.session_id}})
        self._schedule_startup_probe(generation)
        client.settimeout(5)
        while not self._stop.is_set():
            try:
                outgoing = self.outgoing.get_nowait()
            except queue.Empty:
                outgoing = None
            if outgoing:
                send_frame(client, outgoing)
            readable, _, _ = select.select([client], [], [], 0.2)
            if not readable:
                continue
            message = validate(recv_frame(client))
            if message["kind"] in {"task_submit", "task_query", "task_cancel", "result_ack", "task_status", "backend_status"}:
                try:
                    kind = message["kind"]
                    journal = self.state.journal
                    if kind == "task_submit":
                        value = journal.submit(message["task"])
                    elif kind == "task_query":
                        value = journal.get(message["task_id"])
                        if value is not None:
                            value = {**value, "journal_blocked": journal.blocked}
                    elif kind == "task_cancel":
                        value = journal.cancel(message["task"], state=message.get("state", "cancelled"))
                    elif kind == "result_ack":
                        value = journal.ack(message["task_id"])
                    elif kind == "backend_status":
                        self.state.backend_status = message["state"]
                        value = True
                    else:
                        value = {"busy": journal.busy(), "blocked": journal.blocked,
                                 "stopped": self.state.stopped.is_set(), "connected": self.state.connected.is_set()}
                    reply = {"ok": True, "data": value}
                except Exception as exc:
                    reply = {"ok": False, "error": str(exc)}
                send_frame(client, {"version": 1, "kind": "reply", "request_id": message["request_id"], **reply})
                continue
            if message["kind"] not in CONTROL_KINDS:
                send_frame(client, {"version": 1, "kind": "reply", "request_id": message["request_id"],
                                   "ok": False, "error": "Unsupported control request"})
                continue
            finished = threading.Event()
            result = {}
            def done(value, error, response=result, completed=finished):
                response.update(value=value, error=error)
                completed.set()
            try:
                self.executor.submit(message["kind"], message, done)
            except queue.Full:
                done(None, "ANSA main-thread queue is full")
            if not finished.wait(5):
                reply = {"ok": False, "error": "Main thread did not execute within 5 seconds"}
            else:
                reply = {"ok": result["error"] is None,
                         "data": result["value"], "error": result["error"]}
            send_frame(client, {"version": 1, "kind": "reply", "request_id": message["request_id"], **reply})

    def close(self):
        self._stop.set()
        self._disconnect_backend()
        with self._client_lock:
            if self._client is not None:
                try:
                    self._client.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        self.listener.close()
        if self._published:
            path = self.state.instance_dir / "connection.json"
            path.unlink(missing_ok=True)
            self._published = False
