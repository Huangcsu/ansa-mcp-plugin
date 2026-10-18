"""第三阶段文件与组织能力真机验收；复用所属进程核验和真实 MCP harness。"""

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


def remote_fixture_stack(expected_pid, expected_arguments):
    import os
    import sys
    import threading
    import traceback
    from ansa import session
    if os.getpid() != expected_pid or list(session.ProgramArguments()) != expected_arguments:
        raise RuntimeError("Independent process identity changed")
    frame = sys._current_frames()[threading.main_thread().ident]
    return [{"file": item.filename, "line": item.lineno, "function": item.name}
            for item in traceback.extract_stack(frame)[-15:]]


def artifact(result):
    path = Path(result["path"])
    content = path.read_bytes()
    assert content and len(content) == result["size_bytes"], result
    assert hashlib.sha256(content).hexdigest() == result["sha256"], result
    return content


async def followup(h, run, identity, previous, record, recovered_step=None):
    prior = {item["name"]: item["evidence"] for item in previous["cases"]}
    exports = prior["three_solver_exports_independent_content_and_hash"]
    auto = prior["autosave_unique_idempotent_and_preserves_database"]["first"]
    expected_members = prior["set_create_add_readback_idempotency_and_stale_rejection"]["actual"]
    deck = (await h.snapshot_context())["deck"]
    grid_id = identity["seed_entity_id"]
    grid = await h.direct_result("ansa_entity_get", {
        "entity_type": "GRID", "entity_id": grid_id, "fields": ["X1", "X2", "X3"], "deck": deck,
    })
    # Activate a model view only after the late fixture result is authoritative.
    # Preserve that model before Open; never replay the timed-out CAD script.
    if recovered_step is None:
        cad_copy = await h.direct_result("ansa_model_autosave", {"directory": str(run)})
        artifact(cad_copy)
        opened = await h.direct_result("ansa_model_open", {
            "path": cad_copy["path"], "backup_path": str(run / "before-cad-view.ansa"),
        })
        await h.snapshot_context()
        faces = await h.direct_result("ansa_entity_list", {"entity_type": "FACE", "deck": deck})
        assert faces["total"] >= 1, faces
        record("late_fixture_saved_opened_and_face_readback", {"copy": cad_copy, "opened": opened, "faces": faces})
    refused = await h.direct_result("ansa_model_export", {"format": "STEP", "path": str(run / "not-exported.step")}, expected="failed")
    assert not (run / "not-exported.step").exists()
    if recovered_step is None:
        stage._json_write(run / "files-ui-instruction.json", {
            "stage": "step_output_parameters", "pid": identity["pid"],
            "window_file": cad_copy["path"],
            "instruction": "在此独立 ANSA 的 STEP Output Parameters 对话框保留 visible 范围并确认；超时后只查询原任务，不重放。",
        })
        exported = await h.direct_result("ansa_model_export", {"format": "STEP", "path": str(run / "verified-cad.step"), "scope": "visible"})
    else:
        exported = recovered_step["data"]["result"]
    content = artifact(exported)
    assert b"ISO-10303-21;" in content and b"ADVANCED_FACE" in content, content[:400]
    record("step_explicit_visible_scope_and_cad_content", {
        "scope_rejection": refused["error"], "step": exported,
        "recovered_task_id": recovered_step["task_id"] if recovered_step else None,
        "native_output_parameters_confirmed": True,
    })
    before_epoch = h.model["epoch"]
    before_count = (await h.direct_result("ansa_entity_count", {"entity_type": "GRID", "deck": deck}))["count"]
    required = await h.direct_result("ansa_model_import", {"format": "NASTRAN", "path": exports["NASTRAN"]["path"]}, expected="failed")
    assert (await h.snapshot_context())["model"]["epoch"] == before_epoch
    imported = await h.direct_result("ansa_model_import", {
        "format": "NASTRAN", "path": exports["NASTRAN"]["path"], "backup_path": str(run / "before-import.ansa"),
    })
    assert imported["imported"] and imported["entity_summary_verified"], imported
    after_snap = await h.snapshot_context()
    assert h.model["epoch"] > before_epoch
    count = (await h.direct_result("ansa_entity_count", {"entity_type": "GRID", "deck": deck}))["count"]
    assert count > before_count
    current_grid = await h.direct_result("ansa_entity_get", {
        "entity_type": "GRID", "entity_id": grid_id, "fields": ["X1", "X2", "X3"], "deck": deck,
    })
    assert current_grid["fields"] == grid["fields"]
    record("nastran_import_backup_id_offsets_and_new_context", {
        "backup_required": required["error"], "imported": imported, "node_count_before": before_count,
        "node_count_after": count, "original_node": current_grid, "epoch_after": h.model["epoch"],
    })
    for file_format in ("ABAQUS", "LSDYNA"):
        other = await h.direct_result("ansa_model_import", {
            "format": file_format, "path": exports[file_format]["path"],
            "backup_path": str(run / ("before-" + file_format.casefold() + "-import.ansa")),
        })
        assert other["imported"] and other["entity_summary_verified"], other
        after_snap = await h.snapshot_context()
        record(file_format.casefold() + "_import_readback", {"imported": other, "model": after_snap["model"], "deck": after_snap["deck"]})
        if not after_snap["model"]["write_enabled"]:
            h.report["restrictions"].append("导入后当前 deck 写门关闭，需重新查询已验证 deck；不绕过保护")
            break
    if after_snap["model"]["write_enabled"]:
        restored = await h.direct_result("ansa_model_open", {"path": auto["path"], "backup_path": str(run / "before-autosave-readback.ansa")})
        await h.snapshot_context()
        members = await h.direct_result("ansa_set_members", {"set_id": expected_members["set"]["id"], "deck": deck})
        assert members["members"] == expected_members["members"]
        record("autosave_open_independent_set_readback", {"restored": restored, "members": members})


async def verify(run, instructions, identity, helpers, report, previous=None):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    stage_one, stage_two, _original_channel = helpers
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-B", "-m", "ansa_mcp_plugin", "--runtime-home", instructions["runtime_home"]],
        env={**os.environ, "PYTHONPATH": str(stage.PROJECT_ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"},
    )

    def record(name, evidence):
        report["cases"].append({"name": name, "ok": True, "evidence": evidence})
        stage._json_write(run / report["report_file"], report)
        print(json.dumps({"case": name, "ok": True}, ensure_ascii=True), flush=True)

    with (run / (Path(report["report_file"]).stem + ".stderr.log")).open("w", encoding="utf-8") as log:
        async with stdio_client(parameters, errlog=log) as (reader, writer):
            async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=30)) as client:
                await client.initialize()
                h = stage.McpHarness(client, report, run)
                record("registered_tool_schemas", await h.list_schema())
                for _ in range(100):
                    status = await h.call("ansa_connection_status")
                    if any(item["instance_id"] == identity["instance_id"]
                           for item in status["data"].get("instances", [])):
                        break
                    await asyncio.sleep(.1)
                recovered_step = None
                if previous is not None:
                    late = await h.call("ansa_task_query", {"task_id": report["resume_task_id"]}, allow_error=True)
                    assert late["status"] == "succeeded", late
                    assert late["data"]["result"]["executor"]["pid"] == identity["pid"], late
                    assert late["data"]["result"]["result"]["faces"], late
                    record("original_timed_out_fixture_result_recovered_without_replay", {
                        "task_id": late["task_id"], "operation_id": late["operation_id"], "result": late["data"]["result"],
                    })
                    if report.get("resume_step_task_id"):
                        recovered_step = await h.call("ansa_task_query", {"task_id": report["resume_step_task_id"]}, allow_error=True)
                        assert recovered_step["status"] == "succeeded", recovered_step
                        assert recovered_step["data"]["result"]["executor"]["pid"] == identity["pid"], recovered_step
                        assert recovered_step["data"]["result"]["format"] == "STEP", recovered_step
                        record("original_step_task_recovered_without_replay", {
                            "task_id": recovered_step["task_id"], "operation_id": recovered_step["operation_id"],
                            "result": recovered_step["data"]["result"],
                        })
                snap = await stage._bind_for_run(h, identity, stage_one, stage_two, identity["port"])
                report["target"] = h.target
                report["initial_model"] = snap["model"]
                deck = snap["deck"]
                grid_id = identity["seed_entity_id"]

                if previous is not None:
                    await followup(h, run, identity, previous, record, recovered_step)
                    report["last_model"] = (await h.snapshot_context())["model"]
                    report["success"] = True
                    stage._json_write(run / report["report_file"], report)
                    return

                part = await h.direct_result("ansa_part_create", {"name": "Stage three file verification", "module_id": "FILES-20261013"})
                part_read = await h.direct_result("ansa_entity_get", {
                    "entity_type": part["entity"]["type"], "entity_id": part["entity"]["id"], "fields": ["Name"], "deck": deck,
                })
                assert part_read["fields"]["Name"] == part["name"]
                found = await h.direct_result("ansa_entity_find", {"pattern": part["name"], "match": "exact", "deck": deck})
                assert part["entity"] in found["entities"], found
                record("part_create_and_name_find", {"created": part, "readback": part_read, "found": found})

                created_set = await h.direct_result("ansa_entity_create", {"entity_type": "SET", "fields": {}, "deck": deck})
                set_id = created_set["entity"]["id"]
                set_card = await h.direct_result("ansa_entity_get", {"entity_type": "SET", "entity_id": set_id, "fields": ["Name"], "deck": deck})
                named_set = await h.direct_result("ansa_entity_update", {
                    "entity_type": "SET", "entity_id": set_id, "expected_fields": set_card["fields"],
                    "fields": {"Name": "Verified direct node set"}, "deck": deck,
                })
                set_fields = named_set["fields"]
                members = await h.direct_result("ansa_set_members", {"set_id": set_id, "deck": deck})
                assert members["members"] == [], members
                grid = await h.direct_result("ansa_entity_get", {
                    "entity_type": "GRID", "entity_id": grid_id, "fields": ["X1", "X2", "X3"], "deck": deck,
                })
                add_params = {"set_id": set_id, "expected_fields": set_fields,
                              "expected_members": members["members"], "deck": deck,
                              "entities": [{"type": "GRID", "id": grid_id, "expected_fields": grid["fields"]}]}
                added_task, added_done = await h.task("ansa_set_add", add_params)
                added = added_done["data"]["result"]
                repeated, _ = await h.task("ansa_set_add", add_params, operation_id=added_task["operation_id"])
                assert repeated["task_id"] == added_task["task_id"]
                stale = await h.direct_result("ansa_set_add", add_params, expected="failed")
                actual = await h.direct_result("ansa_set_members", {"set_id": set_id, "deck": deck})
                assert actual["members"] == [grid["entity"]], actual
                assert actual["members"] == added["members"] and stale["status"] == "failed"
                record("set_create_add_readback_idempotency_and_stale_rejection", {
                    "set": created_set, "actual": actual, "original_task": added_task["task_id"],
                    "repeated_task": repeated["task_id"], "stale_error": stale["error"],
                })

                auto_submit, auto_done = await h.task("ansa_model_autosave", {"directory": str(run)})
                auto = auto_done["data"]["result"]
                artifact(auto)
                auto_repeat, _ = await h.task("ansa_model_autosave", {"directory": str(run)}, operation_id=auto_submit["operation_id"])
                auto_second = await h.direct_result("ansa_model_autosave", {"directory": str(run)})
                artifact(auto_second)
                summary = await h.direct_result("ansa_model_summary", {"deck": deck})
                assert auto["path"] != auto_second["path"]
                assert auto_repeat["task_id"] == auto_submit["task_id"]
                assert Path(summary["database"]).resolve() == Path(identity["seed_path"]).resolve()
                record("autosave_unique_idempotent_and_preserves_database", {"first": auto, "second": auto_second, "database": summary["database"]})

                rejected_save = await h.direct_result("ansa_model_save_current", {"expected_path": auto["path"], "overwrite": True}, expected="failed")
                implicit_save = await h.direct_result("ansa_model_save_current", {"expected_path": identity["seed_path"]}, expected="failed")
                saved = await h.direct_result("ansa_model_save_current", {"expected_path": identity["seed_path"], "overwrite": True})
                artifact(saved)
                record("save_current_requires_path_and_overwrite", {"saved": saved, "wrong_path": rejected_save["error"], "implicit_overwrite": implicit_save["error"]})

                exports = {}
                for file_format, suffix, marker in (("NASTRAN", ".bdf", b"grid"), ("ABAQUS", ".inp", b"*node"), ("LSDYNA", ".key", b"*node")):
                    destination = run / ("verified-export" + suffix)
                    exported = await h.direct_result("ansa_model_export", {"format": file_format, "path": str(destination)})
                    content = artifact(exported)
                    assert marker in content.lower(), (file_format, content[:200])
                    exports[file_format] = exported
                record("three_solver_exports_independent_content_and_hash", exports)
                output_before = Path(exports["NASTRAN"]["path"]).read_bytes()
                refused = await h.direct_result("ansa_model_export", {"format": "NASTRAN", "path": exports["NASTRAN"]["path"]}, expected="failed")
                assert Path(exports["NASTRAN"]["path"]).read_bytes() == output_before
                overwritten = await h.direct_result("ansa_model_export", {"format": "NASTRAN", "path": exports["NASTRAN"]["path"], "overwrite": True})
                artifact(overwritten)
                record("solver_export_overwrite_intent", {"rejected": refused["error"], "overwritten": overwritten})

                geometry = await h.direct_result("ansa_script_execute", {"script": (
                    "from ansa import base, constants\n"
                    "points = [[0.,0.,0.], [20.,0.,0.], [20.,10.,0.], [0.,10.,0.]]\n"
                    "curves = [base.CurvesNew([points[i], points[(i+1)%4]]) for i in range(4)]\n"
                    "if any(curve is None for curve in curves): raise RuntimeError('CAD fixture curve creation failed')\n"
                    "faces = base.FacesNewPlanar(curves, ret_ents=True)\n"
                    "if not faces: raise RuntimeError('CAD fixture face creation failed')\n"
                    "RESULT = {'faces': [face._id for face in faces]}\n"
                )})
                step_default = await h.direct_result("ansa_model_export", {"format": "STEP", "path": str(run / "not-exported.step")}, expected="failed")
                assert not (run / "not-exported.step").exists()
                step_result = await h.direct_result("ansa_model_export", {"format": "STEP", "path": str(run / "verified-cad.step"), "scope": "visible"})
                step_content = artifact(step_result)
                assert b"ISO-10303-21;" in step_content and b"ADVANCED_FACE" in step_content, step_content[:400]
                record("step_explicit_visible_scope_and_cad_content", {"fixture": geometry["result"], "scope_rejection": step_default["error"], "step": step_result})

                before_epoch = h.model["epoch"]
                before_grid_count = (await h.direct_result("ansa_entity_count", {"entity_type": "GRID", "deck": deck}))["count"]
                backup_required = await h.direct_result("ansa_model_import", {"format": "NASTRAN", "path": exports["NASTRAN"]["path"]}, expected="failed")
                assert (await h.snapshot_context())["model"]["epoch"] == before_epoch
                imported = await h.direct_result("ansa_model_import", {
                    "format": "NASTRAN", "path": exports["NASTRAN"]["path"], "backup_path": str(run / "before-import.ansa"),
                })
                assert imported["imported"] and imported["entity_summary_verified"], imported
                assert Path(imported["backup"]["path"]).is_file()
                after_snap = await h.snapshot_context()
                assert h.model["epoch"] > before_epoch
                count = (await h.direct_result("ansa_entity_count", {"entity_type": "GRID", "deck": deck}))["count"]
                assert count > before_grid_count
                original_grid = await h.direct_result("ansa_entity_get", {"entity_type": "GRID", "entity_id": grid_id, "fields": ["X1", "X2", "X3"], "deck": deck})
                assert original_grid["fields"] == grid["fields"]
                record("nastran_import_backup_id_offsets_and_new_context", {
                    "missing_backup": backup_required["error"], "imported": imported, "node_count_before": before_grid_count,
                    "node_count_after": count, "original_node": original_grid, "epoch_after": h.model["epoch"],
                })

                # Refresh/retry using the original operation ID is covered by autosave and set_add.
                # Cross-solver import is separately exercised while the current deck remains supported.
                for file_format in ("ABAQUS", "LSDYNA"):
                    imported_other = await h.direct_result("ansa_model_import", {
                        "format": file_format, "path": exports[file_format]["path"],
                        "backup_path": str(run / ("before-" + file_format.casefold() + "-import.ansa")),
                    })
                    assert imported_other["imported"] and imported_other["entity_summary_verified"], imported_other
                    after_snap = await h.snapshot_context()
                    record(file_format.casefold() + "_import_readback", {"imported": imported_other, "model": after_snap["model"], "deck": after_snap["deck"]})
                    if not after_snap["model"]["write_enabled"]:
                        report["restrictions"].append("导入后当前 deck 写门关闭，需在 ANSA 切回已验证 deck 并重新查询；不绕过保护")
                        break

                if after_snap["model"]["write_enabled"]:
                    restored = await h.direct_result("ansa_model_open", {"path": auto["path"], "backup_path": str(run / "before-autosave-readback.ansa")})
                    await h.snapshot_context()
                    restored_members = await h.direct_result("ansa_set_members", {"set_id": set_id, "deck": deck})
                    assert restored_members["members"] == actual["members"]
                    record("autosave_open_independent_set_readback", {"restored": restored, "members": restored_members})
                report["last_model"] = (await h.snapshot_context())["model"]
                report["success"] = True
                stage._json_write(run / report["report_file"], report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ansa-root", required=True, type=Path)
    parser.add_argument("--runtime-home", required=True, type=Path)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--keep-ansa", action="store_true")
    parser.add_argument("--resume-task-id", help="原夹具已完成后按原任务 ID 补验；不重新执行该脚本")
    parser.add_argument("--resume-step-task-id", help="原 STEP 任务已完成后只查询该任务，再补验导入；须同时指定 --resume-task-id")
    args = parser.parse_args()
    if args.resume_step_task_id and not args.resume_task_id:
        parser.error("--resume-step-task-id requires --resume-task-id")
    run, instructions = stage._load_run(args)
    report = {"scope": "MCP stdio → TCP → ANSA 2026.1 所属主线程；模型文件与SET/Part能力",
              "started_at": datetime.now(timezone.utc).isoformat(), "success": False, "cases": [], "restrictions": [],
              "visual_acceptance": False, "ribbon_lifecycle_acceptance": False}
    report["report_file"] = "files-followup-report.json" if args.resume_task_id else "files-report.json"
    if args.resume_step_task_id:
        report["report_file"] = "files-import-report.json"
        report["resume_step_task_id"] = args.resume_step_task_id
    if (run / report["report_file"]).exists():
        report["report_file"] = Path(report["report_file"]).stem + "-" + uuid.uuid4().hex[:8] + ".json"
    print(json.dumps({"report_path": str(run / report["report_file"])}, ensure_ascii=True), flush=True)
    previous = None
    identity = None
    helpers = None
    try:
        if args.resume_task_id:
            import grpc
            previous = json.loads((run / "files-report.json").read_text(encoding="utf-8"))
            identity = previous["identity"]
            one, two = stage._import_runtime_helpers()
            original = stage._bounded_grpc(grpc, one.DeadlineInterceptor)
            helpers = (one, two, original)
            basic = two.rpc(identity["port"], one.remote_basic)
            stage._verify_program_arguments(basic, Path(instructions["translator"]), identity["port"])
            if basic["pid"] != identity["pid"] or basic["program_args"] != identity["program_args"]:
                raise RuntimeError("补验目标进程不是原独立实例")
            report["resume_task_id"] = args.resume_task_id
            report["previous_report"] = str(run / "files-report.json")
        else:
            identity, _ansa, _grpc, helpers = stage._prepare_owned_ansa(run, instructions)
        report["identity"] = identity
        asyncio.run(verify(run, instructions, identity, helpers, report, previous))
    except BaseException as exc:
        report["success"] = False
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
