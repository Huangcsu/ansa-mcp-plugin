"""在专属 ANSA GUI 中验收表面网格；任务经过 MCP stdio 与自研 TCP。"""

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import traceback
import uuid

import stage_three_ansa as stage


def remote_load_fixture(expected_pid, expected_arguments, fixture):
    import os
    from pathlib import Path
    import threading
    from ansa import base, session
    if (os.getpid() != expected_pid or list(session.ProgramArguments()) != expected_arguments
            or threading.current_thread() is not threading.main_thread()):
        raise RuntimeError("CAD fixture loader is not on the owned ANSA main thread")
    if base.Open(fixture) != 0 or Path(base.DataBaseName()).resolve() != Path(fixture).resolve():
        raise RuntimeError("Independent CAD fixture was not opened")
    return {"pid": os.getpid(), "model_path": base.DataBaseName()}


def remote_mesh_readback(expected_pid, expected_arguments):
    import math
    import os
    import threading
    from ansa import base, mesh, session
    if (os.getpid() != expected_pid or list(session.ProgramArguments()) != expected_arguments
            or threading.current_thread() is not threading.main_thread()):
        raise RuntimeError("Mesh oracle is not on the owned ANSA main thread")
    deck = base.CurrentDeck()
    shells = base.CollectEntities(deck, None, "SHELL")
    grid = base.CollectEntities(deck, None, "GRID")
    shell_rows = []
    for shell in shells[:200]:
        values = base.GetEntityCardValues(deck, shell, tuple(shell.card_fields(deck)))
        # Preserve field names from this exact installed deck, never infer G/N labels.
        shell_rows.append({"id": shell._id, "fields": {
            name: value._id if isinstance(value, base.Entity) else value
            for name, value in values.items() if value is None or isinstance(value, (str, int, float, base.Entity))
        }})
    coordinates = [base.GetEntityCardValues(deck, node, ("X1", "X2", "X3")) for node in grid]
    if not all(all(isinstance(value, (int, float)) and math.isfinite(value) for value in item.values())
               and set(item) == {"X1", "X2", "X3"} for item in coordinates):
        raise RuntimeError("GRID coordinates were not independently confirmed as finite")
    return {"pid": os.getpid(), "deck": deck, "model_path": base.DataBaseName(),
            "shell_ids": sorted(shell._id for shell in shells), "shell_cards": shell_rows,
            "grid_count": len(grid), "coordinates_finite": True,
            "face_ids": sorted(face._id for face in base.CollectEntities(deck, None, "FACE")),
            "settings": base.BCSettingsGetValues(("mesh_change_option", "element_type", "element_order", "target_element_length"))}


def remote_refresh_meshing(expected_pid, expected_arguments):
    import builtins
    import importlib
    import os
    import sys
    from ansa import session
    if os.getpid() != expected_pid or list(session.ProgramArguments()) != expected_arguments:
        raise RuntimeError("Not the original independent ANSA process")
    bridge = builtins._ansa_mcp_plugin_bridge
    bridge.state.assert_main()
    if bridge.state.journal.busy() or bridge.state.journal.blocked:
        raise RuntimeError("Original executor has unresolved tasks; do not refresh")
    name = "ansa_mcp_bridge.operations.meshing"
    if name in sys.modules:
        importlib.reload(sys.modules[name])
    return {"module": name, "idle_refresh": True}


async def delete_and_reopen(h, run, deck, actual, auto, record, oracle):
    # Explicit fields are mandatory: entity_get intentionally defaults to metadata only.
    shell_id = actual["shell_ids"][0]
    card = await h.direct_result("ansa_entity_get", {
        "entity_type": "SHELL", "entity_id": shell_id, "deck": deck, "fields": ["type", "EID", "PID", "G1", "G2", "G3"],
    })
    deletion = [{"id": shell_id, "expected_fields": card["fields"]}]
    bad = json.loads(json.dumps(deletion))
    bad[0]["expected_fields"]["EID"] += 100000
    stale_delete = await h.direct_result("ansa_mesh_delete", {"entities": bad, "deck": deck}, expected="failed")
    assert oracle() == actual
    delete_op = "stage-three-mesh-delete-" + uuid.uuid4().hex
    delete_submit, delete_done = await h.task("ansa_mesh_delete", {"entities": deletion, "deck": deck}, operation_id=delete_op)
    removed = delete_done["data"]["result"]
    after_delete = oracle()
    assert after_delete["shell_ids"] == [item for item in actual["shell_ids"] if item != shell_id]
    assert after_delete["grid_count"] == actual["grid_count"] and after_delete["face_ids"] == actual["face_ids"]
    assert after_delete["settings"] == actual["settings"] and removed["scope_verified"]
    delete_repeat = await h.call("ansa_mesh_delete", {"operation_id": delete_op, "expected_target": h.target,
                                                       "expected_model": h.model, "entities": deletion, "deck": deck})
    assert delete_repeat["task_id"] == delete_submit["task_id"] and oracle() == after_delete
    record("guarded_shell_delete_preserves_other_shells_nodes_faces_and_idempotency", {
        "stale_error": stale_delete["error"], "deleted": removed, "independent": after_delete,
    })
    opened = await h.direct_result("ansa_model_open", {"path": auto["path"], "backup_path": str(run / "before-mesh-open.ansa")})
    await h.snapshot_context()
    reopened = oracle()
    assert reopened["shell_ids"] == actual["shell_ids"] and reopened["grid_count"] == actual["grid_count"]
    record("saved_mesh_open_independent_readback", {"opened": opened, "independent": reopened})


async def verify(run, instructions, identity, helpers, report, previous=None):
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
        stage._json_write(run / report["report_file"], report)
        print(json.dumps({"case": name, "ok": True}, ensure_ascii=True), flush=True)

    def oracle():
        return two.rpc(identity["port"], remote_mesh_readback, identity["pid"], identity["program_args"])

    with (run / (Path(report["report_file"]).stem + ".stderr.log")).open("w", encoding="utf-8") as log:
        async with stdio_client(parameters, errlog=log) as (reader, writer):
            async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=30)) as client:
                await client.initialize()
                h = stage.McpHarness(client, report, run)
                record("registered_tool_schemas", await h.list_schema())
                for _ in range(100):
                    status = await h.call("ansa_connection_status")
                    if any(item["instance_id"] == identity["instance_id"] for item in status["data"].get("instances", [])):
                        break
                    await asyncio.sleep(.1)
                snap = await stage._bind_for_run(h, identity, one, two, identity["port"])
                report["target"] = h.target
                deck = snap["deck"]
                if previous is not None:
                    prior = {case["name"]: case["evidence"] for case in previous["cases"]}
                    meshed = prior["tria_surface_mesh_readback_parameter_restore_and_idempotency"]
                    original = await h.call("ansa_task_query", {"task_id": meshed["task_id"]})
                    assert original["status"] == "succeeded" and original["data"]["result"]["created_shell_count"] == meshed["mesh"]["created_shell_count"]
                    actual = oracle()
                    assert actual == meshed["independent"], "模型已变化；不能使用原删除前置条件继续"
                    auto = prior["tria_solver_content_and_saved_mesh_artifact"]["save"]
                    assert hashlib.sha256(Path(auto["path"]).read_bytes()).hexdigest() == auto["sha256"]
                    report["meshed_model_path"] = auto["path"]
                    record("original_mesh_result_recovered_without_replay", {"task_id": meshed["task_id"], "independent": actual})
                    await delete_and_reopen(h, run, deck, actual, auto, record, oracle)
                    report["last_model"] = (await h.snapshot_context())["model"]
                    report["success"] = True
                    return
                before = oracle()
                assert before["face_ids"] and not before["shell_ids"], before
                read = await h.direct_result("ansa_mesh_faces", {"face_ids": before["face_ids"], "deck": deck})
                conditions = [row["condition"] for row in read["faces"]]
                record("unmeshed_macro_snapshot_and_native_zero_sentinel", {"read": read, "independent": before})
                stale = json.loads(json.dumps(conditions))
                stale[0]["expected_fields"]["Thickness"] += 1.0
                rejected = await h.direct_result("ansa_mesh_surface", {"faces": stale, "target_length": 2.0, "deck": deck}, expected="failed")
                assert oracle() == before
                record("stale_face_rejected_without_mesh_or_parameter_change", {"task_id": rejected["task_id"], "error": rejected["error"]})

                operation = "stage-three-mesh-" + uuid.uuid4().hex
                arguments = {"faces": conditions, "target_length": 2.0, "deck": deck}
                submitted, completed = await h.task("ansa_mesh_surface", arguments, operation_id=operation)
                meshed = completed["data"]["result"]
                assert meshed["parameters_restored"] and meshed["scope_verified"] and meshed["created_shell_count"] > 0, meshed
                actual = oracle()
                assert actual["shell_ids"] == meshed["created_shell_ids"] and actual["settings"] == before["settings"], actual
                assert actual["face_ids"] == before["face_ids"] and actual["coordinates_finite"], actual
                repeat = await h.call("ansa_mesh_surface", {"operation_id": operation, "expected_target": h.target,
                                                             "expected_model": h.model, **arguments})
                assert repeat["task_id"] == submitted["task_id"] and oracle() == actual
                record("tria_surface_mesh_readback_parameter_restore_and_idempotency", {"task_id": submitted["task_id"], "mesh": meshed, "independent": actual})
                new_read = await h.direct_result("ansa_mesh_faces", {"face_ids": actual["face_ids"], "deck": deck})
                refused = await h.direct_result("ansa_mesh_surface", {
                    "faces": [row["condition"] for row in new_read["faces"]], "target_length": 2.0, "deck": deck,
                }, expected="failed")
                assert oracle() == actual
                record("existing_mesh_is_not_implicitly_replaced", {"task_id": refused["task_id"], "error": refused["error"]})

                quality = await h.direct_result("ansa_model_inspect", {
                    "action": "mesh_quality", "options": {"entity_type": "FACE", "entity_ids": actual["face_ids"]}, "deck": deck,
                })
                assert quality["read_only"] and not quality["auto_fix"] and oracle() == actual
                record("real_face_mesh_quality_report_without_auto_fix", quality)
                output = await h.direct_result("ansa_model_export", {"format": "NASTRAN", "path": str(run / "verified-mesh.bdf")})
                content = Path(output["path"]).read_bytes()
                assert hashlib.sha256(content).hexdigest() == output["sha256"] and b"ctria3" in content.lower(), output
                assert b"cquad4" not in content.lower()
                auto = await h.direct_result("ansa_model_autosave", {"directory": str(run)})
                report["meshed_model_path"] = auto["path"]
                record("tria_solver_content_and_saved_mesh_artifact", {"export": output, "save": auto})

                await delete_and_reopen(h, run, deck, actual, auto, record, oracle)
                report["last_model"] = (await h.snapshot_context())["model"]
                report["success"] = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ansa-root", required=True, type=Path)
    parser.add_argument("--runtime-home", required=True, type=Path)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--fixture", type=Path, help="专属未网格化 CAD .ansa 副本，不覆盖源文件")
    parser.add_argument("--resume-report", type=Path, help="仅恢复已有网格原任务，补验尚未开始的删除/重开；不重放网格")
    parser.add_argument("--keep-ansa", action="store_true")
    args = parser.parse_args()
    run, instructions = stage._load_run(args)
    previous = None
    if args.resume_report:
        prior_path = args.resume_report.resolve(strict=True)
        if prior_path.parent != run:
            raise ValueError("resume-report 必须属于本轮 run-dir")
        previous = json.loads(prior_path.read_text(encoding="utf-8"))
        passed = {case["name"] for case in previous["cases"]}
        if (previous["success"] or "tria_solver_content_and_saved_mesh_artifact" not in passed
                or "guarded_shell_delete_preserves_other_shells_nodes_faces_and_idempotency" in passed):
            raise ValueError("此补验仅接受已有网格/保存、尚未完成删除的原失败报告")
        fixture = Path(previous["fixture"])
    else:
        if args.fixture is None:
            parser.error("初次验收需要 --fixture")
        fixture = args.fixture.resolve(strict=True)
        fixture.relative_to(stage.VERIFICATION_ROOT.resolve())
        if fixture.suffix.casefold() != ".ansa" or (run / "seed-baseline.ansa").exists():
            raise ValueError("需要 runtime/verification 下的 CAD .ansa 和未执行过的专属 run-dir")
    report = {"scope": "MCP stdio → TCP → ANSA 2026.1 所属主线程；完整宏范围的三角形表面网格",
              "started_at": datetime.now(timezone.utc).isoformat(), "success": False, "cases": [],
              "report_file": "mesh-report.json", "fixture": str(fixture),
              "visual_acceptance": False, "ribbon_lifecycle_acceptance": False}
    identity = helpers = None
    if previous is not None:
        report["report_file"] = "mesh-followup-" + uuid.uuid4().hex[:8] + ".json"
        report["previous_report"] = str(prior_path)
    try:
        import grpc
        one, two = stage._import_runtime_helpers()
        original = stage._bounded_grpc(grpc, one.DeadlineInterceptor)
        basic = two.rpc(instructions["port"], one.remote_basic)
        stage._verify_program_arguments(basic, Path(instructions["translator"]), instructions["port"])
        if previous is not None:
            identity = previous["identity"]
            from ansa_mcp_shared.identity import same_process_alive
            if (basic["pid"] != identity["pid"] or basic["program_args"] != identity["program_args"]
                    or not same_process_alive(identity["pid"], identity["process_start"])):
                raise RuntimeError("补验进程与原任务归属不同，拒绝继续")
            helpers = (one, two, original)
            report["code_refresh"] = two.rpc(identity["port"], remote_refresh_meshing, identity["pid"], identity["program_args"])
        else:
            report["fixture_loaded"] = two.rpc(instructions["port"], remote_load_fixture, basic["pid"], basic["program_args"], str(fixture))
            grpc.insecure_channel = original
            identity, _ansa, _grpc, helpers = stage._prepare_owned_ansa(run, instructions)
        report["identity"] = identity
        asyncio.run(verify(run, instructions, identity, helpers, report, previous))
    except BaseException as exc:
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
        report["exception_tree"] = stage._exception_tree(exc)
        (run / (Path(report["report_file"]).stem + ".error.log")).write_text(traceback.format_exc(), encoding="utf-8")
        raise
    finally:
        if identity is not None and helpers is not None and report["success"] and not args.keep_ansa:
            try:
                report["cleanup"] = stage._cleanup_owned_ansa(identity, instructions, helpers)
            except Exception as exc:
                report["cleanup"] = {"error": str(exc), "stopped_owned_process": False}
                report["success"] = False
        stage._json_write(run / report["report_file"], report)


if __name__ == "__main__":
    main()
