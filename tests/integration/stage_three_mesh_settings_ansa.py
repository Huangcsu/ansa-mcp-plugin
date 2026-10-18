"""在唯一归属的 ANSA GUI 中验证会话网格设置，经 MCP stdio 和自研 TCP 执行。"""

import argparse
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import traceback
import uuid

import stage_three_ansa as stage


def remote_initialize_visible_fixture(expected_pid, expected_arguments, seed_path, seed_id):
    import builtins
    import os
    import threading
    from pathlib import Path
    from ansa import base, constants, session
    if (os.getpid() != expected_pid or list(session.ProgramArguments()) != expected_arguments
            or threading.current_thread() is not threading.main_thread()):
        raise RuntimeError("Visible fixture preparation is not on the owned ANSA main thread")
    if not Path(seed_path).is_file() or builtins._ansa_mcp_plugin_bridge.state.journal.busy():
        raise RuntimeError("Visible fixture preparation requires the saved baseline and no pending tasks")
    # The common seed helper can save backend data while ANSA is on its welcome
    # page. Initialize the owned MDI explicitly, then reopen its saved baseline
    # before binding or changing settings; retain both original fixture copies.
    session.New("discard")
    status = base.Open(seed_path)
    nodes = base.CollectEntities(constants.NASTRAN, None, "GRID")
    if (status != 0 or Path(base.DataBaseName()).resolve() != Path(seed_path).resolve()
            or len(nodes) != 1 or nodes[0]._id != seed_id or not nodes[0].is_usable()):
        raise RuntimeError("The owned visible baseline was not opened as expected")
    return {"open_code": status, "model_path": base.DataBaseName(), "seed_entity_id": nodes[0]._id}


def remote_settings_readback(expected_pid, expected_arguments):
    import os
    import threading
    from ansa import base, session
    if (os.getpid() != expected_pid or list(session.ProgramArguments()) != expected_arguments
            or threading.current_thread() is not threading.main_thread()):
        raise RuntimeError("Settings oracle is not on the owned ANSA main thread")
    deck = base.CurrentDeck()
    grid = base.CollectEntities(deck, None, "GRID")
    return {
        "pid": os.getpid(), "deck": deck, "model_path": base.DataBaseName(),
        "settings": base.BCSettingsGetValues(("mesh_change_option", "element_type", "element_order", "target_element_length")),
        "entities": {
            "grid": [{"id": node._id, "fields": base.GetEntityCardValues(deck, node, ("X1", "X2", "X3"))} for node in grid],
            "shell_ids": sorted(item._id for item in base.CollectEntities(deck, None, "SHELL")),
            "face_ids": sorted(item._id for item in base.CollectEntities(deck, None, "FACE")),
        },
    }


def remote_restore_settings(expected_pid, expected_arguments, backup, expected_settings):
    import os
    import threading
    from ansa import base, mesh, session
    if (os.getpid() != expected_pid or list(session.ProgramArguments()) != expected_arguments
            or threading.current_thread() is not threading.main_thread()):
        raise RuntimeError("Settings restoration is not on the owned ANSA main thread")
    status = mesh.ReadMeshParams(backup)
    mode_status = base.BCSettingsSetValues({"mesh_change_option": expected_settings["mesh_change_option"]})
    actual = base.BCSettingsGetValues(tuple(expected_settings))
    if status != 1 or mode_status != 0 or actual != expected_settings:
        raise RuntimeError("Original test settings were not restored; retain backup and inspect")
    return {"file_restore": status, "mode_restore": mode_status, "settings": actual}


async def verify(run, instructions, identity, helpers, report, *, wait_ui):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    one, two, _original = helpers
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-B", "-m", "ansa_mcp_plugin", "--runtime-home", instructions["runtime_home"]],
        env={**os.environ, "PYTHONPATH": str(stage.PROJECT_ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"},
    )

    def record(name, evidence):
        report["cases"].append({"name": name, "ok": True, "evidence": evidence})
        stage._json_write(run / "settings-report.json", report)
        print(json.dumps({"case": name, "ok": True}, ensure_ascii=True), flush=True)

    def oracle():
        return two.rpc(identity["port"], remote_settings_readback, identity["pid"], identity["program_args"])

    @asynccontextmanager
    async def backend(log_name):
        with (run / log_name).open("w", encoding="utf-8") as log:
            async with stdio_client(parameters, errlog=log) as (reader, writer):
                async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=30)) as client:
                    await client.initialize()
                    h = stage.McpHarness(client, report, run)
                    for _ in range(100):
                        status = await h.call("ansa_connection_status")
                        if any(item["instance_id"] == identity["instance_id"] for item in status["data"].get("instances", [])):
                            break
                        await asyncio.sleep(.1)
                    await stage._bind_for_run(h, identity, one, two, identity["port"])
                    yield h

    before = oracle()
    report["initial_oracle"] = before
    async with backend("settings-backend.stderr.log") as h:
        record("registered_tool_schemas", await h.list_schema())
        report["target"] = h.target
        read = await h.direct_result("ansa_mesh_settings", {"action": "read"})
        assert read["read_only"] and read["settings"] == before["settings"] and oracle() == before
        record("settings_read_has_no_model_or_setting_effects", {"read": read, "independent": before})

        incomplete = await h.call("ansa_mesh_settings", {
            "operation_id": "missing-precondition-" + uuid.uuid4().hex,
            "expected_target": h.target, "expected_model": h.model,
            "action": "update", "settings": {"target_length": 3.25},
        }, allow_error=True)
        assert not incomplete["ok"] and not incomplete.get("task_id") and oracle() == before
        boolean = await h.client.call_tool("ansa_mesh_settings", {
            "operation_id": "invalid-length-" + uuid.uuid4().hex,
            "expected_target": h.target, "expected_model": h.model, "action": "update",
            "expected_settings": before["settings"], "settings": {"target_length": True},
        })
        assert boolean.isError and oracle() == before
        explicit_null = await h.client.call_tool("ansa_mesh_settings", {
            "operation_id": "invalid-null-" + uuid.uuid4().hex,
            "expected_target": h.target, "expected_model": h.model, "action": "update",
            "expected_settings": before["settings"], "settings": {"target_length": None, "element_type": "tria"},
        })
        assert explicit_null.isError and oracle() == before
        stale = dict(before["settings"], element_type="stale-value")
        refused = await h.direct_result("ansa_mesh_settings", {
            "action": "update", "expected_settings": stale, "settings": {"target_length": 3.25},
        }, expected="failed")
        assert oracle() == before
        record("missing_invalid_and_stale_preconditions_rejected_before_changes", {
            "missing": incomplete, "invalid_boolean_length_rejected": boolean.isError,
            "explicit_null_rejected": explicit_null.isError, "stale_task": refused,
        })

        operation = "settings-update-" + uuid.uuid4().hex
        arguments = {"operation_id": operation, "expected_target": h.target, "expected_model": h.model,
                     "action": "update", "expected_settings": before["settings"],
                     "settings": {"target_length": 3.25, "element_type": "tria", "element_order": "1st Order"}}
        submitted = await h.call("ansa_mesh_settings", arguments)
        completed = await h.wait_task(submitted["task_id"])
        updated = completed["data"]["result"]
        applied = oracle()
        assert updated["settings_after"] == applied["settings"]
        assert applied["settings"]["element_type"] == "Tria" and float(applied["settings"]["target_element_length"]) == 3.25
        assert applied["settings"]["element_order"] == "1st Order"
        assert applied["settings"]["mesh_change_option"] == before["settings"]["mesh_change_option"]
        assert applied["entities"] == before["entities"] and applied["model_path"] == before["model_path"]
        assert Path(updated["parameter_backup"]).is_file() and updated["persisted_in_session"] and not updated["defaults_file_written"]
        report["initial_parameter_backup"] = updated["parameter_backup"]
        repeated = await h.call("ansa_mesh_settings", arguments)
        assert repeated["task_id"] == submitted["task_id"] and oracle() == applied
        record("session_update_independent_readback_backup_and_idempotency", {
            "task_id": submitted["task_id"], "result": updated, "independent": applied,
        })

    async with backend("settings-restarted-backend.stderr.log") as h:
        queried = await h.call("ansa_task_query", {"task_id": submitted["task_id"]})
        repeated = await h.call("ansa_mesh_settings", arguments)
        assert queried["status"] == "succeeded" and repeated["task_id"] == submitted["task_id"] and oracle() == applied
        read = await h.direct_result("ansa_mesh_settings", {"action": "read"})
        assert read["settings"] == applied["settings"]
        record("backend_restart_preserves_settings_and_original_operation", {"original_task": queried, "read": read})

        changed = await h.direct_result("ansa_mesh_settings", {
            "action": "update", "expected_settings": applied["settings"],
            "settings": {"element_type": "quad", "element_order": "2nd Order"},
        })
        actual = oracle()
        assert actual["settings"] == changed["settings_after"]
        assert actual["settings"]["element_type"] == "Quad" and actual["settings"]["element_order"] == "2nd Order"
        assert actual["settings"]["target_element_length"] == applied["settings"]["target_element_length"]
        assert actual["settings"]["mesh_change_option"] == before["settings"]["mesh_change_option"]
        assert actual["entities"] == before["entities"]
        record("partial_settings_update_preserves_length_mode_and_entities", {"result": changed, "independent": actual})

        if wait_ui:
            marker = run / "settings-ui-confirmation.json"
            if marker.exists():
                raise RuntimeError("UI confirmation already exists; do not reuse stale visual evidence")
            stage._json_write(run / "settings-ui-instruction.json", {
                "identity": identity, "window_file": Path(identity["seed_path"]).name,
                "expected_settings": actual["settings"],
                "instruction": "核对所属主窗口的 Mesh Parameters 显示 Quad、2nd Order 和目标长度 3.25；关闭参数窗口后记录本轮确认。",
            })
            print(json.dumps({"status": "waiting_for_settings_ui", "run_dir": str(run)}, ensure_ascii=True), flush=True)
            deadline = asyncio.get_running_loop().time() + stage.PANEL_WAIT_SECONDS
            while not marker.is_file():
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError("UI check timed out; settings remain in the owned test instance, do not replay updates")
                await asyncio.sleep(.5)
            visual = json.loads(marker.read_text(encoding="utf-8"))
            if (visual.get("checked") is not True or visual.get("pid") != identity["pid"]
                    or visual.get("process_start") != identity["process_start"]
                    or visual.get("expected_settings") != actual["settings"]):
                raise RuntimeError("UI confirmation identity or values do not match this run")
            assert oracle() == actual
            report["visual_acceptance"] = True
            record("owned_mesh_parameters_window_values", visual)

        restored = two.rpc(identity["port"], remote_restore_settings, identity["pid"], identity["program_args"],
                           report["initial_parameter_backup"], before["settings"])
        read = await h.direct_result("ansa_mesh_settings", {"action": "read"})
        assert read["settings"] == before["settings"] and oracle() == before
        record("original_test_parameters_restored_and_read_back", {"restore": restored, "read": read})
        report["success"] = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ansa-root", required=True, type=Path)
    parser.add_argument("--runtime-home", required=True, type=Path)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--wait-ui", action="store_true", help="等待所属 Mesh Parameters 窗口的独立人工/GUI 检查")
    parser.add_argument("--keep-ansa", action="store_true")
    args = parser.parse_args()
    run, instructions = stage._load_run(args)
    if (run / "seed-baseline.ansa").exists() or (run / "settings-report.json").exists():
        raise ValueError("仅接受新的准备目录；结果未知时查询原报告/任务，不重放本批设置")
    report = {"scope": "ANSA 2026.1 GUI 会话网格参数；MCP stdio → 自研 TCP → 所属主线程",
              "started_at": datetime.now(timezone.utc).isoformat(), "success": False, "cases": [],
              "visual_acceptance": False, "defaults_file_written": False, "ribbon_lifecycle_acceptance": False}
    identity = helpers = None
    try:
        identity, _ansa, _grpc, helpers = stage._prepare_owned_ansa(run, instructions)
        report["identity"] = identity
        report["visible_fixture"] = helpers[1].rpc(
            identity["port"], remote_initialize_visible_fixture, identity["pid"],
            identity["program_args"], identity["seed_path"], identity["seed_entity_id"],
        )
        asyncio.run(verify(run, instructions, identity, helpers, report, wait_ui=args.wait_ui))
    except BaseException as exc:
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
        report["exception_tree"] = stage._exception_tree(exc)
        (run / "settings-report.error.log").write_text(traceback.format_exc(), encoding="utf-8")
        raise
    finally:
        if identity is not None and helpers is not None and report["success"] and not args.keep_ansa:
            try:
                report["cleanup"] = stage._cleanup_owned_ansa(identity, instructions, helpers)
            except Exception as exc:
                report["cleanup"] = {"error": str(exc), "stopped_owned_process": False}
                report["success"] = False
        stage._json_write(run / "settings-report.json", report)


if __name__ == "__main__":
    main()
