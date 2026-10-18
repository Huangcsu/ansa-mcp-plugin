"""Finish only the remaining real GUI, restart, and rollback checks.

Official gRPC is a test loader/oracle; formal requests use MCP and custom TCP.
The GUI wait observes model_epoch directly and does not require a marker file.
"""

import argparse
import asyncio
from datetime import timedelta
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time

import ansa
import grpc
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from stage_two_ansa import (ROOT, DeadlineInterceptor, record, rpc, remote_basic,
                            remote_seed, remote_start, remote_click_bind,
                            remote_oracle, remote_legacy_rollback)


def remote_gui_witness(action):
    import builtins
    bridge = builtins._ansa_mcp_plugin_bridge
    if action == "retain":
        builtins._phase_two_gui_witness = bridge.state._witness
    old = builtins._phase_two_gui_witness
    return {"old_usable": bool(old and old.is_usable()), "snapshot": bridge.state.probe()}


async def verify(home, run, identity, previous, report):
    original = previous["original_create"]
    retry = {"operation_id": original["operation_id"],
             "expected_target": original["target"], "expected_model": original["model"],
             "position": [4., 5., 6.]}
    parameters = StdioServerParameters(command=sys.executable,
        args=["-B", "-m", "ansa_mcp_plugin", "--runtime-home", str(home)],
        env={**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"})
    first_backend = None
    for restart in (False, True):
        with (run / ("restart.stderr.log" if restart else "backend.stderr.log")).open("w", encoding="utf-8") as log:
            async with stdio_client(parameters, errlog=log) as (reader, writer):
                async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=25)) as client:
                    await client.initialize()

                    async def call(name, arguments=None):
                        answer = await client.call_tool(name, arguments or {})
                        assert not answer.isError and answer.structuredContent is not None, answer
                        return answer.structuredContent

                    info = (await call("ansa_connection_status"))["data"]
                    assert info["active_target"] is None and info["recovery_required"], info
                    old = await call("ansa_grid_create", retry)
                    assert old["task_id"] == original["task_id"] and old["status"] == "succeeded"
                    rpc(identity["port"], remote_click_bind)
                    for _ in range(100):
                        info = (await call("ansa_connection_status"))["data"]
                        active = info["active_target"]
                        if active and active["instance_id"] == identity["instance_id"]:
                            break
                        await asyncio.sleep(.2)
                    assert active and active["instance_id"] == identity["instance_id"], info
                    assert (await call("ansa_grid_create", retry))["task_id"] == original["task_id"]
                    oracle = rpc(identity["port"], remote_oracle)
                    assert oracle["nodes"] == [1] and oracle["snapshot"]["main_thread"]
                    if restart:
                        assert active["backend_id"] != first_backend
                        record(report, "real_backend_restart_original_task_no_replay", {"target": active, "original": old, "oracle": oracle})
                        continue
                    first_backend = active["backend_id"]
                    record(report, "previous_run_history_recovered_after_executor_exit", {"target": active, "original": old})
                    before = (await call("ansa_model_snapshot", {"expected_target": active}))["data"]
                    assert before["model"]["write_enabled"]
                    retained = rpc(identity["port"], remote_gui_witness, "retain")
                    assert retained["old_usable"]
                    fixture = run / "phase2-followup.ansa"
                    stage = {"stage": "same_path_open", "pid": identity["pid"], "path": str(fixture),
                             "window_title": "phase2-followup.ansa", "before_epoch": before["model"]["epoch"]}
                    (run / "ui_stage.json").write_text(json.dumps(stage), encoding="utf-8")
                    print(json.dumps({"GUI_action_required": stage}), flush=True)
                    deadline = time.monotonic() + 900
                    while True:
                        after = (await call("ansa_model_snapshot", {"expected_target": active}))["data"]
                        if after["model"]["epoch"] > before["model"]["epoch"]:
                            break
                        if time.monotonic() > deadline:
                            raise TimeoutError("GUI same-path Open not observed within 15 minutes")
                        await asyncio.sleep(.5)
                    independent = rpc(identity["port"], remote_gui_witness, "check")
                    assert not independent["old_usable"] and after["model"]["write_enabled"]
                    assert Path(after["model"]["database"]).resolve() == fixture.resolve()
                    record(report, "GUI_same_path_Open_invalidates_context", {"before": before, "after": after, "independent": independent})
                    empty = rpc(identity["port"], remote_oracle, None, None, "new")
                    assert not empty["snapshot"]["model"]["write_enabled"] and not empty["old_witness_usable"]
                    record(report, "New_empty_model_closes_write_gate", empty)
                    reopened = rpc(identity["port"], remote_oracle, None, None, "open", str(fixture))
                    assert reopened["snapshot"]["model"]["write_enabled"]
                    reset = rpc(identity["port"], remote_oracle, None, None, "reset")
                    assert not reset["snapshot"]["model"]["write_enabled"] and not reset["old_witness_usable"]
                    record(report, "database_reset_invalidates_witness", reset)
                    rpc(identity["port"], remote_oracle, None, None, "open", str(fixture))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ansa-root", type=Path, required=True)
    parser.add_argument("--previous-report", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    previous = json.loads(args.previous_report.read_text(encoding="utf-8"))
    home, run = Path(previous["runtime_home"]), args.run_dir.resolve()
    if not run.is_relative_to(ROOT / "runtime/verification") or run.exists():
        raise ValueError("Use a new verification directory inside this project")
    run.mkdir(parents=True)
    report = {"run_dir": str(run), "runtime_home": str(home), "previous_report": str(args.previous_report.resolve()), "cases": []}
    port, identity, process = 50051, None, None
    original_channel = grpc.insecure_channel
    def bounded(target, options=None, **kwargs):
        return grpc.intercept_channel(original_channel(target, options=[*(options or []), ("grpc.enable_http_proxy", 0)], **kwargs), DeadlineInterceptor(20))
    grpc.insecure_channel = bounded
    log = (run / "ansa.log").open("wb")
    try:
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", port))
        translator = run / "isolated_transl.py"
        translator.write_text("# Isolated phase-two follow-up loader.\n")
        command = [str(args.ansa_root / "ansa64.bat"), "-grpcport", str(port), "-grpchost", "127.0.0.1",
                   "-changedir", str(Path.home()), "-translfp", str(translator), "-nolauncher", "--skip-release-highlights"]
        legacy_home = str(run / "legacy-runtime")
        env = {**os.environ, "ANSA_SRV": "5280@localhost", "ANSA_MCP_SUM_HOME": legacy_home,
               "ANSA_MCP_SUM_AUTO_POLL": "0", "PYTHONDONTWRITEBYTECODE": "1"}
        process = subprocess.Popen(command, cwd=run, env=env, stdout=log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
        ready = original_channel("127.0.0.1:" + str(port), options=[("grpc.enable_http_proxy", 0)])
        try:
            grpc.channel_ready_future(ready).result(timeout=75)
        finally:
            ready.close()
        basic = rpc(port, remote_basic)
        assert str(translator) in basic["program_args"] and str(port) in basic["program_args"] and basic["main_thread"]
        identity = {"pid": basic["pid"], "port": port, "program_args": basic["program_args"]}
        fixture = run / "phase2-followup.ansa"
        seed = rpc(port, remote_seed, str(fixture))
        assert seed["usable"] and fixture.stat().st_size > 0
        shutil.copyfile(fixture, run / "phase2-backup.ansa")
        identity.update(rpc(port, remote_start, str(ROOT), str(home), str(run / "bootstrap.txt")), port=port)
        record(report, "owned_GUI_fixture_backup_and_bridge", identity)
        asyncio.run(verify(home, run, identity, previous, report))
        legacy = rpc(port, remote_legacy_rollback, "start_legacy", str(home), legacy_home)
        assert legacy["active"] and not legacy["busy"]
        restored = rpc(port, remote_legacy_rollback, "restore_new", str(home), legacy_home)
        assert "legacy" in restored["conflict"].lower() and restored["new_bridge"]["main_thread"]
        record(report, "independent_legacy_rollback_conflict_and_new_restore", restored)
        report["success"] = True
    except Exception as exc:
        report["success"], report["fatal_error"] = False, repr(exc)
    finally:
        cleanup = []
        if identity:
            connection = None
            try:
                basic = rpc(port, remote_basic)
                assert basic["pid"] == identity["pid"] and basic["program_args"] == identity["program_args"]
                connection = ansa.connect(host="127.0.0.1", port=port)
                ansa.stop(connection)
                connection = None
                cleanup.append({"pid": identity["pid"], "stopped_owned_process": True})
            except Exception as exc:
                cleanup.append({"pid": identity["pid"], "error": repr(exc)})
            finally:
                if connection:
                    ansa.disconnect(connection)
        if process:
            try:
                process.wait(timeout=12)
            except subprocess.TimeoutExpired:
                cleanup.append({"launcher_pid": process.pid, "still_running": True})
        log.close()
        grpc.insecure_channel = original_channel
        report["cleanup"] = cleanup
        (run / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(run / "report.json"), "success": report["success"], "cases": len(report["cases"]), "error": report.get("fatal_error")}), flush=True)
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
