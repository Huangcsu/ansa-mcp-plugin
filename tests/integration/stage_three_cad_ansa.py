"""在独占 ANSA GUI 验证明确替换、默认备份和 STEP 几何回读。"""

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sys
import traceback
import uuid

import stage_three_ansa as stage


def remote_initialize_fixture(expected_pid, expected_arguments, seed_path, seed_id):
    import builtins
    import os
    from pathlib import Path
    import threading
    from ansa import base, session
    if (os.getpid() != expected_pid or list(session.ProgramArguments()) != expected_arguments
            or threading.current_thread() is not threading.main_thread()):
        raise RuntimeError("CAD fixture requires the owned ANSA main thread")
    if not Path(seed_path).is_file() or builtins._ansa_mcp_plugin_bridge.state.journal.busy():
        raise RuntimeError("CAD fixture requires a saved baseline and no pending tasks")
    session.New("discard")
    if base.Open(seed_path) != 0:
        raise RuntimeError("Could not activate the owned visible seed model")
    nodes = base.CollectEntities(base.CurrentDeck(), None, "GRID")
    if len(nodes) != 1 or nodes[0]._id != seed_id or not nodes[0].is_usable():
        raise RuntimeError("Owned visible seed differs from saved fixture")
    return {"pid": os.getpid(), "current_model_path": base.DataBaseName()}


def remote_cad_readback(expected_pid, expected_arguments):
    import os
    import threading
    from ansa import base, constants, session
    if (os.getpid() != expected_pid or list(session.ProgramArguments()) != expected_arguments
            or threading.current_thread() is not threading.main_thread()):
        raise RuntimeError("CAD oracle requires the owned ANSA main thread")
    deck = base.CurrentDeck()
    faces = [{"id": entity._id, "area": base.GetFaceArea(entity), "usable": entity.is_usable()}
             for entity in sorted(base.CollectEntities(deck, None, "FACE"), key=lambda e: e._id)]
    nodes = [{"id": entity._id, "fields": base.GetEntityCardValues(deck, entity, ("X1", "X2", "X3"))}
             for entity in sorted(base.CollectEntities(deck, None, "GRID"), key=lambda e: e._id)]
    return {"pid": os.getpid(), "main_thread": True, "version": str(constants.app_version),
            "deck": deck, "model_path": base.DataBaseName(), "faces": faces, "nodes": nodes}


def fingerprint(path):
    payload = path.read_bytes()
    return {"path": str(path), "size_bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}


def same_content(left, right):
    return {key: value for key, value in left.items() if key != "model_path"} == {
        key: value for key, value in right.items() if key != "model_path"}


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
        stage._json_write(run / "cad-report.json", report)
        print(json.dumps({"case": name, "ok": True}, ensure_ascii=True), flush=True)

    def oracle():
        return two.rpc(identity["port"], remote_cad_readback, identity["pid"], identity["program_args"])

    source = run / "rectangle-20x10-mm.step"
    source_record = fingerprint(source)
    before = oracle()
    assert before["faces"] == [] and before["nodes"] == [
        {"id": identity["seed_entity_id"], "fields": {"X1": 1.0, "X2": 2.0, "X3": 3.0}}]
    report["initial_oracle"] = before
    record("owned_saved_seed_and_independent_main_thread_readback", before)
    with (run / "cad-backend.stderr.log").open("w", encoding="utf-8") as log:
        async with stdio_client(parameters, errlog=log) as (reader, writer):
            async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=30)) as client:
                await client.initialize()
                h = stage.McpHarness(client, report, run)
                await stage._bind_for_run(h, identity, one, two, identity["port"])
                schemas = await h.list_schema()
                listed = await client.list_tools()
                spec = next(tool.inputSchema for tool in listed.tools if tool.name == "ansa_model_import")
                assert "STEP" in spec["properties"]["format"]["enum"]
                assert spec["properties"]["mode"]["default"] == "merge"
                assert set(spec["properties"]["mode"]["enum"]) == {"merge", "replace"}
                record("registered_schema_preserves_merge_default_and_exposes_explicit_replace", schemas)

                rejected = []
                invalid = run / "invalid.step"
                invalid.write_bytes(b"not a STEP container\n")
                for fields in (
                    {"format": "STEP", "path": str(source), "backup_path": str(run / "must-not-save.ansa")},
                    {"format": "STEP", "path": str(source), "mode": "replace"},
                    {"format": "STEP", "path": str(invalid), "mode": "replace", "preservation": "discard"},
                ):
                    epoch = h.model.copy()
                    rejected.append(await h.direct_result("ansa_model_import", fields, expected="failed"))
                    assert oracle() == before and (await h.snapshot_context())["model"]["epoch"] == epoch["epoch"]
                assert not (run / "must-not-save.ansa").exists()
                record("ambiguous_mode_missing_backup_and_invalid_container_rejected_without_changes", rejected)

                original_target, original_model = h.target.copy(), h.model.copy()
                operation_id = "cad-replace-" + uuid.uuid4().hex
                fields = {"format": "STEP", "path": str(source), "mode": "replace",
                          "backup_path": str(run / "before-step-import.ansa")}
                submitted, completed = await h.task("ansa_model_import", fields, operation_id=operation_id)
                imported = completed["data"]["result"]
                report["import_task"] = {"submitted": submitted, "completed": completed}
                stage._json_write(run / "cad-report.json", report)
                after = oracle()
                assert imported["imported"] and imported["geometry_verified"] and imported["return_code"] == 0
                assert imported["mode"] == "replace" and imported["source"] == source_record
                assert imported["source_unchanged"] and fingerprint(source) == source_record
                assert Path(after["model_path"]).resolve() == source.resolve() and after["nodes"] == []
                assert len(after["faces"]) == 1 and after["faces"][0]["usable"]
                assert math.isclose(after["faces"][0]["area"], 200.0, rel_tol=1e-8, abs_tol=1e-8)
                assert imported["after"]["face_ids"] == [after["faces"][0]["id"]]
                assert imported["after"]["face_ids_limit"] == 200 and not imported["after"]["face_ids_truncated"]
                assert math.isclose(imported["after"]["face_area_total"], 200.0, rel_tol=1e-8, abs_tol=1e-8)
                backup_path = Path(imported["backup"]["path"])
                assert imported["backup"]["saved"] and fingerprint(backup_path) == {
                    key: imported["backup"][key] for key in ("path", "size_bytes", "sha256")}
                await h.snapshot_context()
                assert h.model["epoch"] > original_model["epoch"]
                record("explicit_step_replace_backup_hash_and_independent_face_area", {
                    "imported": imported, "independent": after, "model_before": original_model, "model_after": h.model})

                repeat = await h.call("ansa_model_import", {"operation_id": operation_id,
                    "expected_target": original_target, "expected_model": original_model, **fields})
                assert repeat["task_id"] == submitted["task_id"] and oracle() == after
                repeat_result = await h.wait_task(repeat["task_id"])
                assert repeat_result["data"]["result"] == imported
                epoch = h.model.copy()
                stale = await h.call("ansa_model_import", {"operation_id": "cad-stale-" + uuid.uuid4().hex,
                    "expected_target": original_target, "expected_model": original_model, **fields}, allow_error=True)
                assert stale.get("ok") is False and oracle() == after
                assert (await h.snapshot_context())["model"]["epoch"] == epoch["epoch"]
                record("original_operation_id_is_idempotent_and_new_stale_context_is_rejected", {
                    "original_task_id": submitted["task_id"], "repeat": repeat, "stale": stale})

                saved_path = run / "cad-saved-copy.ansa"
                saved = await h.direct_result("ansa_model_save", {"path": str(saved_path)})
                assert saved_path.is_file() and saved_path.stat().st_size > 0 and oracle() == after
                opened = await h.direct_result("ansa_model_open", {
                    "path": str(saved_path), "backup_path": str(run / "before-cad-reopen.ansa")})
                await h.snapshot_context()
                reopened = oracle()
                assert Path(reopened["model_path"]).resolve() == saved_path.resolve() and same_content(reopened, after)
                record("imported_geometry_survives_save_copy_and_reopen", {"saved": saved, "opened": opened, "independent": reopened})

                restored = await h.direct_result("ansa_model_open", {
                    "path": str(backup_path), "backup_path": str(run / "before-original-restore.ansa")})
                await h.snapshot_context()
                original_restored = oracle()
                assert Path(original_restored["model_path"]).resolve() == backup_path.resolve()
                assert same_content(original_restored, before)
                opened_again = await h.direct_result("ansa_model_open", {
                    "path": str(saved_path), "backup_path": str(run / "before-cad-display.ansa")})
                await h.snapshot_context()
                displayed = oracle()
                assert displayed == reopened and fingerprint(source) == source_record
                record("pre_import_backup_restores_original_node_then_saved_cad_reopens", {
                    "restored": restored, "original_independent": original_restored,
                    "cad_opened": opened_again, "cad_independent": displayed})

                if wait_ui:
                    marker = run / "cad-ui-confirmation.json"
                    if marker.exists():
                        raise RuntimeError("Refuse stale CAD GUI evidence")
                    stage._json_write(run / "cad-ui-instruction.json", {
                        "identity": identity, "model_path": displayed["model_path"],
                        "window_file": saved_path.name, "expected_face_count": 1, "independent_face_area": 200.0,
                        "instruction": "在所属 ANSA 主窗口确认保存重开的平面矩形 FACE 已显示，再记录本轮确认。"})
                    print(json.dumps({"status": "waiting_for_cad_ui", "run_dir": str(run)}, ensure_ascii=True), flush=True)
                    deadline = asyncio.get_running_loop().time() + stage.PANEL_WAIT_SECONDS
                    while not marker.is_file():
                        if asyncio.get_running_loop().time() >= deadline:
                            raise TimeoutError("CAD GUI timed out; retain original task and owned instance")
                        await asyncio.sleep(.5)
                    visual = json.loads(marker.read_text(encoding="utf-8"))
                    if (visual.get("checked") is not True or visual.get("pid") != identity["pid"]
                            or visual.get("process_start") != identity["process_start"]
                            or visual.get("model_path") != displayed["model_path"]
                            or visual.get("geometry_visible") is not True):
                        raise RuntimeError("CAD GUI evidence does not match owned process and model")
                    assert oracle() == displayed
                    report["visual_acceptance"] = True
                    record("owned_main_window_rectangle_and_post_ui_independent_readback", visual)
    report["success"] = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ansa-root", required=True, type=Path)
    parser.add_argument("--runtime-home", required=True, type=Path)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--wait-ui", action="store_true")
    parser.add_argument("--keep-ansa", action="store_true")
    args = parser.parse_args()
    run, instructions = stage._load_run(args)
    if (run / "seed-baseline.ansa").exists() or (run / "cad-report.json").exists():
        raise ValueError("仅接受新准备目录；未知结果应查询原任务，禁止重放 CAD 夹具")
    source = stage.PROJECT_ROOT / "tests/fixtures/rectangle-20x10-mm.step"
    shutil.copy2(source, run / source.name)
    report = {"scope": "ANSA 2026.1 GUI/NASTRAN STEP 单矩形明确替换导入；MCP stdio → 自研 TCP → 所属主线程",
              "started_at": datetime.now(timezone.utc).isoformat(), "success": False, "cases": [],
              "visual_acceptance": False, "cad_merge_acceptance": False, "ribbon_lifecycle_acceptance": False}
    identity = helpers = None
    try:
        identity, _ansa, _grpc, helpers = stage._prepare_owned_ansa(run, instructions)
        report["identity"] = identity
        stage._json_write(run / "cad-report.json", report)
        report["fixture"] = helpers[1].rpc(identity["port"], remote_initialize_fixture,
            identity["pid"], identity["program_args"], identity["seed_path"], identity["seed_entity_id"])
        asyncio.run(verify(run, instructions, identity, helpers, report, wait_ui=args.wait_ui))
    except BaseException as exc:
        report["success"] = False
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
        report["exception_tree"] = stage._exception_tree(exc)
        (run / "cad-report.error.log").write_text(traceback.format_exc(), encoding="utf-8")
        raise
    finally:
        if identity is not None and helpers is not None and report["success"] and not args.keep_ansa:
            try:
                report["cleanup"] = stage._cleanup_owned_ansa(identity, instructions, helpers)
            except Exception as exc:
                report["cleanup"] = {"error": str(exc), "stopped_owned_process": False}
                report["success"] = False
        stage._json_write(run / "cad-report.json", report)


if __name__ == "__main__":
    main()
