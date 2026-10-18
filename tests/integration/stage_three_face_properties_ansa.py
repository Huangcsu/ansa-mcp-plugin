"""独立 NASTRAN FACE 属性查询验收；只在所属备份夹具中写入准备和重开。"""

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import sys
import traceback
import uuid

import stage_three_ansa as stage
from stage_three_cad_ansa import fingerprint, remote_initialize_fixture
from stage_three_geometry_ansa import close_numbers
from stage_three_geometry_delete_ansa import FIXTURE_SCRIPT


def remote_face_properties_readback(expected_pid, expected_arguments):
    import math
    import os
    import threading
    from ansa import base, session
    if (os.getpid() != expected_pid or list(session.ProgramArguments()) != expected_arguments
            or threading.current_thread() is not threading.main_thread()):
        raise RuntimeError("FACE properties oracle requires the owned main thread")
    deck = base.CurrentDeck()
    rows = []
    for face in sorted(base.CollectEntities(deck, None, "FACE"), key=lambda e: e._id):
        direction = list(base.GetFaceOrientation(face))
        length = math.hypot(*direction)
        if length <= 0 or not math.isfinite(length):
            raise RuntimeError("Independent fixture direction is invalid")
        rows.append({"id": face._id, "area": base.GetFaceArea(face), "center": list(base.Cog(face)),
            "orientation": direction, "unit_normal": [value / length for value in direction],
            "bounds": list(base.BoundBox([face])),
            "fields": base.GetEntityCardValues(deck, face, ("ID", "PID", "Thickness", "Frozen")),
            "cons_ids": sorted(e._id for e in base.CollectEntities(deck, face, "CONS"))})
    identities = {kind: sorted([e.ansa_type(deck), e._id] for e in base.CollectEntities(deck, None, kind))
        for kind in ("CONS", "CURVE", "GRID", "SHELL", "__PROPERTIES__", "__MATERIALS__")}
    grids = [{"id": e._id, "fields": base.GetEntityCardValues(deck, e, ("X1", "X2", "X3"))}
        for e in sorted(base.CollectEntities(deck, None, "GRID"), key=lambda e: e._id)]
    return {"deck": deck, "model_path": base.DataBaseName(), "faces": rows,
        "visible_face_ids": sorted(e._id for e in base.CollectEntities(deck, None, "FACE", filter_visible=True)),
        "identities": identities, "grids": grids}


def check_enriched(result, expected, requested):
    assert result["read_only"] is True and result["scope"] == "explicit"
    assert result["include_geometry"] is True and result["complete"] is True
    assert [row["face"]["id"] for row in result["faces"]] == requested
    by_id = {row["id"]: row for row in expected["faces"]}
    for row in result["faces"]:
        assert row["found"] is True and row["complete"] is True
        reference = by_id[row["face"]["id"]]
        for key in ("area", "center", "orientation", "unit_normal", "bounds"):
            close_numbers(row[key], reference[key])
        close_numbers(math.hypot(*row["unit_normal"]), 1.)
    json.dumps(result, ensure_ascii=False, allow_nan=False)


async def verify(run, instructions, identity, helpers, report, *, wait_ui):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    one, two, _original = helpers
    parameters = StdioServerParameters(command=sys.executable,
        args=["-B", "-m", "ansa_mcp_plugin", "--runtime-home", instructions["runtime_home"]],
        env={**os.environ, "PYTHONPATH": str(stage.PROJECT_ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"})

    def oracle():
        return two.rpc(identity["port"], remote_face_properties_readback, identity["pid"], identity["program_args"])

    def record(name, evidence):
        report["cases"].append({"name": name, "ok": True, "evidence": evidence})
        stage._json_write(run / "face-properties-report.json", report)
        print(json.dumps({"case": name, "ok": True}), flush=True)

    with (run / "face-properties-backend.stderr.log").open("w", encoding="utf-8") as log:
        async with stdio_client(parameters, errlog=log) as (reader, writer):
            async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=30)) as client:
                await client.initialize()
                h = stage.McpHarness(client, report, run)
                await stage._bind_for_run(h, identity, one, two, identity["port"])
                schemas = await h.list_schema()
                assert oracle()["faces"] == []
                await h.direct_result("ansa_script_execute", {"script": FIXTURE_SCRIPT})
                before = oracle()
                assert len(before["faces"]) == 3 and len(before["grids"]) == 1
                ordered = sorted(before["faces"], key=lambda row: row["center"][0])
                for row, area, bounds in zip(ordered, (200., 100., 50.), (
                    [0., 0., 0., 20., 10., 0.], [30., 0., 10., 40., 10., 10.],
                    [50., 0., 0., 50., 5., 10.])):
                    close_numbers(row["area"], area)
                    close_numbers(row["bounds"], bounds)
                close_numbers(ordered[0]["unit_normal"], [0., 0., 1.])
                close_numbers(ordered[1]["unit_normal"], [0., 0., -1.])
                close_numbers(abs(ordered[2]["unit_normal"][0]), 1.)
                await h.direct_result("ansa_model_save", {"path": str(run / "face-properties-before.ansa")})
                before = oracle()
                record("schemas_analytic_three_direction_fixture_and_backup", {"schemas": schemas,
                    "independent": before, "backup": fingerprint(run / "face-properties-before.ansa")})
                ids = [row["id"] for row in before["faces"]]
                fields = {"action": "face_properties", "deck": before["deck"],
                    "options": {"face_ids": ids, "include_geometry": True}}
                enriched = await h.direct_result("ansa_model_inspect", fields)
                check_enriched(enriched, before, ids)
                assert oracle() == before
                record("orientation_unit_direction_and_bounds_match_independent_readback", enriched)

                defaults = await h.direct_result("ansa_model_inspect", {
                    "action": "face_properties", "deck": before["deck"], "options": {"face_ids": ids}})
                assert all(not any(key in row for key in ("orientation", "unit_normal", "bounds"))
                    for row in defaults["faces"])
                for row, expected in zip(defaults["faces"], before["faces"]):
                    close_numbers(row["area"], expected["area"])
                    close_numbers(row["center"], expected["center"])
                requested = [ids[-1], ids[0]]
                subset = await h.direct_result("ansa_model_inspect", {**fields,
                    "options": {"face_ids": requested, "include_geometry": True}})
                check_enriched(subset, before, requested)
                missing = await h.direct_result("ansa_model_inspect", {**fields,
                    "options": {"face_ids": [ids[0], 99999999], "include_geometry": True}})
                assert missing["complete"] is False
                assert missing["faces"][0]["complete"] is True
                assert missing["faces"][1]["found"] is False and missing["faces"][1]["complete"] is False
                assert oracle() == before
                record("default_compatibility_explicit_subset_and_missing_face_completeness", {
                    "defaults": defaults, "subset": subset, "missing": missing})

                invalids = []
                for options in ({"face_ids": ids, "include_geometry": 1},
                    {"face_ids": ids, "include_geometry": "true"},
                    {"face_ids": []}, {"face_ids": [ids[0], ids[0]]}, {"face_ids": [True]},
                    {"face_ids": ids, "include_geometry": True, "auto_fix": True}):
                    invalids.append(await h.direct_result("ansa_model_inspect", {
                        **fields, "options": options}, expected="failed"))
                invalids.append(await h.direct_result("ansa_model_inspect", {**fields,
                    "deck": before["deck"] + 1}, expected="failed"))
                assert oracle() == before
                record("strict_options_ids_unknown_fields_and_other_deck_reject_without_changes", invalids)

                original_target, original_model = h.target.copy(), h.model.copy()
                op = "face-properties-" + uuid.uuid4().hex
                submitted, completed = await h.task("ansa_model_inspect", fields, operation_id=op)
                repeated = await h.call("ansa_model_inspect", {"operation_id": op,
                    "expected_target": original_target, "expected_model": original_model, **fields})
                assert repeated["task_id"] == submitted["task_id"]
                assert repeated["data"]["result"] == completed["data"]["result"]
                assert oracle() == before
                record("original_operation_idempotency_and_read_only_model_state", {
                    "task_id": submitted["task_id"], "repeated_task_id": repeated["task_id"],
                    "result": completed["data"]["result"]})

                saved_path = run / "face-properties-after.ansa"
                await h.direct_result("ansa_model_save", {"path": str(saved_path)})
                saved = fingerprint(saved_path)
                await h.direct_result("ansa_model_open", {"path": str(saved_path),
                    "backup_path": str(run / "face-properties-reopen-backup.ansa")})
                await h.snapshot_context()
                reopened = oracle()
                assert {key: value for key, value in reopened.items() if key != "model_path"} == {
                    key: value for key, value in before.items() if key != "model_path"}
                after = await h.direct_result("ansa_model_inspect", fields)
                check_enriched(after, reopened, ids)
                assert fingerprint(saved_path) == saved and h.model["epoch"] != original_model["epoch"]
                stale = await h.call("ansa_model_inspect", {"operation_id": "face-properties-stale-" + uuid.uuid4().hex,
                    "expected_target": h.target, "expected_model": original_model, **fields}, allow_error=True)
                assert stale["ok"] is False and stale["task_id"] is None and oracle() == reopened
                record("saved_model_reopen_preserves_properties_and_rejects_old_context", {
                    "independent": reopened, "result": after, "saved": saved, "stale": stale})
                report["core_acceptance"] = True
                stage._json_write(run / "face-properties-report.json", report)

                if wait_ui:
                    marker = run / "face-properties-ui-confirmation.json"
                    if marker.exists():
                        raise RuntimeError("Refuse stale FACE properties GUI evidence")
                    stage._json_write(run / "face-properties-ui-instruction.json", {"identity": identity,
                        "model_path": reopened["model_path"], "face_ids": ids,
                        "instruction": "所属主窗口应保留两个水平矩形 FACE 和一个竖直矩形 FACE，均可见。"})
                    print(json.dumps({"status": "waiting_for_face_properties_ui", "run_dir": str(run)}), flush=True)
                    deadline = asyncio.get_running_loop().time() + stage.PANEL_WAIT_SECONDS
                    while not marker.is_file():
                        if asyncio.get_running_loop().time() >= deadline:
                            raise TimeoutError("FACE properties GUI timed out; retain owned tasks and process")
                        await asyncio.sleep(.5)
                    visual = json.loads(marker.read_text(encoding="utf-8"))
                    if (visual.get("checked") is not True or visual.get("pid") != identity["pid"]
                            or visual.get("process_start") != identity["process_start"]
                            or visual.get("model_path") != reopened["model_path"]
                            or visual.get("all_three_faces_visible") is not True):
                        raise RuntimeError("FACE properties GUI evidence mismatches the owned model")
                    assert oracle() == reopened
                    report["visual_acceptance"] = True
                    record("owned_main_window_three_faces_and_post_ui_independent_readback", visual)
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
    if (run / "seed-baseline.ansa").exists() or (run / "face-properties-report.json").exists():
        raise ValueError("只接受新准备目录；未知结果先查询原任务，不重放夹具")
    report = {"scope": "ANSA 2026.1 Windows GUI/NASTRAN 显式平面 FACE 只读属性",
        "started_at": datetime.now(timezone.utc).isoformat(), "success": False, "cases": [],
        "core_acceptance": False, "visual_acceptance": False}
    identity = helpers = None
    try:
        identity, _ansa, _grpc, helpers = stage._prepare_owned_ansa(run, instructions)
        report["identity"] = identity
        stage._json_write(run / "face-properties-report.json", report)
        report["fixture"] = helpers[1].rpc(identity["port"], remote_initialize_fixture,
            identity["pid"], identity["program_args"], identity["seed_path"], identity["seed_entity_id"])
        asyncio.run(verify(run, instructions, identity, helpers, report, wait_ui=args.wait_ui))
    except BaseException as exc:
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
        report["exception_tree"] = stage._exception_tree(exc)
        (run / "face-properties-report.error.log").write_text(traceback.format_exc(), encoding="utf-8")
        raise
    finally:
        if identity is not None and helpers is not None and report["success"] and not args.keep_ansa:
            try:
                report["cleanup"] = stage._cleanup_owned_ansa(identity, instructions, helpers)
            except Exception as exc:
                report["cleanup"] = {"error": str(exc), "stopped_owned_process": False}
                report["success"] = False
        stage._json_write(run / "face-properties-report.json", report)

    if not report["success"]:
        raise RuntimeError("FACE properties acceptance did not finish successfully; inspect its saved report")


if __name__ == "__main__":
    main()
