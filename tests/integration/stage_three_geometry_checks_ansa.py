"""Owned native acceptance for explicit FACE Basic checks; no automatic fixes."""

import hashlib
import os
from pathlib import Path
import sys
from datetime import timedelta
import uuid

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import stage_three_ansa as common
import stage_three_api_catalog_ansa as owned
from stage_three_connections_ansa import independent_script

ROOT = Path(__file__).resolve().parents[2]


def remote_fixture(expected_pid, expected_arguments, step_path):
    import os
    import threading
    from ansa import base, constants, session
    if (os.getpid() != expected_pid or list(session.ProgramArguments()) != expected_arguments
            or threading.current_thread() is not threading.main_thread()):
        raise RuntimeError("Geometry fixture requires its owned ANSA main thread")
    deck = base.CurrentDeck()
    if deck != constants.NASTRAN or base.CollectEntities(deck, None, "FACE"):
        raise RuntimeError("Geometry fixture requires the owned NASTRAN seed without faces")
    status = base.Open(step_path)
    if (type(status) is not int or status != 0
            or os.path.normcase(os.path.abspath(base.DataBaseName())) != os.path.normcase(os.path.abspath(step_path))):
        raise RuntimeError("Geometry fixture STEP replacement was not confirmed")
    initial_faces = base.CollectEntities(deck, None, "FACE")
    if type(initial_faces) is not list or len(initial_faces) != 1:
        raise RuntimeError("Geometry fixture STEP must contain exactly one FACE")
    faces = [initial_faces[0]._id]
    for points in (
        [[30., 0., 0.], [40., 0., 0.], [40., 10., 0.], [30., 10., 0.]],
        [[60., 0., 0.], [65., 0., 0.], [65., 10., 0.], [60., 10., 0.]],
    ):
        curves = [base.CurvesNew([points[i], points[(i + 1) % 4]]) for i in range(4)]
        if any(curve is None for curve in curves):
            raise RuntimeError("Geometry fixture curve creation failed")
        created = base.FacesNewPlanar(curves, ret_ents=True)
        if type(created) is not list or len(created) != 1:
            raise RuntimeError("Geometry fixture face creation failed")
        faces.append(created[0]._id)
    return {"deck": deck, "face_ids": faces}


def remote_oracle(expected_pid, expected_arguments):
    import math
    import os
    import threading
    from ansa import base, session
    if (os.getpid() != expected_pid or list(session.ProgramArguments()) != expected_arguments
            or threading.current_thread() is not threading.main_thread()):
        raise RuntimeError("Geometry oracle requires its owned ANSA main thread")
    deck = base.CurrentDeck()

    def collect(kind):
        values = base.CollectEntities(deck, None, kind, recursive=False)
        if type(values) is not list or len(values) > 10000:
            raise RuntimeError("Independent geometry oracle collection is incomplete")
        return sorted(values, key=lambda entity: entity._id)

    def vector(value, length):
        result = list(value)
        if len(result) != length or any(type(x) not in (int, float) or not math.isfinite(x) for x in result):
            raise RuntimeError("Independent geometry oracle vector is incomplete")
        return result

    def card(entity, fields):
        available = entity.card_fields(deck)
        if type(available) is not list or not set(fields).issubset(available):
            raise RuntimeError("Independent geometry oracle card fields unavailable")
        values = base.GetEntityCardValues(deck, entity, fields)
        if type(values) is not dict or set(values) != set(fields):
            raise RuntimeError("Independent geometry oracle card values incomplete")
        return values

    faces = []
    for face in collect("FACE"):
        cons = base.CollectEntities(deck, face, "CONS", recursive=False)
        if type(cons) is not list or len(cons) != 4:
            raise RuntimeError("Rectangle fixture must retain four boundary CONS")
        area = base.GetFaceArea(face)
        if type(area) not in (int, float) or not math.isfinite(area) or area <= 0:
            raise RuntimeError("Independent FACE area is invalid")
        faces.append({"id": face._id, "card": card(face, ("ID", "PID", "Thickness", "Frozen")),
            "area": area, "center": vector(base.Cog(face), 3),
            "bounds": vector(base.BoundBox(face), 6), "cons_ids": sorted(e._id for e in cons)})
    cons = [{"id": entity._id, "bounds": vector(base.BoundBox(entity), 6),
             "face_ids": sorted(e._id for e in base.GetFacesOfCons(entity))} for entity in collect("CONS")]
    return {"deck": deck, "database": base.DataBaseName(), "faces": faces, "cons": cons,
        "curves": [{"id": e._id, "bounds": vector(base.BoundBox(e), 6)} for e in collect("CURVE")],
        "nodes": [{"id": e._id, "card": card(e, ("X1", "X2", "X3"))} for e in collect("GRID")],
        "elements": sorted([e.ansa_type(deck), e._id] for e in collect("__ELEMENTS__")),
        "properties": sorted([e.ansa_type(deck), e._id] for e in collect("__PROPERTIES__")),
        "materials": sorted([e.ansa_type(deck), e._id] for e in collect("__MATERIALS__")),
        "visible_face_ids": sorted(e._id for e in base.CollectEntities(deck, None, "FACE", filter_visible=True))}


async def verify(instructions, identity, helpers, run_dir, report, path):
    one, two, _ = helpers
    parameters = StdioServerParameters(command=sys.executable,
        args=["-B", "-m", "ansa_mcp_plugin", "--runtime-home", instructions["runtime_home"]],
        env={**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"})
    receipts = []

    async def oracle(h):
        return await independent_script(h, remote_oracle, identity["pid"], identity["program_args"])

    async def check(h, ids, single_cons):
        request = {"action": "geometry_check", "deck": baseline["deck"],
            "options": {"check": "basic", "entity_type": "FACE", "entity_ids": ids, "single_cons": single_cons}}
        operation_id = "geometry-check-" + uuid.uuid4().hex
        submitted, completed = await h.task("ansa_model_inspect", request, operation_id=operation_id)
        result = completed["data"]["result"]
        expected_cons = sorted({cons for face in baseline["faces"] if face["id"] in ids for cons in face["cons_ids"]}) if single_cons else []
        if (result["failed_cons_ids"] != expected_cons
                or result["affected_face_ids"] != (sorted(ids) if single_cons else [])
                or result["issue_counts"]["error"] != len(expected_cons)
                or result["issue_counts"]["warning"] != 0
                or result["read_only"] is not True or result["auto_fix"] is not False
                or result["reports_truncated"] is not False):
            raise AssertionError(f"Geometry check differs from independent rectangle boundaries: {result}")
        observed_context = {row["face"]["id"]: sorted(cons["id"] for cons in row["cons"]) for row in result["boundary_context"]}
        expected_context = {row["id"]: row["cons_ids"] for row in baseline["faces"] if row["id"] in ids}
        if observed_context != expected_context or await oracle(h) != baseline:
            raise AssertionError("Geometry check changed the model/view or reported an incorrect boundary context")
        receipts.append({"request": request, "operation_id": operation_id, "task_id": submitted["task_id"],
                         "result": result, "target": completed["target"], "model": completed["model"]})
        return result

    with (run_dir / "geometry-checks-backend.stderr.log").open("w", encoding="utf-8") as log:
        async with stdio_client(parameters, errlog=log) as (reader, writer):
            async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=120)) as client:
                await client.initialize()
                h = common.McpHarness(client, report, run_dir)
                report["schema"] = await h.list_schema()
                await common._bind_for_run(h, identity, one, two, identity["port"])
                step_path = ROOT / "tests/fixtures/rectangle-20x10-mm.step"
                fixture = await independent_script(h, remote_fixture, identity["pid"], identity["program_args"], str(step_path), allow_model_change=True)
                await h.snapshot_context()
                baseline = await oracle(h)
                faces = sorted(baseline["faces"], key=lambda row: row["center"][0])
                ids = [row["id"] for row in faces]
                if (len(faces) != 3 or len(baseline["cons"]) != 12 or baseline["nodes"]
                        or baseline["elements"] or [round(row["area"], 7) for row in faces] != [200., 100., 50.]):
                    raise AssertionError("Independent geometry fixture differs from the three rectangle definitions")
                for face in faces:
                    if any(row["face_ids"] != [face["id"]] for row in baseline["cons"] if row["id"] in face["cons_ids"]):
                        raise AssertionError("Rectangle boundary reverse ownership differs")
                common._json_write(run_dir / "geometry-checks-independent-baseline.json", baseline)
                owned.record(report, path, "owned_fixture_three_rectangles_boundary_ownership_and_schema", {"fixture": fixture, "independent": baseline})

                clean = await check(h, ids[:1], False)
                positive = await check(h, ids[:1], True)
                subset = await check(h, ids[1:2], True)
                mixed = await check(h, ids[:2], True)
                owned.record(report, path, "default_clean_single_cons_positive_subsets_and_complete_native_reports", {"clean": clean, "positive": positive, "subset": subset, "mixed": mixed})

                visible = await h.direct_result("ansa_model_inspect", {"action": "visibility_snapshot", "deck": baseline["deck"]})
                await h.direct_result("ansa_model_inspect", {"action": "entity_visibility", "deck": baseline["deck"],
                    "options": {"action": "hide", "entity_type": "FACE", "entity_ids": ids[:1], "expected_visible": visible["visible_ids"]}})
                hidden = await oracle(h)
                if ids[0] in hidden["visible_face_ids"]:
                    raise AssertionError("Selected FACE did not become hidden")
                baseline = hidden
                hidden_result = await check(h, ids[:1], True)
                owned.record(report, path, "hidden_explicit_face_checked_without_changing_visibility_or_outside_face", hidden_result)

                rejected = []
                valid_options = dict(receipts[0]["request"]["options"])
                for change in ({"entity_ids": []}, {"entity_ids": [ids[0], ids[0]]}, {"entity_ids": [True]},
                        {"entity_ids": [str(ids[0])]}, {"entity_ids": [2147483646]}, {"entity_type": "CONS"},
                        {"check": "extensive"}, {"single_cons": 1}, {"single_cons": "true"},
                        {"fix": True}, {"scope": "all"}):
                    _, failed = await h.task("ansa_model_inspect", {"action": "geometry_check", "deck": baseline["deck"],
                        "options": {**valid_options, **change}}, expected="failed")
                    rejected.append({"task_id": failed["task_id"], "error": failed["error"]})
                _, failed = await h.task("ansa_model_inspect", {"action": "geometry_check", "deck": 2, "options": valid_options}, expected="failed")
                rejected.append({"task_id": failed["task_id"], "error": failed["error"]})
                first = receipts[1]
                _, replay = await h.task("ansa_model_inspect", first["request"], operation_id=first["operation_id"])
                if replay["task_id"] != first["task_id"] or replay["data"]["result"] != first["result"]:
                    raise AssertionError("Original geometry check receipt differs")
                conflict = await h.call("ansa_model_inspect", {"operation_id": first["operation_id"],
                    "expected_target": h.target, "expected_model": h.model, **first["request"],
                    "options": {**first["request"]["options"], "single_cons": False}}, allow_error=True)
                if conflict.get("ok") is not False or conflict.get("task_id") is not None or await oracle(h) != baseline:
                    raise AssertionError("Rejected or duplicate geometry check changed the model")
                owned.record(report, path, "strict_rejections_original_receipt_and_operation_conflict_without_effects", {"rejected": rejected, "original_task_id": first["task_id"]})

                saved = run_dir / "accepted-geometry-checks.ansa"
                await h.direct_result("ansa_model_save", {"path": str(saved)})
                saved_hash = hashlib.sha256(saved.read_bytes()).hexdigest()
                baseline = await oracle(h)
                for index in (1, 2):
                    old_target, old_model = dict(h.target), dict(h.model)
                    await h.direct_result("ansa_model_open", {"path": str(saved), "backup_path": str(run_dir / f"geometry-reopen-{index}-backup.ansa")})
                    await h.snapshot_context()
                    if h.model["epoch"] == old_model["epoch"]:
                        raise AssertionError("Geometry fixture reopen did not advance model epoch")
                    reopened = await oracle(h)
                    if {key: value for key, value in reopened.items() if key != "database"} != {key: value for key, value in baseline.items() if key != "database"}:
                        raise AssertionError("Geometry model differs after save/reopen")
                    baseline = reopened
                    await check(h, ids[:2], True)
                    stale = await h.call("ansa_model_inspect", {"operation_id": "stale-" + uuid.uuid4().hex,
                        "expected_target": old_target, "expected_model": old_model, **first["request"]}, allow_error=True)
                    if stale.get("ok") is not False or stale.get("task_id") is not None:
                        raise AssertionError("Stale geometry context accepted")
                if hashlib.sha256(saved.read_bytes()).hexdigest() != saved_hash:
                    raise AssertionError("Geometry check modified the saved fixture")
                previous_backend = h.target["backend_id"]
                owned.record(report, path, "save_same_path_reopen_readback_and_stale_context", {"saved": str(saved), "sha256": saved_hash, "independent": baseline})

    with (run_dir / "geometry-checks-restarted-backend.stderr.log").open("w", encoding="utf-8") as log:
        async with stdio_client(parameters, errlog=log) as (reader, writer):
            async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=120)) as client:
                await client.initialize()
                h = common.McpHarness(client, report, run_dir)
                status = await h.call("ansa_connection_status")
                if status["data"]["backend_id"] == previous_backend:
                    raise AssertionError("Backend did not restart")
                for receipt in receipts:
                    queried = await h.call("ansa_task_query", {"operation_id": receipt["operation_id"]})
                    if any(queried[key] != receipt[key] for key in ("task_id", "target", "model")) or queried["data"]["result"] != receipt["result"]:
                        raise AssertionError("Persisted geometry check receipt differs after restart")
                await common._bind_for_run(h, identity, one, two, identity["port"])
                if await oracle(h) != baseline or hashlib.sha256(saved.read_bytes()).hexdigest() != saved_hash:
                    raise AssertionError("Original receipt queries changed geometry or saved data")
                owned.record(report, path, "backend_restart_original_receipts_without_rechecking_or_model_changes", {"original_task_ids": [row["task_id"] for row in receipts], "unchanged": True})


if __name__ == "__main__":
    raise SystemExit(owned.main(report_name="geometry-checks-report.json",
        scope="NASTRAN explicit FACE Basic checks and boundary CONS reports; no fixes, global scope or canvas acceptance", verify_callback=verify))
