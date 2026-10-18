"""独占 ANSA 主窗口验证法向排名和备份后的受控 FACE 删除。"""

import argparse
import asyncio
import copy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import traceback
import uuid

import stage_three_ansa as stage
from stage_three_cad_ansa import fingerprint, remote_initialize_fixture
from stage_three_geometry_ansa import close_numbers


def remote_geometry_delete_readback(expected_pid, expected_arguments):
    import math
    import os
    import threading
    from ansa import base, mesh, session
    if (os.getpid() != expected_pid or list(session.ProgramArguments()) != expected_arguments
            or threading.current_thread() is not threading.main_thread()):
        raise RuntimeError("Geometry deletion oracle requires the owned ANSA main thread")
    deck = base.CurrentDeck()
    faces = []
    for face in sorted(base.CollectEntities(deck, None, "FACE"), key=lambda entity: entity._id):
        orientation = list(base.GetFaceOrientation(face))
        length = math.hypot(*orientation)
        if length <= 0 or not math.isfinite(length):
            raise RuntimeError("Fixture orientation must be finite and nonzero")
        cons = base.CollectEntities(deck, face, "CONS")
        macro = base.GetFaceMacro(face)
        shells = mesh.GetMacroShells([macro])
        faces.append({"id": face._id, "area": base.GetFaceArea(face), "center": list(base.Cog(face)),
            "bounds": list(base.BoundBox([face])), "normal": [value/length for value in orientation],
            "fields": base.GetEntityCardValues(deck, face, ("ID", "PID", "Thickness", "Frozen")),
            "cons_ids": sorted(entity._id for entity in cons),
            "adjacent_face_ids": sorted({entity._id for entity in base.GetFacesOfCons(cons)}),
            "macro_face_ids": sorted(entity._id for entity in base.GetFacesOfMacros([macro])),
            "shell_ids": [] if shells == 0 else sorted(entity._id for entity in shells)})
    identities = {}
    for kind in ("CONS", "CURVE", "GRID", "SHELL", "__PROPERTIES__", "__MATERIALS__"):
        entities = base.CollectEntities(deck, None, kind)
        identities[kind] = sorted([entity.ansa_type(deck), entity._id] for entity in entities)
    grids = [{"id": entity._id, "coordinates": base.GetEntityCardValues(deck, entity, ("X1", "X2", "X3"))}
             for entity in base.CollectEntities(deck, None, "GRID")]
    return {"deck": deck, "model_path": base.DataBaseName(), "faces": faces,
            "visible_face_ids": sorted(entity._id for entity in base.CollectEntities(deck, None, "FACE", filter_visible=True)),
            "identities": identities, "grids": grids}


FIXTURE_SCRIPT = """from ansa import base
points_by_face = [
    [[0.,0.,0.],[20.,0.,0.],[20.,10.,0.],[0.,10.,0.]],
    [[30.,0.,10.],[40.,0.,10.],[40.,10.,10.],[30.,10.,10.]],
    [[50.,0.,0.],[50.,5.,0.],[50.,5.,10.],[50.,0.,10.]],
]
faces = []
for points in points_by_face:
    curves = [base.CurvesNew([points[i],points[(i+1)%4]]) for i in range(4)]
    if any(curve is None for curve in curves): raise RuntimeError('fixture curve failed')
    created = base.FacesNewPlanar(curves, ret_ents=True)
    if not created or len(created) != 1: raise RuntimeError('fixture face failed')
    faces.append(created[0])
for face, wanted_z in zip(faces[:2], [1., -1.]):
    if base.GetFaceOrientation(face)[2] * wanted_z < 0:
        if base.Or([face]) != 1: raise RuntimeError('fixture visibility failed')
        base.InvertGeomOrientation()
if base.Or(faces) != 1: raise RuntimeError('fixture all-face visibility failed')
RESULT = {'fixture_face_ids': [face._id for face in faces]}
"""


async def verify(run, instructions, identity, helpers, report, *, wait_ui):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    one, two, _original = helpers
    parameters = StdioServerParameters(command=sys.executable,
        args=["-B", "-m", "ansa_mcp_plugin", "--runtime-home", instructions["runtime_home"]],
        env={**os.environ, "PYTHONPATH": str(stage.PROJECT_ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"})

    def record(name, evidence):
        report["cases"].append({"name": name, "ok": True, "evidence": evidence})
        stage._json_write(run / "geometry-delete-report.json", report)
        print(json.dumps({"case": name, "ok": True}), flush=True)

    def oracle():
        return two.rpc(identity["port"], remote_geometry_delete_readback, identity["pid"], identity["program_args"])

    with (run / "geometry-delete-backend.stderr.log").open("w", encoding="utf-8") as log:
        async with stdio_client(parameters, errlog=log) as (reader, writer):
            async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=30)) as client:
                await client.initialize()
                h = stage.McpHarness(client, report, run)
                await stage._bind_for_run(h, identity, one, two, identity["port"])
                schemas = await h.list_schema()
                await h.direct_result("ansa_script_execute", {"script": FIXTURE_SCRIPT})
                before = oracle()
                report["fixture_readback"] = before
                stage._json_write(run / "geometry-delete-report.json", report)
                by_x = sorted(before["faces"], key=lambda face: face["center"][0])
                assert len(by_x) == 3 and len(before["grids"]) == 1
                for face, area, center, normal in zip(by_x, (200., 100., 50.),
                        ([10.,5.,0.], [35.,5.,10.], [50.,2.5,5.]), ([0.,0.,1.], [0.,0.,-1.], [-1.,0.,0.])):
                    close_numbers(face["area"], area)
                    close_numbers(face["center"], center)
                    close_numbers(face["normal"], normal)
                    assert face["adjacent_face_ids"] == [face["id"]] and face["shell_ids"] == []
                backup_path = run / "geometry-before-delete.ansa"
                await h.direct_result("ansa_model_save", {"path": str(backup_path)})
                backup = fingerprint(backup_path)
                before = oracle()
                record("schemas_fixture_normal_oracle_and_recoverable_backup", {"schemas": schemas, "independent": before, "backup": backup})

                rankings = {}
                for query, descending in (("normal_pos_z", True), ("normal_neg_z", False)):
                    expected = [face["id"] for face in sorted(before["faces"],
                        key=lambda face: ((-face["normal"][2] if descending else face["normal"][2]), face["id"]))]
                    value = await h.direct_result("ansa_geometry_select", {"deck": before["deck"], "query": query, "limit": 3})
                    assert value["face_ids"] == expected and value["source_count"] == value["matched_count"] == 3
                    for candidate in value["candidates"]:
                        face = next(face for face in before["faces"] if face["id"] == candidate["face"]["id"])
                        close_numbers(candidate["normal"], face["normal"])
                        close_numbers(candidate["score"], face["normal"][2])
                    rankings[query] = value
                assert oracle() == before
                report["normal_query_acceptance"] = True
                record("normal_z_rankings_include_all_signs_and_preserve_model_visibility", rankings)

                selected = [face["id"] for face in by_x[:2]]
                preview = await h.direct_result("ansa_geometry_preview", {"deck": before["deck"], "query": "normal_pos_z",
                    "face_ids": selected, "limit": 2, "include_delete_conditions": True})
                assert preview["delete_ready"] is True and preview["delete_rejections"] == []
                conditions = preview["delete_conditions"]
                assert sorted(row["id"] for row in conditions) == sorted(selected)
                for condition in conditions:
                    face = next(face for face in before["faces"] if face["id"] == condition["id"])
                    assert condition["expected_fields"] == face["fields"] and condition["expected_cons_ids"] == face["cons_ids"]
                    for key in ("area", "center", "bounds", "normal"):
                        close_numbers(condition["expected_geometry"][key], face[key])
                assert oracle() == before and fingerprint(backup_path) == backup
                record("fresh_deletion_conditions_preview_has_no_write_or_ui_effects", preview)

                failures = []
                for change in ("area", "cons", "missing"):
                    bad = copy.deepcopy(conditions)
                    if change == "area":
                        bad[1]["expected_geometry"]["area"] += 1.
                    elif change == "cons":
                        bad[1]["expected_cons_ids"] = [2147483647]
                    else:
                        bad[1]["id"] = 2147483647
                    _submitted, failed = await h.task("ansa_geometry_delete", {"deck": before["deck"], "faces": bad}, expected="failed")
                    assert oracle() == before and fingerprint(backup_path) == backup
                    failures.append(failed)
                record("whole_batch_rejects_stale_geometry_boundary_and_missing_face_before_any_deletion", failures)
                _submitted, generic_failed = await h.task("ansa_entity_delete", {"deck": before["deck"],
                    "entity_type": "FACE", "entities": [{"entity_id": conditions[0]["id"],
                    "expected_fields": conditions[0]["expected_fields"]}]}, expected="failed")
                assert oracle() == before
                record("generic_card_only_face_deletion_cannot_bypass_geometry_conditions", generic_failed)

                operation = "geometry-delete-" + uuid.uuid4().hex
                submitted, completed = await h.task("ansa_geometry_delete", {"deck": before["deck"], "faces": conditions}, operation_id=operation)
                deleted = completed["data"]["result"]
                after = oracle()
                assert deleted["scope_verified"] is True and deleted["deleted_face_ids"] == sorted(selected)
                assert deleted["remaining_requested_face_ids"] == [] and deleted["changed_protected_types"] == []
                assert deleted["outside_removed_cons_count"] == deleted["unexpected_added_cons_count"] == 0
                assert deleted["force"] is False and deleted["compress"] is False
                assert after["faces"] == [by_x[2]] and after["grids"] == before["grids"]
                assert after["visible_face_ids"] == [by_x[2]["id"]]
                for kind in ("GRID", "SHELL", "CURVE", "__PROPERTIES__", "__MATERIALS__"):
                    assert after["identities"][kind] == before["identities"][kind]
                repeat = await h.call("ansa_geometry_delete", {"operation_id": operation, "expected_target": h.target,
                    "expected_model": h.model, "deck": before["deck"], "faces": conditions})
                assert repeat["task_id"] == submitted["task_id"]
                assert repeat["data"]["result"] == deleted and oracle() == after
                assert fingerprint(backup_path) == backup
                report["face_delete_acceptance"] = True
                record("explicit_two_face_deletion_scope_and_same_operation_idempotency", {"result": deleted, "independent": after, "repeat": repeat})

                saved_path = run / "geometry-after-delete.ansa"
                await h.direct_result("ansa_model_save", {"path": str(saved_path)})
                saved = fingerprint(saved_path)
                old_model = dict(h.model)
                await h.direct_result("ansa_model_open", {"path": str(saved_path),
                    "backup_path": str(run / "geometry-reopen-backup.ansa")})
                await h.snapshot_context()
                reopened = oracle()
                assert reopened["faces"] == after["faces"] and reopened["grids"] == after["grids"]
                assert reopened["identities"] == after["identities"] and fingerprint(saved_path) == saved
                stale = await h.call("ansa_geometry_delete", {"operation_id": "stale-"+uuid.uuid4().hex,
                    "expected_target": h.target, "expected_model": old_model, "deck": before["deck"], "faces": conditions}, allow_error=True)
                assert stale["ok"] is False and stale["task_id"] is None
                assert oracle() == reopened
                record("saved_deletion_reopens_and_stale_model_context_is_rejected", {"independent": reopened, "saved": saved, "stale": stale})

                if wait_ui:
                    marker = run / "geometry-delete-ui-confirmation.json"
                    if marker.exists():
                        raise RuntimeError("Refuse stale geometry deletion GUI evidence")
                    stage._json_write(run / "geometry-delete-ui-instruction.json", {"identity": identity,
                        "model_path": reopened["model_path"], "remaining_face_id": by_x[2]["id"],
                        "expected_visible_face_ids": reopened["visible_face_ids"],
                        "instruction": "在所属主窗口确认灰色 FACE 只保留右侧竖直矩形，Database FACE 为1；水平 CURVE 可保留但不可再有水平 FACE。"})
                    print(json.dumps({"status": "waiting_for_geometry_delete_ui", "run_dir": str(run)}), flush=True)
                    deadline = asyncio.get_running_loop().time() + stage.PANEL_WAIT_SECONDS
                    while not marker.is_file():
                        if asyncio.get_running_loop().time() >= deadline:
                            raise TimeoutError("Geometry deletion GUI timed out; query original tasks and retain owned instance")
                        await asyncio.sleep(.5)
                    visual = json.loads(marker.read_text(encoding="utf-8"))
                    if (visual.get("checked") is not True or visual.get("pid") != identity["pid"]
                            or visual.get("process_start") != identity["process_start"]
                            or visual.get("model_path") != reopened["model_path"]
                            or visual.get("one_vertical_face_visible") is not True):
                        raise RuntimeError("Geometry deletion GUI evidence does not match the owned instance and model")
                    assert oracle() == reopened
                    report["visual_acceptance"] = True
                    record("owned_main_window_single_remaining_face_and_post_ui_readback", visual)
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
    if (run / "seed-baseline.ansa").exists() or (run / "geometry-delete-report.json").exists():
        raise ValueError("仅接受全新准备目录；未知结果须查询原任务，禁止重放删除")
    report = {"scope": "ANSA 2026.1 GUI/NASTRAN 法向Z排名、只读条件预览及两个独立未网格FACE删除",
              "started_at": datetime.now(timezone.utc).isoformat(), "success": False, "cases": [],
              "visual_acceptance": False, "normal_query_acceptance": False, "face_delete_acceptance": False}
    identity = helpers = None
    try:
        identity, _ansa, _grpc, helpers = stage._prepare_owned_ansa(run, instructions)
        report["identity"] = identity
        stage._json_write(run / "geometry-delete-report.json", report)
        report["fixture"] = helpers[1].rpc(identity["port"], remote_initialize_fixture,
            identity["pid"], identity["program_args"], identity["seed_path"], identity["seed_entity_id"])
        asyncio.run(verify(run, instructions, identity, helpers, report, wait_ui=args.wait_ui))
    except BaseException as exc:
        report["success"] = False
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
        report["exception_tree"] = stage._exception_tree(exc)
        (run / "geometry-delete-report.error.log").write_text(traceback.format_exc(), encoding="utf-8")
        raise
    finally:
        if identity is not None and helpers is not None and report["success"] and not args.keep_ansa:
            try:
                report["cleanup"] = stage._cleanup_owned_ansa(identity, instructions, helpers)
            except Exception as exc:
                report["cleanup"] = {"error": str(exc), "stopped_owned_process": False}
                report["success"] = False
        stage._json_write(run / "geometry-delete-report.json", report)


if __name__ == "__main__":
    main()
