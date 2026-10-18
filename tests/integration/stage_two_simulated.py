"""Exercise phase-two persistence, transport, journal, and scheduling without ANSA.

Model operations are deliberately mocked and run on each fake executor's
owning thread. This checks the surrounding protocol and durable state machine;
it is not evidence of ANSA API or GUI behavior.
"""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import queue
import sqlite3
import sys
import threading
import time
import types
import uuid

from ansa_mcp_bridge.executor import MainThreadExecutor
from ansa_mcp_bridge.journal import Journal
from ansa_mcp_bridge.transport import BridgeTransport
from ansa_mcp_plugin.backend import service as service_module
from ansa_mcp_shared.identity import process_start_filetime


def _assert(condition, message):
    if not condition:
        raise AssertionError(message)


def _wait_for(predicate, message, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("Timed out waiting for " + message)


class FakeState:
    """Small fake ANSA state; executor and mocked operations stay on main_id."""

    def __init__(self, root):
        self.pid = os.getpid()
        self.process_start = process_start_filetime()
        self.instance_id = str(uuid.uuid4())
        self.session_id = str(uuid.uuid4())
        self.main_id = None
        self.main_thread_id = None
        self.instance_dir = root / "instances" / self.instance_id
        self.session_dir = self.instance_dir / "sessions" / self.session_id
        self.session_dir.mkdir(parents=True)
        self.authorization_file = self.session_dir / "authorization.json"
        self.authorization = None
        self.model_epoch = 1
        self.write_enabled = True
        self.connected = threading.Event()
        self.connected_backend_id = None
        self.stopped = threading.Event()
        self.panel_refresh = None
        self.last_error = None
        self.journal = Journal(self.session_dir, self.identity())
        self.entities = {"grid-1": {"coordinates": [1.0, 2.0, 3.0], "label": "original"}}
        self.executions = []
        self.pause_claim = threading.Event()
        self.pause_probe = threading.Event()
        self.probe_entered = threading.Event()
        self.probe_release = threading.Event()
        self.operation_entered = threading.Event()
        self.operation_release = threading.Event()

    def identity(self):
        return {key: getattr(self, key)
                for key in ("pid", "process_start", "instance_id", "session_id")}

    def assert_main(self):
        if threading.get_ident() != self.main_id:
            raise RuntimeError("Fake ANSA operation ran outside its owning thread")

    def check_conflict(self):
        self.assert_main()
        return None

    def probe(self):
        self.assert_main()
        if self.pause_probe.is_set():
            self.probe_entered.set()
            self.probe_release.wait()
        return {
            **self.identity(),
            "main_thread": True,
            "thread_id": threading.get_ident(),
            "deck": 1,
            "model": {"epoch": self.model_epoch, "detection": "simulated",
                      "write_enabled": self.write_enabled},
            "authorization": self.authorization,
            "connected": self.connected.is_set(),
            "stopped": self.stopped.is_set(),
            "persistence_blocked": bool(self.journal and self.journal.blocked),
            "last_error": self.last_error,
        }

    def save_authorization(self, value):
        self.assert_main()
        temporary = self.authorization_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(value), encoding="utf-8")
        os.replace(temporary, self.authorization_file)
        self.authorization = value

    def validate_task(self, task):
        self.assert_main()
        if not self.connected.is_set() or self.stopped.is_set():
            raise RuntimeError("Fake executor is disconnected or stopped")
        if task["identity"] != self.identity():
            raise RuntimeError("Task identity changed")
        target = task["target"]
        grant = self.authorization
        if not grant or not grant.get("active") or (
                target["backend_id"], target["generation"]) != (
                grant["backend_id"], grant["generation"]):
            raise RuntimeError("Task authorization is stale")
        if (target["instance_id"], target["session_id"]) != (
                self.instance_id, self.session_id):
            raise RuntimeError("Task session is stale")
        if task["model"]["epoch"] != self.model_epoch:
            raise RuntimeError("Task model context is stale")
        if task["tool"] != "grid_read":
            self.check_conflict()
            if not self.write_enabled:
                raise RuntimeError("Simulated write capability is disabled")


class FakeBridge:
    def __init__(self, root, token):
        self.state = FakeState(root)
        self.executor = MainThreadExecutor(self.state)
        self.transport = BridgeTransport(self.state, self.executor, token)
        self.stop_event = threading.Event()
        self.main_calls = queue.Queue()
        self._original_task_tick = self.executor._task_tick
        self.executor._task_tick = self._task_tick
        self.thread = threading.Thread(target=self._run, name="fake-ansa-main", daemon=True)
        self.thread.start()

    def _run(self):
        self.state.main_id = threading.get_ident()
        self.state.main_thread_id = self.state.main_id
        while not self.stop_event.is_set():
            try:
                call, completed, result = self.main_calls.get_nowait()
            except queue.Empty:
                self.executor.tick()
            else:
                try:
                    result["value"] = call()
                except Exception as exc:
                    result["error"] = exc
                finally:
                    completed.set()
            time.sleep(0.005)

    def _task_tick(self):
        if not self.state.pause_claim.is_set():
            self._original_task_tick()

    def on_main(self, call, timeout=5.0):
        completed = threading.Event()
        result = {}
        self.main_calls.put((call, completed, result))
        if not completed.wait(timeout):
            raise TimeoutError("Fake ANSA main thread did not run test action")
        if "error" in result:
            raise result["error"]
        return result.get("value")

    def close(self):
        self.state.operation_release.set()
        self.stop_event.set()
        self.transport.close()
        self.thread.join(timeout=3)
        self.state.journal.close()


def _make_operations_module():
    """Install deterministic mock model operations for the fake main threads."""
    module = types.ModuleType("ansa_mcp_bridge.operations")

    def execute(state, tool, params):
        state.assert_main()
        label = params.get("test_case", tool)
        state.executions.append({"test_case": label, "tool": tool,
                                 "thread_id": threading.get_ident()})
        if params.get("hold"):
            state.operation_entered.set()
            if not state.operation_release.wait(10):
                raise TimeoutError("mock operation release timed out")

        if tool == "grid_create":
            entity_id = "grid-" + str(len(state.entities) + 1)
            state.entities[entity_id] = {"coordinates": params.get("coordinates", [0, 0, 0]),
                                         "label": label}
            return {"entity_id": entity_id, "entity": dict(state.entities[entity_id])}
        if tool == "grid_read":
            entity_id = params["entity_id"]
            return {"entity_id": entity_id, "entity": dict(state.entities[entity_id])}
        if tool == "grid_update":
            entity = state.entities[params["entity_id"]]
            for name, expected in params["expected_fields"].items():
                if entity.get(name) != expected:
                    raise RuntimeError("Entity precondition changed: " + name)
            entity.update(params["set_fields"])
            return {"entity_id": params["entity_id"], "entity": dict(entity)}
        if tool == "grid_delete":
            entity_id = params["entity_id"]
            del state.entities[entity_id]
            return {"deleted": entity_id}
        if tool == "model_save":
            return {"saved": True, "path": params["path"]}
        raise ValueError("Unsupported simulated operation: " + tool)

    module.execute = execute
    return module


@contextmanager
def _isolated_acl_and_operations():
    """Mock runtime ACL setup and model APIs; retain real sockets and identity."""
    security_names = ("ensure_safe_parent", "ensure_private_directory", "verify_private_path")
    prior_security = {name: getattr(service_module, name) for name in security_names}
    operations_name = "ansa_mcp_bridge.operations"
    prior_operations = sys.modules.get(operations_name)
    service_module.ensure_safe_parent = lambda path: None
    service_module.ensure_private_directory = lambda path: Path(path).mkdir(parents=True, exist_ok=True)
    service_module.verify_private_path = lambda path: None
    sys.modules[operations_name] = _make_operations_module()
    try:
        yield
    finally:
        for name, value in prior_security.items():
            setattr(service_module, name, value)
        if prior_operations is None:
            sys.modules.pop(operations_name, None)
        else:
            sys.modules[operations_name] = prior_operations


def _model(target):
    return {"epoch": target["model_epoch"]}


def _active_target(service, instance_id):
    value = service.status()["active_target"]
    return bool(value and value["instance_id"] == instance_id)


def _request_bind(service, bridge):
    _wait_for(bridge.state.connected.is_set, "backend discovery before panel binding")
    bridge.transport.request_bind()
    _wait_for(lambda: _active_target(service, bridge.state.instance_id),
              "panel binding for " + bridge.state.instance_id)


def _submit(service, target, tool, params, operation_id, ttl=300, model_epoch=1):
    return service.submit_task(tool, params, operation_id, target,
                               {"epoch": model_epoch}, ttl)


def _task_local(service, task_id):
    return service.tasks.get(task_id=task_id)


def _query_until_terminal(service, operation_id):
    def current():
        record = service.query_task(operation_id=operation_id)
        return record if record["state"] in {"succeeded", "failed", "cancelled", "expired"} else None

    return _wait_for(current, "operation " + operation_id + " to reach a terminal result")


def _wait_state(service, task_id, state, message):
    def current():
        record = _task_local(service, task_id)
        return record if record and record["state"] == state else None

    return _wait_for(current, message)


def _only_one_active(bridges):
    active = [bridge.state.instance_id for bridge in bridges
              if bridge.state.authorization and bridge.state.authorization.get("active")]
    _assert(len(active) == 1, "Expected exactly one active ANSA authorization; got " + repr(active))
    return active[0]


def _run_scenarios(root):
    service = service_module.Service(root)
    bridges = []
    checks = []
    response_lost_task = None
    response_lost_operation = "op-response-lost"
    original_execution_total = 0
    restarted = None
    try:
        bridges = [FakeBridge(root, service.token), FakeBridge(root, service.token)]
        first, second = bridges
        _request_bind(service, first)
        checks.append("initial panel binding")
        target = {"backend_id": service.backend_id, "instance_id": first.state.instance_id,
                  "session_id": first.state.session_id,
                  "generation": service.store.current()["generation"]}

        # Make one real socket task submission take effect, then lose its ACK at
        # the backend boundary. Reconciliation must read the journal, not resend.
        first_connection = service.connections[first.state.instance_id]
        submit_original_call = first_connection.call
        lost = {"once": True}

        def lose_submit_reply(kind, *args, **kwargs):
            result = submit_original_call(kind, *args, **kwargs)
            if kind == "task_submit" and lost["once"]:
                lost["once"] = False
                raise TimeoutError("simulated task_submit response loss")
            return result

        first_connection.call = lose_submit_reply
        request = {"coordinates": [4.0, 5.0, 6.0], "test_case": "response-loss"}
        accepted = _submit(service, target, "grid_create", request, response_lost_operation)
        duplicate = _submit(service, target, "grid_create", request, response_lost_operation)
        _assert(duplicate["task_id"] == accepted["task_id"], "same operation_id created a second task")
        try:
            _submit(service, target, "grid_create", {**request, "coordinates": [9, 9, 9]},
                    response_lost_operation)
            raise AssertionError("same operation_id accepted different parameters")
        except ValueError:
            pass
        converged = _query_until_terminal(service, response_lost_operation)
        _assert(converged["state"] == "succeeded",
                "query did not converge to journal success: " + repr(converged))
        _assert(sum(item["test_case"] == "response-loss" for item in first.state.executions) == 1,
                "lost submit ACK caused a duplicate model operation")
        _assert(converged["acked"], "terminal executor result was not acknowledged after persistence")
        first_connection.call = submit_original_call
        response_lost_task = accepted["task_id"]
        checks.extend(("duplicate submit idempotency", "changed request rejected",
                       "lost response query convergence", "no replay after uncertain submit"))

        # Hold the executor claim path while the socket already has the task.
        # Cancellation must install a journal tombstone before a duplicate late
        # delivery is accepted by the same real TCP endpoint.
        first.state.pause_claim.set()
        cancel_record = _submit(service, target, "grid_create",
                                {"coordinates": [7, 8, 9], "test_case": "cancel-before-claim"},
                                "op-cancel-before-claim")
        _wait_for(lambda: _task_local(service, cancel_record["task_id"])["dispatch"] == "sent",
                  "task to reach the transport")
        _wait_for(lambda: first.state.journal.get(cancel_record["task_id"]) is not None,
                  "sent task to persist in executor journal")
        cancelled = service.cancel_task(cancel_record["task_id"])
        _assert(cancelled["state"] == "cancelled" and cancelled["acked"],
                "unclaimed sent task was not cancelled and acknowledged")
        late = first_connection.call("task_submit", task=cancel_record["task"])
        _assert(late["state"] == "cancelled", "late duplicate delivery bypassed cancellation tombstone")
        first.state.pause_claim.clear()
        time.sleep(0.25)
        _assert(not any(item["test_case"] == "cancel-before-claim" for item in first.state.executions),
                "cancelled task executed after a late delivery")
        checks.append("sent unclaimed cancellation tombstone")

        # Pause scheduling while admitting a task, then replace the model before
        # dispatch. Resume must expire the old-context task without executing it.
        with service._lock:
            stale_model = _submit(service, target, "grid_create",
                                  {"test_case": "stale-model"}, "op-stale-model")
            service.dispatch_blocked = True
        first.on_main(lambda: setattr(first.state, "model_epoch", 2))
        service.dispatch_blocked = False
        expired = _wait_state(service, stale_model["task_id"], "expired",
                              "queued old-model task to expire")
        _assert(expired["dispatch"] == "not_sent", "stale model task reached ANSA transport")
        _assert(not any(item["test_case"] == "stale-model" for item in first.state.executions),
                "stale model task executed")
        checks.append("queued task expired after model replacement")

        # An entity can change within the same model epoch. The mock update
        # checks the caller's expected field on the owning main thread.
        with service._lock:
            stale_entity = _submit(
                service, target, "grid_update",
                {"entity_id": "grid-1", "expected_fields": {"coordinates": [1.0, 2.0, 3.0]},
                 "set_fields": {"coordinates": [6.0, 6.0, 6.0]}, "test_case": "stale-entity"},
                "op-stale-entity", model_epoch=2,
            )
            first.on_main(lambda: first.state.entities["grid-1"].update(
                coordinates=[3.0, 2.0, 1.0], label="manual-edit"))
        failed_entity = _wait_state(service, stale_entity["task_id"], "failed",
                                    "manual entity change to fail expected-field update")
        _assert("precondition" in str(failed_entity["error"]).lower(),
                "entity failure did not report the changed expected field")
        _assert(first.state.entities["grid-1"]["coordinates"] == [3.0, 2.0, 1.0],
                "stale update overwrote a manual entity edit")
        checks.append("manual entity field change rejected")

        # A blocked main-thread operation proves cancellation and switching are
        # refused after claim. Once complete, panel requests can switch A→B→A.
        first.state.operation_entered.clear()
        first.state.operation_release.clear()
        running = _submit(service, target, "grid_create",
                          {"test_case": "running-switch", "hold": True}, "op-running-switch",
                          model_epoch=2)
        _wait_for(lambda: first.state.operation_entered.is_set(), "main-thread task claim")
        observed_running = service.query_task(task_id=running["task_id"])
        _assert(observed_running["state"] == "running", "journal did not expose the claimed task")
        try:
            service.cancel_task(running["task_id"])
            raise AssertionError("already-running task accepted cancellation")
        except ValueError:
            pass
        try:
            service.bind(second.state.instance_id)
            raise AssertionError("busy target switched while ANSA operation was running")
        except RuntimeError as exc:
            _assert("task" in str(exc).lower() or "busy" in str(exc).lower(),
                    "busy switch failed for an unrelated reason: " + str(exc))
        _assert(first.state.authorization and first.state.authorization["active"],
                "busy switch revoked the old target")
        _assert(not second.state.authorization or not second.state.authorization.get("active"),
                "busy switch granted the candidate target")
        first.state.operation_release.set()
        finished_running = service.query_task(task_id=running["task_id"])
        _assert(finished_running["state"] == "succeeded", "running task did not finish after release")
        _request_bind(service, second)
        _assert(_only_one_active(bridges) == second.state.instance_id,
                "A→B switch did not produce one active grant")
        _request_bind(service, first)
        _assert(_only_one_active(bridges) == first.state.instance_id,
                "B→A switch did not produce one active grant")
        rebound = service.store.current()
        target = {"backend_id": rebound["backend_id"], "instance_id": rebound["instance_id"],
                  "session_id": rebound["session_id"], "generation": rebound["generation"]}
        checks.extend(("running cancellation rejected", "busy switch refused",
                       "A-to-B-to-A maintained one grant"))

        # An active grant outside the persisted current target is unaccounted;
        # binding is refused until the executor's real authorization is cleared.
        second_connection = service.connections[second.state.instance_id]
        orphan_grant = {"backend_id": "orphan-backend", "generation": 88, "active": True}
        second_connection.call("grant", expected=second.state.identity(),
                               authorization=orphan_grant)
        try:
            service.bind(first.state.instance_id)
            raise AssertionError("unaccounted residual grant did not block bind")
        except RuntimeError as exc:
            _assert("grant" in str(exc).lower() or "authorization" in str(exc).lower(),
                    "residual grant refusal had an unrelated cause: " + str(exc))
        _assert(first.state.authorization and first.state.authorization["active"],
                "residual grant refusal disturbed the current target")
        second_connection.call("revoke", backend_id="orphan-backend")
        checks.append("orphaned residual grant blocked bind")

        # Cause a real main-thread preflight timeout before network dispatch.
        # The timeout is transient, so the backend must leave the task queued
        # and retry after the fake main thread becomes responsive.
        probe_timeout = threading.Event()
        inject_timeout = {"active": True}
        with service._lock:
            preflight = _submit(service, target, "grid_create",
                                {"test_case": "preflight-timeout"}, "op-preflight-timeout",
                                model_epoch=2)
            first.state.pause_probe.set()
            preflight_original_call = first_connection.call

            def observe_preflight_timeout(kind, *args, **kwargs):
                try:
                    return preflight_original_call(kind, *args, **kwargs)
                except Exception:
                    if kind == "model_snapshot" and inject_timeout["active"]:
                        inject_timeout["active"] = False
                        probe_timeout.set()
                    raise

            first_connection.call = observe_preflight_timeout
        try:
            try:
                _wait_for(lambda: first.state.probe_entered.is_set(),
                          "pre-dispatch model probe to enter the fake ANSA main thread")
            except AssertionError as exc:
                current = _task_local(service, preflight["task_id"])
                raise AssertionError(
                    str(exc) + "; task=" + repr(current) + "; dispatch_blocked="
                    + repr(service.dispatch_blocked) + "; scheduler_error=" + repr(service.last_error)
                ) from exc
            _wait_for(lambda: probe_timeout.is_set(), "main-thread probe timeout response", timeout=7.0)
            after_timeout = _task_local(service, preflight["task_id"])
            _assert(after_timeout["state"] == "queued" and after_timeout["dispatch"] == "not_sent",
                    "transient preflight timeout incorrectly expired or dispatched the task: "
                    + repr(after_timeout))
            _assert(not any(item["test_case"] == "preflight-timeout" for item in first.state.executions),
                    "task executed before its preflight recovered")
        finally:
            first.state.probe_release.set()
            first.state.pause_probe.clear()
            first_connection.call = preflight_original_call
        recovered_preflight = _query_until_terminal(service, "op-preflight-timeout")
        _assert(recovered_preflight["state"] == "succeeded",
                "task did not execute after the transient main-thread timeout recovered")
        _assert(sum(item["test_case"] == "preflight-timeout" for item in first.state.executions) == 1,
                "preflight retry executed the model operation more than once")
        checks.append("pre-dispatch main-thread timeout stayed queued then recovered once")

        # Inject a genuine SQLite abort on the external task INSERT. New task
        # admission must stop while the old durable operation remains queryable.
        failure_operation = "op-external-store-failure"
        failed_request = {"test_case": "must-not-persist"}
        database = sqlite3.connect(root / "state.sqlite3")
        try:
            database.execute("""
                CREATE TRIGGER reject_task_insert BEFORE INSERT ON tasks
                BEGIN SELECT RAISE(ABORT, 'simulated external task-store failure'); END
            """)
            database.commit()
        finally:
            database.close()
        try:
            try:
                _submit(service, target, "grid_create", failed_request,
                        failure_operation, model_epoch=2)
                raise AssertionError("SQLite trigger did not abort task persistence")
            except sqlite3.Error as exc:
                _assert("simulated external task-store failure" in str(exc),
                        "task insert failed for an unexpected SQLite reason: " + str(exc))
            _assert(service.dispatch_blocked, "task-store failure did not stop dispatch/admission")
            failed_request_record = {
                "tool": "grid_create", "params": failed_request, "target": target,
                "model": {"epoch": 2}, "ttl_seconds": 300,
            }
            _assert(service.tasks.lookup(failure_operation, failed_request_record) is None,
                    "failed transaction left a partial operation mapping")
            try:
                _submit(service, target, "grid_create", {"test_case": "after-db-failure"},
                        "op-after-db-failure", model_epoch=2)
                raise AssertionError("backend admitted a new task after SQLite failure")
            except RuntimeError as exc:
                _assert("admission" in str(exc).lower() or "stopped" in str(exc).lower(),
                        "post-failure task was rejected for an unrelated reason: " + str(exc))
            historical = service.query_task(operation_id=response_lost_operation)
            _assert(historical["task_id"] == response_lost_task
                    and historical["state"] == "succeeded" and historical["acked"],
                    "SQLite write failure made a prior durable result unqueryable")
        finally:
            database = sqlite3.connect(root / "state.sqlite3")
            try:
                database.execute("DROP TRIGGER IF EXISTS reject_task_insert")
                database.commit()
            finally:
                database.close()
        checks.extend(("SQLite task insert failure blocks admission", "failed transaction leaves no mapping",
                       "prior task remains queryable after SQLite failure"))

        original_execution_total = len(first.state.executions) + len(second.state.executions)
        old_backend_id = service.backend_id
        service.close()
        service = None

        # A fresh backend ID may query old task history but cannot re-dispatch
        # under the old ANSA grant/session.
        restarted = service_module.Service(root)
        recovered = _wait_for(
            lambda: restarted.query_task(operation_id=response_lost_operation),
            "old operation lookup after backend restart",
        )
        _assert(recovered["task_id"] == response_lost_task,
                "backend restart created a new task for the old operation_id")
        _assert(recovered["state"] == "succeeded" and recovered["acked"],
                "backend restart did not recover the durable result")
        _assert(restarted.backend_id != old_backend_id, "backend restart reused backend_id")
        time.sleep(0.35)
        _assert(len(first.state.executions) + len(second.state.executions) == original_execution_total,
                "backend restart re-dispatched an old task")
        _assert(restarted.status()["active_target"] is None,
                "new backend silently adopted an old execution grant")
        checks.extend(("operation lookup survives backend restart", "no old-task re-dispatch"))
        return {"checks": checks, "execution_count": original_execution_total,
                "main_thread_execution_count": sum(
                    item["thread_id"] == bridge.state.main_id
                    for bridge in bridges for item in bridge.state.executions),
                "response_lost_task_id": response_lost_task}
    finally:
        if service is not None:
            service.close()
        if restarted is not None:
            restarted.close()
        for bridge in bridges:
            bridge.close()


def main():
    import argparse
    from ansa_mcp_plugin.backend.security import ensure_private_directory, ensure_safe_parent

    parser = argparse.ArgumentParser(description="Run the isolated task simulation in a private runtime")
    parser.add_argument("--runtime-home", type=Path, required=True)
    args = parser.parse_args()
    runtime_home = args.runtime_home.expanduser().resolve()
    ensure_safe_parent(runtime_home)
    ensure_private_directory(runtime_home)
    simulation_home = runtime_home / ("run-" + uuid.uuid4().hex)
    ensure_private_directory(simulation_home)
    verification = Path(__file__).resolve().parents[2] / "runtime" / "verification"
    verification.mkdir(parents=True, exist_ok=True)
    report_path = verification / ("stage-two-simulated-" + time.strftime("%Y%m%d-%H%M%S")
                                 + "-" + uuid.uuid4().hex[:8] + ".json")
    report = {"ok": False, "simulation": True,
              "note": "Socket, SQLite and executor journal are real; model APIs are mocked on fake ANSA main threads.",
              "checks": [], "report_path": str(report_path), "runtime_home": str(simulation_home)}
    try:
        with _isolated_acl_and_operations():
            outcome = _run_scenarios(simulation_home)
        report.update(ok=True, checks=outcome["checks"],
                      execution_count=outcome["execution_count"],
                      main_thread_execution_count=outcome["main_thread_execution_count"],
                      response_lost_task_id=outcome["response_lost_task_id"])
    except Exception as exc:
        report["error"] = type(exc).__name__ + ": " + str(exc)
        raise
    finally:
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
