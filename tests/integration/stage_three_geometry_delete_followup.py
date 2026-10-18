"""查询已完成删除的原结果，仅补验保存重开、实际备份恢复和所属 GUI。"""

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import traceback
import uuid

import stage_three_ansa as stage
from stage_three_cad_ansa import fingerprint
from stage_three_geometry_delete_ansa import remote_geometry_delete_readback


async def verify(run, instructions, identity, helpers, original, report, wait_ui):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    one, two, _original = helpers
    before = original["cases"][0]["evidence"]["independent"]
    after = original["cases"][5]["evidence"]["independent"]
    deletion = original["cases"][5]["evidence"]["result"]
    old_task = original["cases"][5]["evidence"]["repeat"]["task_id"]
    conditions = original["cases"][2]["evidence"]["delete_conditions"]
    saved_path, backup_path = run / "geometry-after-delete.ansa", run / "geometry-before-delete.ansa"
    saved, backup = fingerprint(saved_path), fingerprint(backup_path)
    assert backup == original["cases"][0]["evidence"]["backup"]
    parameters = StdioServerParameters(command=sys.executable,
        args=["-B", "-m", "ansa_mcp_plugin", "--runtime-home", instructions["runtime_home"]],
        env={**os.environ, "PYTHONPATH": str(stage.PROJECT_ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"})

    def record(name, evidence):
        report["cases"].append({"name": name, "ok": True, "evidence": evidence})
        stage._json_write(run / "geometry-delete-followup-report.json", report)
        print(json.dumps({"case": name, "ok": True}), flush=True)

    def oracle():
        return two.rpc(identity["port"], remote_geometry_delete_readback, identity["pid"], identity["program_args"])

    def same_content(actual, expected):
        for key in ("deck", "faces", "identities", "grids", "visible_face_ids"):
            assert actual[key] == expected[key], key

    with (run / "geometry-delete-followup-backend.stderr.log").open("w", encoding="utf-8") as log:
        async with stdio_client(parameters, errlog=log) as (reader, writer):
            async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=30)) as client:
                await client.initialize()
                h = stage.McpHarness(client, report, run)
                await stage._bind_for_run(h, identity, one, two, identity["port"])
                await h.list_schema()
                queried = await h.call("ansa_task_query", {"task_id": old_task})
                assert queried["status"] == "succeeded" and queried["data"]["result"] == deletion
                same_content(oracle(), after)
                old_model = dict(h.model)
                await h.direct_result("ansa_model_open", {"path": str(saved_path),
                    "backup_path": str(run / "geometry-followup-reopen-backup.ansa")})
                await h.snapshot_context()
                reopened = oracle()
                same_content(reopened, after)
                assert fingerprint(saved_path) == saved and fingerprint(backup_path) == backup
                stale = await h.call("ansa_geometry_delete", {"operation_id": "stale-"+uuid.uuid4().hex,
                    "expected_target": h.target, "expected_model": old_model, "deck": before["deck"], "faces": conditions}, allow_error=True)
                assert stale["ok"] is False and stale["task_id"] is None
                record("original_delete_task_readback_saved_copy_reopen_and_stale_context_rejection",
                       {"original_task": queried, "independent": reopened, "saved": saved, "stale": stale})

                await h.direct_result("ansa_model_open", {"path": str(backup_path),
                    "backup_path": str(run / "geometry-followup-deleted-model-backup.ansa")})
                await h.snapshot_context()
                restored = oracle()
                same_content(restored, before)
                assert len(restored["faces"]) == 3 and fingerprint(backup_path) == backup
                await h.direct_result("ansa_model_open", {"path": str(saved_path),
                    "backup_path": str(run / "geometry-followup-restored-model-backup.ansa")})
                await h.snapshot_context()
                final = oracle()
                same_content(final, after)
                record("actual_pre_delete_backup_restores_three_faces_and_saved_deletion_reopens",
                       {"restored": restored, "final": final, "backup": backup})

                if wait_ui:
                    marker = run / "geometry-delete-ui-confirmation.json"
                    if marker.exists():
                        raise RuntimeError("Refuse stale GUI evidence")
                    stage._json_write(run / "geometry-delete-ui-instruction.json", {"identity": identity,
                        "model_path": final["model_path"], "expected_visible_face_ids": final["visible_face_ids"],
                        "instruction": "确认所属 ANSA 主窗口灰色 FACE 只剩右侧竖直矩形，Database FACE 为1；保留的水平 CURVE 不代表 FACE。"})
                    print(json.dumps({"status": "waiting_for_geometry_delete_ui", "run_dir": str(run)}), flush=True)
                    deadline = asyncio.get_running_loop().time() + stage.PANEL_WAIT_SECONDS
                    while not marker.is_file():
                        if asyncio.get_running_loop().time() >= deadline:
                            raise TimeoutError("GUI timed out; retain this owned instance and original tasks")
                        await asyncio.sleep(.5)
                    visual = json.loads(marker.read_text(encoding="utf-8"))
                    if (visual.get("checked") is not True or visual.get("pid") != identity["pid"]
                            or visual.get("process_start") != identity["process_start"]
                            or visual.get("model_path") != final["model_path"]
                            or visual.get("one_vertical_face_visible") is not True):
                        raise RuntimeError("GUI evidence does not match the owned instance and model")
                    assert oracle() == final
                    report["visual_acceptance"] = True
                    record("owned_main_window_single_face_after_backup_restore_and_post_ui_readback", visual)
    report["success"] = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ansa-root", required=True, type=Path)
    parser.add_argument("--runtime-home", required=True, type=Path)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--wait-ui", action="store_true")
    args = parser.parse_args()
    run, instructions = stage._load_run(args)
    original = json.loads((run / "geometry-delete-report.json").read_text(encoding="utf-8"))
    expected = ["schemas_fixture_normal_oracle_and_recoverable_backup",
        "normal_z_rankings_include_all_signs_and_preserve_model_visibility",
        "fresh_deletion_conditions_preview_has_no_write_or_ui_effects",
        "whole_batch_rejects_stale_geometry_boundary_and_missing_face_before_any_deletion",
        "generic_card_only_face_deletion_cannot_bypass_geometry_conditions",
        "explicit_two_face_deletion_scope_and_same_operation_idempotency"]
    if ([case["name"] for case in original["cases"]] != expected
            or not all(case["ok"] is True for case in original["cases"])
            or (run / "geometry-delete-followup-report.json").exists()):
        raise RuntimeError("仅接受已确认删除且尚未补验的原6项记录；其他状态先查询原任务")
    report = {"scope": "原6项之后的保存重开、实际备份恢复及所属GUI补验，不重放已完成删除",
              "started_at": datetime.now(timezone.utc).isoformat(), "success": False, "cases": [],
              "visual_acceptance": False, "identity": original["identity"], "original_report": str(run / "geometry-delete-report.json")}
    one, two = stage._import_runtime_helpers()
    import grpc
    original_channel = stage._bounded_grpc(grpc, one.DeadlineInterceptor)
    helpers = (one, two, original_channel)
    try:
        asyncio.run(verify(run, instructions, report["identity"], helpers, original, report, args.wait_ui))
    except BaseException as exc:
        report["success"] = False
        report["error"] = stage._exception_tree(exc)
        (run / "geometry-delete-followup-report.error.log").write_text(traceback.format_exc(), encoding="utf-8")
        raise
    finally:
        if report["success"]:
            try:
                report["cleanup"] = stage._cleanup_owned_ansa(report["identity"], instructions, helpers)
            except Exception as exc:
                report["cleanup"] = {"error": str(exc), "stopped_owned_process": False}
                report["success"] = False
        stage._json_write(run / "geometry-delete-followup-report.json", report)


if __name__ == "__main__":
    main()
