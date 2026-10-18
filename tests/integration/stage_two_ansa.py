"""第二阶段历史双 ANSA 探针；主入口保留 9 项工具断言，不作为当前回归门槛。

辅助函数仍供后续探针复用；模型任务经过 MCP → 自研 TCP → 所属 ANSA 执行器。
官方 gRPC 仅用于独立测试实例的加载与回读。
"""
import argparse
import asyncio
from datetime import timedelta
import json
import inspect
import importlib.util
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import textwrap
import uuid

import ansa
import grpc
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from stage_one_ansa import DeadlineInterceptor, remote_basic, remote_start, remote_show_panel, remote_click_bind

ROOT = Path(__file__).resolve().parents[2]


def remote_seed(path):
    from ansa import base, constants
    node = base.CreateEntity(constants.NASTRAN, "GRID", {"X1": 1., "X2": 2., "X3": 3.})
    assert node is not None
    assert base.SaveAs(path) == 0
    return {"id": node._id, "usable": node.is_usable()}


def remote_capabilities():
    from ansa import base, constants
    return {"version": constants.app_version, "version_id": constants.app_version_id,
            "windows": [{"name": base.GetAnsaWindowName(w), "models": len(base.GetAnsaModelsOfAnsaWindow(w))}
                        for w in base.GetAllAnsaWindows()]}


def remote_legacy_rollback(action, home, expected_legacy_home):
    import builtins
    import os
    from pathlib import Path
    from ansa_mcp_sum import bridge_runtime, plugin
    from ansa_mcp_bridge.bootstrap import start
    assert Path(os.environ["ANSA_MCP_SUM_HOME"]).resolve() == Path(expected_legacy_home).resolve()
    assert Path(plugin.CONFIG.stop_file).resolve().is_relative_to(Path(expected_legacy_home).resolve())
    if action == "start_legacy":
        builtins._ansa_mcp_plugin_bridge.close()
        assert bridge_runtime.start_polling(force=True)
        return bridge_runtime.snapshot()
    prior = bridge_runtime.snapshot()
    assert prior["active"] and not prior["busy"]
    try:
        try:
            start(home, show=False)
        except RuntimeError as exc:
            conflict = str(exc)
        else:
            raise AssertionError("New bridge was enabled while legacy polling was active")
    finally:
        bridge_runtime.stop_polling("isolated stage-two rollback acceptance")
    assert bridge_runtime.is_stopped() and not bridge_runtime.is_active()
    restored = start(home, show=False)
    return {"legacy_before": prior, "conflict": conflict, "legacy_after": bridge_runtime.snapshot(),
            "new_bridge": restored.state.probe()}


def remote_oracle(node_id=None, change=None, action=None, path=None):
    import builtins
    from ansa import base, constants, session
    bridge = builtins._ansa_mcp_plugin_bridge
    old = bridge.state._witness
    if action == "open":
        base.Open(path)
    elif action == "new":
        session.New("discard")
    elif action == "reset":
        base.ClearIncludeConfiguration(2)
    elif action == "mutex":
        from ansa_mcp_bridge.bootstrap import Bridge
        try:
            Bridge(path)
        except RuntimeError as exc:
            return {"rejected": str(exc)}
        raise AssertionError("Different runtime acquired the same process executor")
    elif action == "close":
        bridge.close()
        return {"closed": True}
    node = base.GetEntity(constants.NASTRAN, "GRID", node_id) if node_id else None
    if change is not None:
        base.SetEntityCardValues(constants.NASTRAN, node, {"X1": change[0], "X2": change[1], "X3": change[2]})
    return {"snapshot": bridge.state.probe(), "old_witness_usable": old.is_usable() if old else False,
            "nodes": [n._id for n in base.CollectEntities(constants.NASTRAN, None, "GRID")],
            "card": base.GetEntityCardValues(constants.NASTRAN, node, ("X1", "X2", "X3")) if node else None}


def rpc(port, fn, *args):
    # ANSA can cache an older script function table across repeated remote names.
    # A fresh source/module/function name forces an independent test invocation.
    name = "probe_" + uuid.uuid4().hex
    source = textwrap.dedent(inspect.getsource(fn)).replace("def " + fn.__name__ + "(", "def " + name + "(", 1)
    directory = ROOT / "runtime/verification/rpc-sources"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (name + ".py")
    path.write_text(source, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    connection = ansa.connect(host="127.0.0.1", port=port)
    try:
        return ansa.execute_function(getattr(module, name), *args)
    finally:
        ansa.disconnect(connection)


def record(report, name, value):
    report["cases"].append({"name": name, "ok": True, "value": value})
    (Path(report["run_dir"]) / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"case": name, "ok": True}, ensure_ascii=True), flush=True)


async def session_check(home, other_home, report, run, identities, panel_check):
    parameters = StdioServerParameters(command=sys.executable,
        args=["-B", "-m", "ansa_mcp_plugin", "--runtime-home", str(home)],
        env={**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"})
    saved_request = None
    async def one_session(restart=False):
        nonlocal saved_request
        with (run / ("restart.stderr.log" if restart else "backend.stderr.log")).open("w", encoding="utf-8") as log:
            async with stdio_client(parameters, errlog=log) as (reader, writer):
                async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=25)) as client:
                    await client.initialize()
                    async def call(name, arguments=None, error=False):
                        answer = await client.call_tool(name, arguments or {})
                        data = answer.structuredContent
                        assert data is not None, str(answer.content)
                        if not error:
                            assert not answer.isError, data
                        return data
                    async def status():
                        return (await call("ansa_connection_status"))["data"]
                    async def finish(submitted, expected="succeeded"):
                        task_id = submitted["task_id"]
                        for _ in range(80):
                            value = await call("ansa_task_query", {"task_id": task_id}, error=True)
                            if value["status"] in {"succeeded", "failed", "expired", "cancelled"}:
                                assert value["status"] == expected, value
                                return value
                            await asyncio.sleep(.15)
                        raise TimeoutError("Task did not converge: " + task_id)
                    async def switch(index, visible):
                        identity = identities[index]
                        panel_task = None
                        if visible:
                            panel_task = asyncio.create_task(asyncio.to_thread(rpc, identity["port"], remote_show_panel))
                            (run / "ui_stage.json").write_text(json.dumps({"stage": "switch_" + str(index), "pid": identity["pid"], "window_title": ("a" if index == 0 else "b") + "-baseline.ansa"}), encoding="utf-8")
                        else:
                            rpc(identity["port"], remote_click_bind)
                        active = None
                        for _ in range(1200 if visible else 80):
                            info = await status()
                            active = info["active_target"]
                            if active and active["instance_id"] == identity["instance_id"]:
                                break
                            await asyncio.sleep(.25)
                        assert active and active["instance_id"] == identity["instance_id"], info
                        if panel_task:
                            await panel_task
                        snapshots = [rpc(i["port"], remote_oracle)["snapshot"] for i in identities]
                        grants = [s["authorization"] for s in snapshots if s["authorization"] and s["authorization"]["active"]]
                        assert len(grants) == 1 and grants[0]["generation"] == active["generation"]
                        snapshot = (await call("ansa_model_snapshot", {"expected_target": active}))["data"]
                        assert snapshot["pid"] == identity["pid"] and snapshot["main_thread"]
                        assert snapshot["model"]["write_enabled"]
                        record(report, ("restarted_" if restart else "") + "switch_to_" + str(index) + "_generation_" + str(active["generation"]), {"target": active, "snapshots": snapshots})
                        return active, {"epoch": snapshot["model"]["epoch"]}
                    info = await status()
                    assert len(info["instances"]) == 2
                    listed = await client.list_tools()
                    assert len(listed.tools) == 9 and all(t.outputSchema for t in listed.tools)
                    if restart:
                        assert info["active_target"] is None and info["recovery_required"]
                        original = await call("ansa_grid_create", saved_request)
                        assert original["task_id"] == report["original_create"]["task_id"]
                        original_result = await finish(original)
                        active, model = await switch(0, False)
                        assert active["backend_id"] != saved_request["expected_target"]["backend_id"]
                        assert (await call("ansa_grid_create", saved_request))["task_id"] == original["task_id"]
                        assert len(rpc(identities[0]["port"], remote_oracle)["nodes"]) == 1
                        record(report, "backend_restart_idempotency_preserves_original_task", original_result)
                        return
                    record(report, "two_candidates_one_stdio_schema", {"instances": info["instances"], "tools": [t.name for t in listed.tools]})
                    active, model = await switch(0, panel_check)
                    saved_request = {"operation_id": str(uuid.uuid4()), "expected_target": active, "expected_model": model, "position": [4., 5., 6.]}
                    created = await call("ansa_grid_create", saved_request)
                    duplicate = await call("ansa_grid_create", saved_request)
                    assert duplicate["task_id"] == created["task_id"]
                    completed = await finish(created)
                    node_id = completed["data"]["result"]["entity"]["id"]
                    oracle = rpc(identities[0]["port"], remote_oracle, node_id)
                    assert oracle["card"] == {"X1": 4., "X2": 5., "X3": 6.} and len(oracle["nodes"]) == 2
                    report["original_create"] = completed
                    record(report, "MCP_create_duplicate_independent_readback", {"result": completed, "oracle": oracle})
                    changed_request = {**saved_request, "position": [7., 8., 9.]}
                    rejected = await call("ansa_grid_create", changed_request, error=True)
                    assert rejected["error"] and rejected["task_id"] is None
                    record(report, "same_operation_different_request_rejected", rejected)
                    base_request = {"expected_target": active, "expected_model": model, "entity_id": node_id}
                    updated = await finish(await call("ansa_grid_update", {**base_request, "operation_id": str(uuid.uuid4()), "expected_position": [4., 5., 6.], "position": [7., 8., 9.]}))
                    assert rpc(identities[0]["port"], remote_oracle, node_id)["card"] == {"X1": 7., "X2": 8., "X3": 9.}
                    record(report, "MCP_update_independent_readback", updated)
                    rpc(identities[0]["port"], remote_oracle, node_id, [12., 13., 14.])
                    stale = await finish(await call("ansa_grid_update", {**base_request, "operation_id": str(uuid.uuid4()), "expected_position": [7., 8., 9.], "position": [15., 16., 17.]}), "failed")
                    assert rpc(identities[0]["port"], remote_oracle, node_id)["card"] == {"X1": 12., "X2": 13., "X3": 14.}
                    record(report, "manual_edit_stale_entity_rejected", stale)
                    deleted = await finish(await call("ansa_grid_delete", {**base_request, "operation_id": str(uuid.uuid4()), "expected_position": [12., 13., 14.]}))
                    assert rpc(identities[0]["port"], remote_oracle, node_id)["card"] is None
                    record(report, "MCP_delete_absence_readback", deleted)
                    save_request = {"operation_id": str(uuid.uuid4()), "expected_target": active, "expected_model": model, "path": str(run / "mcp-saved.ansa")}
                    saved = await finish(await call("ansa_model_save", save_request))
                    assert (run / "mcp-saved.ansa").stat().st_size > 0
                    record(report, "MCP_SaveAs_creates_copy", saved)
                    overwrite = await finish(await call("ansa_model_save", {**save_request, "operation_id": str(uuid.uuid4())}), "failed")
                    record(report, "overwrite_requires_explicit_intent", overwrite)
                    mutex = rpc(identities[0]["port"], remote_oracle, None, None, "mutex", str(other_home))
                    assert "already" in mutex["rejected"].lower()
                    record(report, "different_runtime_same_process_mutex_rejected", mutex)
                    active_b, model_b = await switch(1, panel_check)
                    old_context = await call("ansa_grid_read", {**base_request, "operation_id": str(uuid.uuid4())}, error=True)
                    assert old_context["status"] == "failed" and old_context["task_id"] is None
                    b_read = await finish(await call("ansa_grid_read", {"operation_id": str(uuid.uuid4()), "expected_target": active_b, "expected_model": model_b, "entity_id": 1}))
                    assert b_read["data"]["result"]["executor"]["pid"] == identities[1]["pid"]
                    record(report, "B_model_thread_readback_old_target_rejected", b_read)
                    active, model = await switch(0, panel_check)
                    assert (await call("ansa_grid_create", saved_request))["task_id"] == created["task_id"]
                    before = rpc(identities[0]["port"], remote_oracle)
                    changed = rpc(identities[0]["port"], remote_oracle, None, None, "open", str(run / "a-baseline.ansa"))
                    assert not changed["old_witness_usable"] and changed["snapshot"]["model"]["epoch"] > before["snapshot"]["model"]["epoch"]
                    samepath = rpc(identities[0]["port"], remote_oracle, None, None, "open", str(run / "a-baseline.ansa"))
                    assert not samepath["old_witness_usable"] and samepath["snapshot"]["model"]["epoch"] > changed["snapshot"]["model"]["epoch"]
                    stale_model = await call("ansa_grid_create", {"operation_id": str(uuid.uuid4()), "expected_target": active, "expected_model": model, "position": [20., 21., 22.]}, error=True)
                    assert stale_model["task_id"] is None and stale_model["status"] == "failed"
                    record(report, "same_path_API_Open_invalidates_retained_witness_and_old_model", {"before": before, "after": samepath, "rejection": stale_model})
                    if panel_check:
                        (run / "ui_stage.json").write_text(json.dumps({"stage": "same_path_open", "pid": identities[0]["pid"], "window_title": "a-baseline.ansa", "path": str(run / "a-baseline.ansa")}), encoding="utf-8")
                        previous_epoch = samepath["snapshot"]["model"]["epoch"]
                        deadline = time.monotonic() + 900
                        while True:
                            observed = (await call("ansa_model_snapshot", {"expected_target": active}))["data"]
                            if observed["model"]["epoch"] > previous_epoch:
                                break
                            if time.monotonic() > deadline:
                                raise TimeoutError("GUI same-path Open was not completed")
                            await asyncio.sleep(.5)
                        gui = rpc(identities[0]["port"], remote_oracle)
                        assert gui["snapshot"]["model"]["epoch"] > previous_epoch
                        record(report, "GUI_same_path_Open_invalidates_context", gui)
                    empty = rpc(identities[0]["port"], remote_oracle, None, None, "new")
                    assert not empty["snapshot"]["model"]["write_enabled"] and not empty["old_witness_usable"]
                    record(report, "New_empty_model_closes_write_gate", empty)
                    reopened = rpc(identities[0]["port"], remote_oracle, None, None, "open", str(run / "a-baseline.ansa"))
                    assert reopened["snapshot"]["model"]["write_enabled"]
                    reset = rpc(identities[0]["port"], remote_oracle, None, None, "reset")
                    assert not reset["snapshot"]["model"]["write_enabled"] and not reset["old_witness_usable"]
                    record(report, "database_reset_invalidates_witness", reset)
                    rpc(identities[0]["port"], remote_oracle, None, None, "open", str(run / "a-baseline.ansa"))
    await one_session()
    await one_session(restart=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ansa-root", type=Path, required=True)
    parser.add_argument("--runtime-home", type=Path, required=True)
    parser.add_argument("--panel-check", action="store_true")
    args = parser.parse_args()
    run = ROOT / "runtime/verification" / ("stage-two-" + uuid.uuid4().hex[:8])
    run.mkdir(parents=True)
    report = {"run_dir": str(run), "cases": [], "runtime_home": str(args.runtime_home)}
    from ansa_mcp_plugin.backend.service import Service
    other_home = args.runtime_home.with_name(args.runtime_home.name + "-other")
    for home in (args.runtime_home, other_home):
        seed = Service(home)
        seed.close()
    original = grpc.insecure_channel
    def bounded(target, options=None, **kwargs):
        return grpc.intercept_channel(original(target, options=[*(options or []), ("grpc.enable_http_proxy", 0)], **kwargs), DeadlineInterceptor(600 if args.panel_check else 20))
    grpc.insecure_channel = bounded
    processes, identities, logs = [], [], []
    try:
        for label, port in (("a", 50051), ("b", 50052)):
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", port))
            translator = run / (label + "_isolated_transl.py")
            translator.write_text("# Isolated phase-two acceptance loader.\n")
            command = [str(args.ansa_root / "ansa64.bat"), "-grpcport", str(port), "-grpchost", "127.0.0.1",
                       "-changedir", str(Path.home()), "-translfp", str(translator), "-nolauncher", "--skip-release-highlights"]
            if not args.panel_check or label == "b":
                command.append("--iconify")
            env = {**os.environ, "ANSA_SRV": "5280@localhost", "ANSA_MCP_SUM_HOME": str(run / (label + "-legacy-runtime")), "ANSA_MCP_SUM_AUTO_POLL": "0", "PYTHONDONTWRITEBYTECODE": "1"}
            log = (run / (label + ".ansa.log")).open("wb")
            logs.append(log)
            processes.append(subprocess.Popen(command, cwd=run, env=env, stdout=log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW))
            ready = original("127.0.0.1:" + str(port), options=[("grpc.enable_http_proxy", 0)])
            try:
                grpc.channel_ready_future(ready).result(timeout=75)
            finally:
                ready.close()
            basic = rpc(port, remote_basic)
            assert str(translator) in basic["program_args"] and str(port) in basic["program_args"] and basic["main_thread"]
            # Record ownership before any model operation or stop request.
            identity = {"pid": basic["pid"], "port": port, "program_args": basic["program_args"]}
            identities.append(identity)
            fixture = run / (label + "-baseline.ansa")
            seed = rpc(port, remote_seed, str(fixture))
            assert seed["usable"] and fixture.stat().st_size > 0
            shutil.copyfile(fixture, run / (label + "-backup.ansa"))
            started = rpc(port, remote_start, str(ROOT), str(args.runtime_home), str(run / (label + "-bootstrap.txt")))
            identity.update(started, port=port)
            identity["capabilities"] = rpc(port, remote_capabilities)
            record(report, label + "_owned_GUI_fixture_backup_and_bridge", identity)
        assert identities[0]["pid"] != identities[1]["pid"]
        asyncio.run(session_check(args.runtime_home, other_home, report, run, identities, args.panel_check))
        legacy_home = str(run / "a-legacy-runtime")
        legacy = rpc(50051, remote_legacy_rollback, "start_legacy", str(args.runtime_home), legacy_home)
        assert legacy["active"]
        restored = rpc(50051, remote_legacy_rollback, "restore_new", str(args.runtime_home), legacy_home)
        assert "legacy" in restored["conflict"].lower() and restored["new_bridge"]["main_thread"]
        record(report, "independent_legacy_rollback_conflict_and_new_restore", restored)
        report["success"] = True
    except Exception as exc:
        report["success"] = False
        report["fatal_error"] = repr(exc)
    finally:
        cleanup = []
        for identity in identities:
            connection = None
            try:
                connection = ansa.connect(host="127.0.0.1", port=identity["port"])
                basic = ansa.execute_function(remote_basic)
                assert basic["pid"] == identity["pid"] and basic["program_args"] == identity["program_args"]
                ansa.stop(connection)
                connection = None
                cleanup.append({"pid": identity["pid"], "stopped_owned_process": True})
            except Exception as exc:
                cleanup.append({"pid": identity["pid"], "error": repr(exc)})
            finally:
                if connection:
                    ansa.disconnect(connection)
        for process in processes:
            try:
                process.wait(timeout=12)
            except subprocess.TimeoutExpired:
                cleanup.append({"launcher_pid": process.pid, "still_running": True})
        for log in logs:
            log.close()
        grpc.insecure_channel = original
        report["cleanup"] = cleanup
        (run / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(run / "report.json"), "success": report["success"], "cases": len(report["cases"]), "error": report.get("fatal_error")}), flush=True)
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
