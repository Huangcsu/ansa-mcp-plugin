"""在独占 ANSA 主窗口验收实体显示与 Near/Neighb，不连接用户模型。"""

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
from stage_three_cad_ansa import fingerprint, remote_initialize_fixture


def remote_visibility_readback(expected_pid, expected_arguments):
    import os
    import threading
    from ansa import base, session
    if (os.getpid() != expected_pid or list(session.ProgramArguments()) != expected_arguments
            or threading.current_thread() is not threading.main_thread()):
        raise RuntimeError("Visibility oracle requires the owned ANSA main thread")
    deck = base.CurrentDeck()
    kinds = ("FACE", "SHELL")
    entities = {kind: sorted(base.CollectEntities(deck, None, kind), key=lambda e: e._id) for kind in kinds}
    visible = {kind: sorted(e._id for e in base.CollectEntities(deck, None, kind, filter_visible=True)) for kind in kinds}
    shells = [{"id": e._id, "fields": base.GetEntityCardValues(deck, e, ("type", "EID", "PID", "G1", "G2", "G3", "G4")),
               "node_ids": sorted(n._id for n in base.CollectEntities(deck, e, "GRID")),
               "is_visible": base.IsEntityVisible(e)} for e in entities["SHELL"]]
    faces = [{"id": e._id, "area": base.GetFaceArea(e), "center": list(base.Cog(e)),
              "bounds": list(base.BoundBox([e]))} for e in entities["FACE"]]
    nodes = [{"id": e._id, "fields": base.GetEntityCardValues(deck, e, ("X1", "X2", "X3"))}
             for e in sorted(base.CollectEntities(deck, None, "GRID"), key=lambda e: e._id)]
    identities = {kind: sorted([e.ansa_type(deck), e._id] for e in base.CollectEntities(deck, None, kind))
                  for kind in ("CONS", "CURVE", "__PROPERTIES__", "__MATERIALS__")}
    return {"deck": deck, "model_path": base.DataBaseName(), "visible_ids": visible,
            "ids": {kind: [e._id for e in entities[kind]] for kind in kinds},
            "shells": shells, "faces": faces, "nodes": nodes, "identities": identities}


FIXTURE_SCRIPT = """from ansa import base
deck = base.CurrentDeck()
nodes = []
for x in (0., 10., 20., 30., 100., 110.):
    for y in (0., 10.):
        node = base.CreateEntity(deck, 'GRID', {'X1': x, 'X2': y, 'X3': 0.})
        if node is None: raise RuntimeError('fixture GRID creation failed')
        nodes.append(node)
prop = base.CreateEntity(deck, 'PSHELL', {})
if prop is None: raise RuntimeError('fixture property failed')
shells = []
for indices in ((0,2,3,1), (2,4,5,3), (4,6,7,5), (8,10,11,9)):
    fields = {'type': 'CQUAD4', 'PID': prop._id}
    fields.update({'G'+str(i+1): nodes[index]._id for i,index in enumerate(indices)})
    shell = base.CreateEntity(deck, 'SHELL', fields)
    if shell is None: raise RuntimeError('fixture SHELL creation failed')
    actual = base.GetEntityCardValues(deck, shell, ('type','PID','G1','G2','G3','G4'))
    if any(actual.get(key) != value for key,value in fields.items()):
        raise RuntimeError('fixture SHELL differs from requested connectivity: '+repr(actual))
    shells.append(shell)
faces = []
for x in (0., 50.):
    points = [[x,30.,0.],[x+5.,30.,0.],[x+5.,35.,0.],[x,35.,0.]]
    curves = [base.CurvesNew([points[i],points[(i+1)%4]]) for i in range(4)]
    created = base.FacesNewPlanar(curves, ret_ents=True)
    if not created or len(created) != 1: raise RuntimeError('fixture FACE failed')
    faces.append(created[0])
if base.Or(shells+faces) != 1: raise RuntimeError('fixture visible setup failed')
RESULT = {'shell_ids': [e._id for e in shells], 'face_ids': [e._id for e in faces]}
"""


def content(value):
    return {key: ([{k: v for k, v in row.items() if k != "is_visible"} for row in data]
                  if key == "shells" else data)
            for key, data in value.items() if key not in {"visible_ids", "model_path"}}


def connected_steps(shells, seeds, steps):
    nodes = {row["id"]: set(row["node_ids"]) for row in shells}
    found = set(seeds)
    for _ in range(steps):
        frontier_nodes = set().union(*(nodes[entity_id] for entity_id in found))
        found.update(entity_id for entity_id, items in nodes.items() if items & frontier_nodes)
    return sorted(found)


async def wait_for_gui(run, identity, expected, oracle, *, followup=False):
    suffix = "ui-followup" if followup else "ui"
    marker = run / f"visibility-{suffix}-confirmation.json"
    if marker.exists():
        raise RuntimeError("Refuse stale GUI evidence")
    stage._json_write(run / f"visibility-{suffix}-instruction.json", {"identity": identity,
        "model_path": expected["model_path"], "visible_ids": expected["visible_ids"],
        "instruction": "所属主窗口应显示相连两个四边形 SHELL，另外两个 SHELL 和两个 FACE 隐藏。"})
    print(json.dumps({"status": "waiting_for_visibility_ui", "run_dir": str(run), "marker": str(marker)}), flush=True)
    deadline = asyncio.get_running_loop().time() + stage.PANEL_WAIT_SECONDS
    while not marker.is_file():
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError("Keep original tasks and owned process for GUI followup")
        await asyncio.sleep(.5)
    visual = json.loads(marker.read_text(encoding="utf-8"))
    if (visual.get("checked") is not True or visual.get("pid") != identity["pid"]
            or visual.get("process_start") != identity["process_start"]
            or visual.get("model_path") != expected["model_path"]
            or visual.get("two_connected_shells_visible") is not True):
        raise RuntimeError("GUI evidence does not match the owned model")
    assert oracle() == expected
    return visual


async def verify_gui_only(run, instructions, identity, helpers, original, report):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    one, two, _original = helpers
    parameters = StdioServerParameters(command=sys.executable,
        args=["-B", "-m", "ansa_mcp_plugin", "--runtime-home", instructions["runtime_home"]],
        env={**os.environ, "PYTHONPATH": str(stage.PROJECT_ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"})

    def oracle():
        return two.rpc(identity["port"], remote_visibility_readback, identity["pid"], identity["program_args"])

    expected = original["cases"][6]["evidence"]["independent"]
    with (run / "visibility-ui-followup-backend.stderr.log").open("w", encoding="utf-8") as log:
        async with stdio_client(parameters, errlog=log) as (reader, writer):
            async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=30)) as client:
                await client.initialize()
                h = stage.McpHarness(client, report, run)
                await stage._bind_for_run(h, identity, one, two, identity["port"])
                assert oracle() == expected
                prior = original["cases"][5]["evidence"][0]
                task = await h.call("ansa_task_query", {"task_id": prior["original_task_id"]})
                assert task["status"] == "succeeded"
                assert task["data"]["result"] == {key: value for key, value in prior.items()
                    if key not in {"original_task_id", "repeated_same_operation"}}
                visual = await wait_for_gui(run, identity, expected, oracle, followup=True)
                report["cases"].append({"name": "original_task_query_and_owned_main_window_without_replaying_display",
                    "ok": True, "evidence": {"original_task_id": prior["original_task_id"], "visual": visual}})
                report["visual_acceptance"] = True
    report["success"] = True


async def verify(run, instructions, identity, helpers, report, *, wait_ui):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    one, two, _original = helpers
    parameters = StdioServerParameters(command=sys.executable,
        args=["-B", "-m", "ansa_mcp_plugin", "--runtime-home", instructions["runtime_home"]],
        env={**os.environ, "PYTHONPATH": str(stage.PROJECT_ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"})

    def oracle():
        return two.rpc(identity["port"], remote_visibility_readback, identity["pid"], identity["program_args"])

    def record(name, evidence):
        report["cases"].append({"name": name, "ok": True, "evidence": evidence})
        stage._json_write(run / "visibility-report.json", report)
        print(json.dumps({"case": name, "ok": True}), flush=True)

    with (run / "visibility-backend.stderr.log").open("w", encoding="utf-8") as log:
        async with stdio_client(parameters, errlog=log) as (reader, writer):
            async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=30)) as client:
                await client.initialize()
                h = stage.McpHarness(client, report, run)
                await stage._bind_for_run(h, identity, one, two, identity["port"])
                schema = await h.list_schema()
                await h.direct_result("ansa_script_execute", {"script": FIXTURE_SCRIPT})
                before = oracle()
                report["fixture_readback"] = before
                stage._json_write(run / "visibility-report.json", report)
                assert len(before["shells"]) == 4 and len(before["faces"]) == 2 and len(before["nodes"]) == 13
                assert len(connected_steps(before["shells"], before["ids"]["SHELL"][:1], 1)) == 2
                await h.direct_result("ansa_model_save", {"path": str(run / "visibility-before.ansa")})
                before = oracle()
                deck = before["deck"]

                async def snapshot():
                    result = await h.direct_result("ansa_model_inspect", {"action": "visibility_snapshot", "deck": deck})
                    current = oracle()
                    assert result["visible_ids"] == current["visible_ids"] and result["ids"] == current["ids"]
                    assert content(current) == content(before)
                    assert [s["id"] for s in current["shells"] if s["is_visible"]] == current["visible_ids"]["SHELL"]
                    return result, current

                initial, independent = await snapshot()
                assert independent == before and initial["read_only"] is True
                record("schemas_fixture_backup_and_readonly_visibility_snapshot", {"schemas": schema,
                    "independent": before, "snapshot": initial, "backup": fingerprint(run / "visibility-before.ansa")})

                async def display(action, *, repeat=False, **fields):
                    current, _ = await snapshot()
                    arguments = {"action": "entity_visibility", "deck": deck,
                        "options": {"action": action, "expected_visible": current["visible_ids"], **fields}}
                    op = "visibility-" + uuid.uuid4().hex
                    submitted, completed = await h.task("ansa_model_inspect", arguments, operation_id=op)
                    result = completed["data"]["result"]
                    if repeat:
                        repeated, again = await h.task("ansa_model_inspect", arguments, operation_id=op)
                        assert submitted["task_id"] == repeated["task_id"] and completed == again
                    after, checked = await snapshot()
                    assert result["applied"] is True and result["readback_complete"] is True
                    assert result["after"]["visible_ids"] == after["visible_ids"]
                    if repeat:
                        result = {**result, "original_task_id": submitted["task_id"], "repeated_same_operation": True}
                    return result, checked

                face_ids, shell_ids = before["ids"]["FACE"], before["ids"]["SHELL"]
                face_results = []
                for action, ids, expected in (("show_only", face_ids[:1], face_ids[:1]),
                        ("show_also", face_ids[1:], face_ids), ("hide", face_ids[:1], face_ids[1:])):
                    result, current = await display(action, entity_type="FACE", entity_ids=ids)
                    assert current["visible_ids"]["FACE"] == expected
                    face_results.append(result)
                record("explicit_face_isolation_addition_and_hiding_preserve_model", face_results)

                shell_results = []
                for action, ids, expected in (("show_only", shell_ids[:1], shell_ids[:1]),
                        ("show_also", shell_ids[1:2], shell_ids[:2]), ("hide", shell_ids[:1], shell_ids[1:2])):
                    result, current = await display(action, entity_type="SHELL", entity_ids=ids)
                    assert current["visible_ids"]["SHELL"] == expected
                    shell_results.append(result)
                record("explicit_shell_isolation_addition_and_hiding_preserve_connectivity", shell_results)

                frozen, checked_before = await snapshot()
                failures = []
                stale = {kind: list(ids) for kind, ids in frozen["visible_ids"].items()}
                stale["SHELL"] = []
                for options in ({"action": "show_only", "entity_type": "SHELL", "entity_ids": shell_ids[:1], "expected_visible": stale},
                        {"action": "show_only", "entity_type": "SHELL", "entity_ids": [shell_ids[0], 99999999], "expected_visible": frozen["visible_ids"]},
                        {"action": "near", "radius": True, "expected_visible": frozen["visible_ids"]}):
                    failed = await h.direct_result("ansa_model_inspect", {"action": "entity_visibility", "deck": deck,
                        "options": options}, expected="failed")
                    assert oracle() == checked_before
                    failures.append(failed)
                record("stale_visibility_missing_last_entity_and_invalid_radius_reject_without_effects", failures)

                await display("show_only", entity_type="SHELL", entity_ids=shell_ids[:1])
                near, current = await display("near", radius=.1, dense_search=True)
                assert near["return_code"] == 0 and current["visible_ids"]["SHELL"] == shell_ids[:2]
                assert near["added_visible_ids"]["SHELL"] == shell_ids[1:2]
                record("near_uses_current_visible_seed_and_model_length_units_with_independent_readback", near)

                neighbors = []
                for steps in (1, 2):
                    await display("show_only", entity_type="SHELL", entity_ids=shell_ids[:1])
                    result, current = await display("neighb", steps=steps, repeat=steps == 1)
                    assert result["return_code"] == 0
                    assert current["visible_ids"]["SHELL"] == connected_steps(before["shells"], shell_ids[:1], steps)
                    neighbors.append(result)
                record("neighb_connectivity_steps_original_operation_reuse_and_remote_shell_preservation", neighbors)

                await display("show_only", entity_type="SHELL", entity_ids=shell_ids[:2])
                saved = run / "visibility-after.ansa"
                await h.direct_result("ansa_model_save", {"path": str(saved)})
                old_model = dict(h.model)
                await h.direct_result("ansa_model_open", {"path": str(saved), "backup_path": str(run / "visibility-reopen-backup.ansa")})
                await h.snapshot_context()
                rejected = await h.call("ansa_model_inspect", {"operation_id": "visibility-stale-"+uuid.uuid4().hex,
                    "expected_target": h.target, "expected_model": old_model, "action": "entity_visibility", "deck": deck,
                    "options": {"action": "show_only", "entity_type": "SHELL", "entity_ids": shell_ids[:1],
                                "expected_visible": frozen["visible_ids"]}}, allow_error=True)
                assert rejected["ok"] is False and not rejected.get("task_id")
                _result, reopened = await display("show_only", entity_type="SHELL", entity_ids=shell_ids[:2])
                assert reopened["visible_ids"] == {"FACE": [], "SHELL": shell_ids[:2]}
                record("saved_model_reopen_preserves_geometry_and_rejects_old_model_context", {
                    "independent": reopened, "saved": fingerprint(saved), "stale_error": rejected["error"]})
                report["core_acceptance"] = True
                stage._json_write(run / "visibility-report.json", report)

                if wait_ui:
                    visual = await wait_for_gui(run, identity, reopened, oracle)
                    report["visual_acceptance"] = True
                    record("owned_main_window_two_connected_shells_and_post_ui_independent_readback", visual)
    report["success"] = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ansa-root", required=True, type=Path)
    parser.add_argument("--runtime-home", required=True, type=Path)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--wait-ui", action="store_true")
    parser.add_argument("--keep-ansa", action="store_true")
    parser.add_argument("--gui-only", action="store_true", help="原入口结束后查询原任务，仅补 GUI，不重放显示")
    args = parser.parse_args()
    run, instructions = stage._load_run(args)
    report_name = "visibility-ui-followup-report.json" if args.gui_only else "visibility-report.json"
    if not args.gui_only and ((run / "seed-baseline.ansa").exists() or (run / report_name).exists()):
        raise ValueError("只接受新准备目录；结果未知时先查询原任务，禁止重放夹具")
    if args.gui_only and (run / report_name).exists():
        raise ValueError("已有 GUI 补验记录；先检查原结果，禁止覆盖")
    report = {"scope": "ANSA 2026.1 GUI/NASTRAN FACE/SHELL 显示、Near/Neighb；监测 FACE/SHELL，其他类型可联动",
        "started_at": datetime.now(timezone.utc).isoformat(), "success": False, "cases": [], "visual_acceptance": False}
    identity = helpers = None
    try:
        if args.gui_only:
            from ansa_mcp_shared.identity import same_process_alive
            import grpc
            original = json.loads((run / "visibility-report.json").read_text(encoding="utf-8"))
            if (len(original["cases"]) != 7 or not all(case["ok"] is True for case in original["cases"])
                    or original["visual_acceptance"] is not False):
                raise ValueError("GUI 补验仅接受 7 项已通过且尚未视觉验收的原记录")
            identity = original["identity"]
            if not same_process_alive(identity["pid"], identity["process_start"]):
                raise RuntimeError("原独占 ANSA 进程已结束；不启动或重放")
            one, two = stage._import_runtime_helpers()
            helpers = (one, two, stage._bounded_grpc(grpc, one.DeadlineInterceptor))
            basic = two.rpc(identity["port"], one.remote_basic)
            stage._verify_program_arguments(basic, Path(instructions["translator"]), int(instructions["port"]))
            if basic["pid"] != identity["pid"] or basic["program_args"] != identity["program_args"]:
                raise RuntimeError("原进程身份已改变")
            report["identity"] = identity
            report["original_report"] = str(run / "visibility-report.json")
            asyncio.run(verify_gui_only(run, instructions, identity, helpers, original, report))
        else:
            identity, _ansa, _grpc, helpers = stage._prepare_owned_ansa(run, instructions)
            report["identity"] = identity
            stage._json_write(run / report_name, report)
            report["fixture"] = helpers[1].rpc(identity["port"], remote_initialize_fixture,
                identity["pid"], identity["program_args"], identity["seed_path"], identity["seed_entity_id"])
            asyncio.run(verify(run, instructions, identity, helpers, report, wait_ui=args.wait_ui))
    except BaseException as exc:
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
        report["exception_tree"] = stage._exception_tree(exc)
        (run / report_name.replace(".json", ".error.log")).write_text(traceback.format_exc(), encoding="utf-8")
        raise
    finally:
        if identity is not None and helpers is not None and report["success"] and not args.keep_ansa:
            try:
                report["cleanup"] = stage._cleanup_owned_ansa(identity, instructions, helpers)
            except Exception as exc:
                report["cleanup"] = {"error": str(exc), "stopped_owned_process": False}
                report["success"] = False
        stage._json_write(run / report_name, report)


if __name__ == "__main__":
    main()
