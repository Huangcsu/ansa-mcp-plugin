"""在独占 ANSA GUI 验证 FACE 排序、只读预览及面积/中心回读。"""

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


def remote_geometry_readback(expected_pid, expected_arguments):
    import os
    import threading
    from ansa import base, session
    if (os.getpid() != expected_pid or list(session.ProgramArguments()) != expected_arguments
            or threading.current_thread() is not threading.main_thread()):
        raise RuntimeError("Geometry oracle requires the owned ANSA main thread")
    deck = base.CurrentDeck()
    faces = sorted(base.CollectEntities(deck, None, "FACE"), key=lambda face: face._id)
    return {"pid": os.getpid(), "deck": deck, "main_thread": True,
            "model_path": base.DataBaseName(),
            "faces": [{"id": face._id, "area": base.GetFaceArea(face),
                       "center": list(base.Cog(face)), "bounds": base.BoundBox(face),
                       "usable": face.is_usable()} for face in faces],
            "visible_face_ids": sorted(face._id for face in base.CollectEntities(
                deck, None, "FACE", filter_visible=True)),
            "counts": {kind: len(base.CollectEntities(deck, None, kind))
                       for kind in ("FACE", "CONS", "CURVE", "GRID", "SHELL")}}


def close_numbers(actual, expected):
    if isinstance(expected, (tuple, list)):
        assert len(actual) == len(expected)
        for left, right in zip(actual, expected):
            close_numbers(left, right)
    else:
        assert math.isclose(actual, expected, rel_tol=1e-8, abs_tol=1e-8), (actual, expected)


def preview_content(result):
    return {key: result[key] for key in (
        "deck", "query", "point", "source_count", "matched_count", "limit", "read_only",
        "selection_changed", "candidates", "face_ids", "scope", "preview_only",
        "would_delete_face_ids", "requires_confirmation", "total_area", "bounds")}


async def verify(run, instructions, identity, helpers, report, *, wait_ui):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    one, two, _original = helpers
    parameters = StdioServerParameters(command=sys.executable,
        args=["-B", "-m", "ansa_mcp_plugin", "--runtime-home", instructions["runtime_home"]],
        env={**os.environ, "PYTHONPATH": str(stage.PROJECT_ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"})

    def record(name, evidence):
        report["cases"].append({"name": name, "ok": True, "evidence": evidence})
        stage._json_write(run / "geometry-report.json", report)
        print(json.dumps({"case": name, "ok": True}), flush=True)

    def oracle():
        return two.rpc(identity["port"], remote_geometry_readback, identity["pid"], identity["program_args"])

    with (run / "geometry-backend.stderr.log").open("w", encoding="utf-8") as log:
        async with stdio_client(parameters, errlog=log) as (reader, writer):
            async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=30)) as client:
                await client.initialize()
                h = stage.McpHarness(client, report, run)
                await stage._bind_for_run(h, identity, one, two, identity["port"])
                schema = await h.list_schema()
                empty = oracle()
                assert empty["faces"] == []
                empty_preview = await h.direct_result("ansa_geometry_preview", {
                    "deck": empty["deck"], "query": "largest_area"})
                assert empty_preview["would_delete_face_ids"] == [] and empty_preview["bounds"] is None
                assert empty_preview["total_area"] == 0 and oracle() == empty
                record("schemas_and_empty_model_read_only_preview", {"schemas": schema, "empty_preview": empty_preview})

                # This script constructs only our fixture; it is not the implementation of either new tool.
                await h.direct_result("ansa_script_execute", {"script": (
                    "from ansa import base\n"
                    "specs = [(0.,20.,10.,0.), (30.,10.,10.,10.), (50.,10.,5.,5.)]\n"
                    "created = []\n"
                    "for x, width, height, z in specs:\n"
                    "    points = [[x,0.,z],[x+width,0.,z],[x+width,height,z],[x,height,z]]\n"
                    "    curves = [base.CurvesNew([points[i],points[(i+1)%4]]) for i in range(4)]\n"
                    "    if any(c is None for c in curves): raise RuntimeError('fixture curve failed')\n"
                    "    faces = base.FacesNewPlanar(curves, ret_ents=True)\n"
                    "    if not faces or len(faces) != 1: raise RuntimeError('fixture planar face failed')\n"
                    "    created.append(faces[0]._id)\n"
                    "RESULT = {'fixture_face_ids': created}\n")})
                before = oracle()
                assert len(before["faces"]) == 3 and before["counts"]["GRID"] == 1
                by_x = sorted(before["faces"], key=lambda row: row["center"][0])
                for face, area, center in zip(by_x, (200., 100., 50.),
                                            ([10.,5.,0.], [35.,5.,10.], [55.,2.5,5.])):
                    close_numbers(face["area"], area)
                    close_numbers(face["center"], center)
                assert before["visible_face_ids"] == sorted(face["id"] for face in by_x)
                report["fixture_oracle"] = before
                record("owned_three_rectangles_independent_area_centroid_and_visibility", before)

                ranking = []
                for query, desired in (("largest_area", by_x[0]), ("smallest_area", by_x[2]),
                                       ("highest_z", by_x[1]), ("lowest_z", by_x[0]),
                                       ("nearest_center", by_x[2])):
                    fields = {"deck": before["deck"], "query": query, "limit": 1}
                    if query == "nearest_center":
                        fields["point"] = [56.,2.5,5.]
                    result = await h.direct_result("ansa_geometry_select", fields)
                    assert result["face_ids"] == [desired["id"]] and result["source_count"] == 3
                    assert result["scope"] == "all" and result["read_only"] and not result["selection_changed"]
                    close_numbers(result["candidates"][0]["area"], desired["area"])
                    close_numbers(result["candidates"][0]["center"], desired["center"])
                    if query == "nearest_center":
                        close_numbers(result["candidates"][0]["score"], 1.)
                    ranking.append(result)
                explicit = await h.direct_result("ansa_geometry_select", {
                    "deck": before["deck"], "query": "largest_area",
                    "face_ids": [by_x[2]["id"], by_x[1]["id"]]})
                assert explicit["face_ids"] == [by_x[1]["id"], by_x[2]["id"]]
                assert explicit["scope"] == "explicit" and explicit["source_count"] == 2
                assert oracle() == before
                record("five_rankings_and_explicit_candidate_scope_without_model_or_display_changes", {
                    "rankings": ranking, "explicit": explicit})

                fields = {"deck": before["deck"], "query": "largest_area", "limit": 2}
                original_target, original_model = h.target.copy(), h.model.copy()
                operation_id = "geometry-preview-" + uuid.uuid4().hex
                submitted, completed = await h.task("ansa_geometry_preview", fields, operation_id=operation_id)
                preview = completed["data"]["result"]
                assert preview["face_ids"] == preview["would_delete_face_ids"] == [by_x[0]["id"], by_x[1]["id"]]
                assert preview["preview_only"] and preview["requires_confirmation"]
                close_numbers(preview["total_area"], 300.)
                close_numbers(preview["bounds"], [0.,0.,0.,40.,10.,10.])
                repeated = await h.call("ansa_geometry_preview", {
                    "operation_id": operation_id, "expected_target": original_target,
                    "expected_model": original_model, **fields})
                assert repeated["task_id"] == submitted["task_id"] and repeated["data"]["result"] == preview
                assert oracle() == before
                record("deletion_preview_bounds_area_and_original_operation_idempotency", {
                    "preview": preview, "task_id": submitted["task_id"], "repeated_task_id": repeated["task_id"]})

                rejected = []
                for invalid in ({"query": "nearest_center"},
                                {"query": "largest_area", "point": [0.,0.,0.]},
                                {"query": "largest_area", "face_ids": [by_x[0]["id"], 99999999]},
                                {"query": "largest_area", "face_ids": [by_x[0]["id"], by_x[0]["id"]]}):
                    rejected.append(await h.direct_result("ansa_geometry_select", {
                        "deck": before["deck"], **invalid}, expected="failed"))
                for tool in ("ansa_geometry_select", "ansa_geometry_preview"):
                    for invalid in ({"face_ids": []}, {"limit": True}, {"query": "normal_positive_z"},
                                    {"deck": True}, {"deck": str(before["deck"])}):
                        response = await client.call_tool(tool, {
                            "operation_id": "geometry-invalid-" + uuid.uuid4().hex,
                            "expected_target": h.target, "expected_model": h.model,
                            "deck": before["deck"], "query": "largest_area", **invalid})
                        assert response.isError
                assert oracle() == before and (await h.snapshot_context())["model"]["epoch"] == original_model["epoch"]
                record("invalid_point_missing_or_duplicate_face_and_schema_inputs_rejected_without_changes", rejected)

                properties = await h.direct_result("ansa_model_inspect", {
                    "action": "face_properties", "deck": before["deck"],
                    "options": {"face_ids": [face["id"] for face in by_x]}})
                by_id = {face["id"]: face for face in by_x}
                for row in properties["faces"]:
                    assert row["found"] is True
                    expected = by_id[row["face"]["id"]]
                    close_numbers(row["area"], expected["area"])
                    close_numbers(row["center"], expected["center"])
                assert oracle() == before
                record("existing_face_properties_agree_with_independent_geometry", properties)

                saved_path = run / "geometry-saved-copy.ansa"
                await h.direct_result("ansa_model_save", {"path": str(saved_path)})
                file_before = fingerprint(saved_path)
                await h.direct_result("ansa_model_open", {
                    "path": str(saved_path), "backup_path": str(run / "before-geometry-reopen.ansa")})
                await h.snapshot_context()
                reopened = oracle()
                assert {key: value for key, value in reopened.items() if key != "model_path"} == {
                    key: value for key, value in before.items() if key != "model_path"}
                again = await h.direct_result("ansa_geometry_preview", fields)
                reopened_file = fingerprint(saved_path)
                report["reopen_evidence"] = {"before": preview, "after": again,
                                              "file_before": file_before, "file_after": reopened_file}
                stage._json_write(run / "geometry-report.json", report)
                assert preview_content(again) == preview_content(preview) and reopened_file == file_before
                assert again["model_after"]["epoch"] == h.model["epoch"] != original_model["epoch"]
                stale = await h.call("ansa_geometry_select", {
                    "operation_id": "geometry-stale-" + uuid.uuid4().hex,
                    "expected_target": h.target, "expected_model": original_model,
                    "deck": reopened["deck"], "query": "largest_area"}, allow_error=True)
                assert stale["ok"] is False and stale["task_id"] is None
                record("save_reopen_preserves_geometry_preview_and_rejects_stale_model_context", {
                    "independent": reopened, "preview": again, "stale_error": stale["error"], "file": file_before})

                if wait_ui:
                    marker = run / "geometry-ui-confirmation.json"
                    if marker.exists():
                        raise RuntimeError("Refuse stale geometry GUI evidence")
                    stage._json_write(run / "geometry-ui-instruction.json", {
                        "identity": identity, "model_path": reopened["model_path"],
                        "expected_face_count": 3, "expected_visible_face_ids": reopened["visible_face_ids"],
                        "instruction": "在所属 ANSA 主窗口确认三个矩形 FACE 仍全部可见，预览未删除或隔离候选。"})
                    print(json.dumps({"status": "waiting_for_geometry_ui", "run_dir": str(run)}), flush=True)
                    deadline = asyncio.get_running_loop().time() + stage.PANEL_WAIT_SECONDS
                    while not marker.is_file():
                        if asyncio.get_running_loop().time() >= deadline:
                            raise TimeoutError("Geometry GUI timed out; retain the original tasks and owned instance")
                        await asyncio.sleep(.5)
                    visual = json.loads(marker.read_text(encoding="utf-8"))
                    if (visual.get("checked") is not True or visual.get("pid") != identity["pid"]
                            or visual.get("process_start") != identity["process_start"]
                            or visual.get("model_path") != reopened["model_path"]
                            or visual.get("all_three_faces_visible") is not True):
                        raise RuntimeError("Geometry GUI evidence does not match the owned process and model")
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
    if (run / "seed-baseline.ansa").exists() or (run / "geometry-report.json").exists():
        raise ValueError("仅接受新准备目录；未知结果应查询原任务，禁止重放夹具")
    report = {"scope": "ANSA 2026.1 GUI/NASTRAN 三矩形 FACE 只读排序、预览及属性；MCP stdio → 自研 TCP → 所属主线程",
              "started_at": datetime.now(timezone.utc).isoformat(), "success": False, "cases": [],
              "visual_acceptance": False, "normal_query_acceptance": False, "face_delete_acceptance": False}
    identity = helpers = None
    try:
        identity, _ansa, _grpc, helpers = stage._prepare_owned_ansa(run, instructions)
        report["identity"] = identity
        stage._json_write(run / "geometry-report.json", report)
        report["fixture"] = helpers[1].rpc(identity["port"], remote_initialize_fixture,
            identity["pid"], identity["program_args"], identity["seed_path"], identity["seed_entity_id"])
        asyncio.run(verify(run, instructions, identity, helpers, report, wait_ui=args.wait_ui))
    except BaseException as exc:
        report["success"] = False
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
        report["exception_tree"] = stage._exception_tree(exc)
        (run / "geometry-report.error.log").write_text(traceback.format_exc(), encoding="utf-8")
        raise
    finally:
        if identity is not None and helpers is not None and report["success"] and not args.keep_ansa:
            try:
                report["cleanup"] = stage._cleanup_owned_ansa(identity, instructions, helpers)
            except Exception as exc:
                report["cleanup"] = {"error": str(exc), "stopped_owned_process": False}
                report["success"] = False
        stage._json_write(run / "geometry-report.json", report)


if __name__ == "__main__":
    main()
