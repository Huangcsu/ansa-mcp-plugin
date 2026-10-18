"""独立 NASTRAN 壳/六面体的面积、体积和三方向质量验收。"""

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


FIXTURE_SCRIPT = """from ansa import base, constants
deck = base.CurrentDeck()
if deck != constants.NASTRAN:
    raise RuntimeError('metrics fixture requires NASTRAN')
for kind in ('SHELL', 'SOLID', 'FACE', '__PROPERTIES__', '__MATERIALS__'):
    if base.CollectEntities(deck, None, kind):
        raise RuntimeError('metrics fixture requires a fresh owned seed model')
def create(kind, fields):
    entity = base.CreateEntity(deck, kind, fields)
    if entity is None:
        raise RuntimeError('metrics fixture creation failed: '+kind)
    actual = base.GetEntityCardValues(deck, entity, tuple(fields))
    if any(actual.get(key) != value for key, value in fields.items()):
        raise RuntimeError('metrics fixture card mismatch: '+kind+' '+repr(actual))
    return entity
material = create('MAT1', {'DEFINED': 'YES', 'E': 200000., 'NU': .3, 'RHO': .01})
shell_property = create('PSHELL', {'MID1': material._id, 'MID2': material._id,
                                  'MID3': material._id, 'T': 2., 'NSM': 0.})
solid_property = create('PSOLID', {'MID': material._id})
elements = []
for kind, element_type, prop, points in (
    ('SHELL', 'CQUAD4', shell_property, ((0.,0.,0.), (20.,0.,0.), (20.,10.,0.), (0.,10.,0.))),
    ('SHELL', 'CTRIA3', shell_property, ((30.,0.,0.), (40.,0.,0.), (30.,10.,0.))),
    ('SOLID', 'CHEXA', solid_property, ((50.,0.,0.), (60.,0.,0.), (60.,5.,0.), (50.,5.,0.),
                                      (50.,0.,4.), (60.,0.,4.), (60.,5.,4.), (50.,5.,4.))),
):
    nodes = [create('GRID', dict(zip(('X1','X2','X3'), point))) for point in points]
    fields = {'type': element_type, 'PID': prop._id}
    fields.update({'G'+str(i+1): node._id for i, node in enumerate(nodes)})
    elements.append(create(kind, fields))
if base.Or(elements) != 1:
    raise RuntimeError('metrics fixture display setup failed')
base.RedrawAll()
base.ZoomAll()
RESULT = {'quad_id': elements[0]._id, 'triangle_id': elements[1]._id, 'hexa_id': elements[2]._id,
          'material_id': material._id, 'shell_property_id': shell_property._id,
          'solid_property_id': solid_property._id}
"""


def remote_metrics_readback(expected_pid, expected_arguments):
    import os
    import threading
    from ansa import base, constants, session
    if (os.getpid() != expected_pid or list(session.ProgramArguments()) != expected_arguments
            or threading.current_thread() is not threading.main_thread()):
        raise RuntimeError("Metrics oracle requires the owned ANSA main thread")
    deck = base.CurrentDeck()
    if deck != constants.NASTRAN:
        raise RuntimeError("Metrics oracle requires NASTRAN")

    def card(entity, fields):
        labels = entity.card_fields(deck)
        if any(field not in labels for field in fields):
            raise RuntimeError("Missing metrics fixture card fields: " + repr(labels))
        values = base.GetEntityCardValues(deck, entity, fields)
        if any(field not in values or values[field] is None for field in fields):
            raise RuntimeError("Incomplete metrics fixture card: " + repr(values))
        return values

    rows = {}
    for kind, fields in (
        ("GRID", ("X1", "X2", "X3")),
        ("MAT1", ("MID", "DEFINED", "E", "NU", "RHO")),
        ("PSHELL", ("PID", "MID1", "MID2", "MID3", "T", "NSM")),
        ("PSOLID", ("PID", "MID")),
    ):
        rows[kind] = [{"id": entity._id, "fields": card(entity, fields)}
            for entity in sorted(base.CollectEntities(deck, None, kind), key=lambda e: e._id)]
    for kind in ("SHELL", "SOLID"):
        rows[kind] = []
        for entity in sorted(base.CollectEntities(deck, None, kind), key=lambda e: e._id):
            labels = entity.card_fields(deck)
            fields = ("type", "EID", "PID") + tuple("G" + str(i) for i in range(1, 9)
                if "G" + str(i) in labels)
            values = card(entity, fields)
            rows[kind].append({"id": entity._id, "fields": values,
                "node_ids": sorted(node._id for node in base.CollectEntities(deck, entity, "GRID"))})
    identities = {kind: sorted([e.ansa_type(deck), e._id] for e in base.CollectEntities(deck, None, kind))
        for kind in ("FACE", "CONS", "CURVE", "__PROPERTIES__", "__MATERIALS__")}
    return {"deck": deck, "model_path": base.DataBaseName(), "cards": rows, "identities": identities,
        "visible_ids": {kind: sorted(e._id for e in base.CollectEntities(deck, None, kind, filter_visible=True))
            for kind in ("GRID", "SHELL", "SOLID")}}


def analytic_metrics(snapshot):
    """Calculate this fixture from node/section/material cards, without metric APIs."""
    cards = snapshot["cards"]
    nodes = {row["id"]: [row["fields"][key] for key in ("X1", "X2", "X3")] for row in cards["GRID"]}
    materials = {row["id"]: row["fields"] for row in cards["MAT1"]}
    properties = {row["id"]: row["fields"] for kind in ("PSHELL", "PSOLID") for row in cards[kind]}

    def subtract(a, b):
        return [x - y for x, y in zip(a, b)]

    def cross(a, b):
        return [a[1]*b[2] - a[2]*b[1], a[2]*b[0] - a[0]*b[2], a[0]*b[1] - a[1]*b[0]]

    def triangle(a, b, c):
        return math.hypot(*cross(subtract(b, a), subtract(c, a))) / 2.

    results = []
    for kind in ("SHELL", "SOLID"):
        for row in cards[kind]:
            fields = row["fields"]
            prop = properties[fields["PID"]]
            if kind == "SHELL":
                count = {"CTRIA3": 3, "CQUAD4": 4}[fields["type"]]
                points = [nodes[fields["G" + str(i)]] for i in range(1, count + 1)]
                measure = triangle(*points[:3])
                if count == 4:
                    measure += triangle(points[0], points[2], points[3])
                density = materials[prop["MID1"]]["RHO"]
                assert all(materials[prop[key]]["DEFINED"] == "YES" for key in ("MID1", "MID2", "MID3"))
                mass = measure * (prop["T"] * density + prop["NSM"])
                action = "area"
            else:
                assert fields["type"] == "CHEXA"
                points = [nodes[fields["G" + str(i)]] for i in range(1, 9)]
                u, v, w = (subtract(points[i], points[0]) for i in (1, 3, 4))
                for index, coefficients in ((2, (1,1,0)), (5, (1,0,1)), (6, (1,1,1)), (7, (0,1,1))):
                    close_numbers(points[index], [points[0][j] + sum(c*edge[j]
                        for c, edge in zip(coefficients, (u,v,w))) for j in range(3)])
                measure = abs(sum(a*b for a, b in zip(u, cross(v, w))))
                density = materials[prop["MID"]]["RHO"]
                assert materials[prop["MID"]]["DEFINED"] == "YES"
                mass = measure * density
                action = "volume"
            results.append({"entity_type": kind, "entity_id": row["id"],
                "action": action, "measure": measure, "mass": [mass, mass, mass]})
    return results


async def verify(run, instructions, identity, helpers, report, *, wait_ui):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    one, two, _original = helpers
    parameters = StdioServerParameters(command=sys.executable,
        args=["-B", "-m", "ansa_mcp_plugin", "--runtime-home", instructions["runtime_home"]],
        env={**os.environ, "PYTHONPATH": str(stage.PROJECT_ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"})

    def oracle():
        return two.rpc(identity["port"], remote_metrics_readback, identity["pid"], identity["program_args"])

    def record(name, evidence):
        report["cases"].append({"name": name, "ok": True, "evidence": evidence})
        stage._json_write(run / "entity-metrics-report.json", report)
        print(json.dumps({"case": name, "ok": True}), flush=True)

    def request(expected, action):
        return {"action": "entity_metrics", "deck": before["deck"],
            "options": {"action": action, "entity_type": expected["entity_type"], "entity_id": expected["entity_id"]}}

    def check(result, expected, action):
        assert result["action"] == action and result["entity"]["id"] == expected["entity_id"]
        close_numbers(result["value"], expected["mass"] if action == "mass" else expected["measure"])
        json.dumps(result, ensure_ascii=False, allow_nan=False)

    with (run / "entity-metrics-backend.stderr.log").open("w", encoding="utf-8") as log:
        async with stdio_client(parameters, errlog=log) as (reader, writer):
            async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=60)) as client:
                await client.initialize()
                h = stage.McpHarness(client, report, run)
                await stage._bind_for_run(h, identity, one, two, identity["port"])
                schemas = await h.list_schema()
                fixture = await h.direct_result("ansa_script_execute", {"script": FIXTURE_SCRIPT})
                await h.direct_result("ansa_model_save", {"path": str(run / "entity-metrics-before.ansa")})
                before = oracle()
                assert len(before["cards"]["GRID"]) == 16
                expected = analytic_metrics(before)
                close_numbers([row["measure"] for row in expected], [200., 50., 200.])
                close_numbers([row["mass"][0] for row in expected], [4., 1., 2.])
                assert before["visible_ids"]["SHELL"] == [row["id"] for row in before["cards"]["SHELL"]]
                assert before["visible_ids"]["SOLID"] == [row["id"] for row in before["cards"]["SOLID"]]
                record("schemas_material_section_connectivity_analytic_fixture_and_backup", {
                    "schemas": schemas, "fixture": fixture, "independent": before,
                    "analytic": expected, "backup": fingerprint(run / "entity-metrics-before.ansa")})

                measures = []
                for row in expected:
                    result = await h.direct_result("ansa_model_inspect", request(row, row["action"]))
                    check(result, row, row["action"])
                    assert oracle() == before
                    measures.append(result)
                record("shell_areas_and_hexa_volume_match_independent_coordinate_calculation", measures)

                masses = []
                original_target, original_model = h.target.copy(), h.model.copy()
                operation = "entity-metrics-mass-" + uuid.uuid4().hex
                for index, row in enumerate(expected):
                    submitted, completed = await h.task("ansa_model_inspect", request(row, "mass"),
                        operation_id=operation if index == 0 else None)
                    report.setdefault("mass_tasks", []).append({"submitted": submitted, "completed": completed})
                    stage._json_write(run / "entity-metrics-report.json", report)
                    result = completed["data"]["result"]
                    check(result, row, "mass")
                    assert oracle() == before
                    masses.append(result)
                    if index == 0:
                        first_task, first_completed = submitted, completed
                record("scoped_shell_and_solid_directional_masses_match_density_section_and_geometry", masses)

                invalids = []
                fields = request(expected[0], "area")
                for options in (
                    {**fields["options"], "entity_id": True},
                    {**fields["options"], "entity_id": 0},
                    {**fields["options"], "entity_id": 99999999},
                    {**fields["options"], "action": "repair"},
                    {**fields["options"], "entity_type": "GRID", "entity_id": identity["seed_entity_id"]},
                    {**fields["options"], "auto_fix": True},
                ):
                    invalids.append(await h.direct_result("ansa_model_inspect", {**fields, "options": options}, expected="failed"))
                invalids.append(await h.direct_result("ansa_model_inspect", {**fields,
                    "deck": before["deck"] + 1}, expected="failed"))
                assert oracle() == before
                record("invalid_ids_missing_entities_options_types_and_deck_reject_without_model_changes", invalids)

                repeated = await h.call("ansa_model_inspect", {"operation_id": operation,
                    "expected_target": original_target, "expected_model": original_model, **request(expected[0], "mass")})
                assert repeated["task_id"] == first_task["task_id"]
                assert repeated["data"]["result"] == first_completed["data"]["result"]
                assert oracle() == before
                record("original_mass_operation_idempotency_and_unchanged_cards_and_visibility", {
                    "task_id": first_task["task_id"], "repeated_task_id": repeated["task_id"], "result": repeated["data"]["result"]})

                saved_path = run / "entity-metrics-after.ansa"
                await h.direct_result("ansa_model_save", {"path": str(saved_path)})
                saved = fingerprint(saved_path)
                await h.direct_result("ansa_model_open", {"path": str(saved_path),
                    "backup_path": str(run / "entity-metrics-reopen-backup.ansa")})
                await h.snapshot_context()
                reopened = oracle()
                assert {k:v for k,v in reopened.items() if k != "model_path"} == {
                    k:v for k,v in before.items() if k != "model_path"}
                assert analytic_metrics(reopened) == expected
                for row in expected:
                    for action in (row["action"], "mass"):
                        check(await h.direct_result("ansa_model_inspect", request(row, action)), row, action)
                assert oracle() == reopened and fingerprint(saved_path) == saved
                assert h.model["epoch"] != original_model["epoch"]
                stale = await h.call("ansa_model_inspect", {"operation_id": "entity-metrics-stale-" + uuid.uuid4().hex,
                    "expected_target": h.target, "expected_model": original_model, **fields}, allow_error=True)
                assert stale["ok"] is False and stale["task_id"] is None
                record("saved_model_reopen_preserves_metrics_and_rejects_old_context", {
                    "independent": reopened, "analytic": expected, "saved": saved, "stale": stale})
                report["core_acceptance"] = True
                stage._json_write(run / "entity-metrics-report.json", report)

                if wait_ui:
                    marker = run / "entity-metrics-ui-confirmation.json"
                    if marker.exists():
                        raise RuntimeError("Refuse stale metrics GUI evidence")
                    stage._json_write(run / "entity-metrics-ui-instruction.json", {"identity": identity,
                        "model_path": reopened["model_path"], "visible_ids": reopened["visible_ids"],
                        "instruction": "所属主窗口显示一个四边形、一个三角形和一个六面体，SHELL 2、SOLID 1。"})
                    print(json.dumps({"status": "waiting_for_entity_metrics_ui", "run_dir": str(run)}), flush=True)
                    deadline = asyncio.get_running_loop().time() + stage.PANEL_WAIT_SECONDS
                    while not marker.is_file():
                        if asyncio.get_running_loop().time() >= deadline:
                            raise TimeoutError("Keep original metrics tasks and owned process for GUI inspection")
                        await asyncio.sleep(.5)
                    visual = json.loads(marker.read_text(encoding="utf-8"))
                    if (visual.get("checked") is not True or visual.get("pid") != identity["pid"]
                            or visual.get("process_start") != identity["process_start"]
                            or visual.get("model_path") != reopened["model_path"]
                            or visual.get("three_elements_visible") is not True):
                        raise RuntimeError("Metrics GUI evidence mismatches the owned model")
                    assert oracle() == reopened
                    report["visual_acceptance"] = True
                    record("owned_main_window_three_elements_and_post_ui_independent_readback", visual)
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
    if (run / "seed-baseline.ansa").exists() or (run / "entity-metrics-report.json").exists():
        raise ValueError("只接受新准备目录；未知结果先查询原任务，不重放夹具")
    report = {"scope": "ANSA 2026.1 Windows GUI/NASTRAN CQUAD4、CTRIA3、CHEXA8 只读度量",
        "started_at": datetime.now(timezone.utc).isoformat(), "success": False, "cases": [],
        "core_acceptance": False, "visual_acceptance": False}
    identity = helpers = None
    try:
        identity, _ansa, _grpc, helpers = stage._prepare_owned_ansa(run, instructions)
        report["identity"] = identity
        stage._json_write(run / "entity-metrics-report.json", report)
        report["fixture"] = helpers[1].rpc(identity["port"], remote_initialize_fixture,
            identity["pid"], identity["program_args"], identity["seed_path"], identity["seed_entity_id"])
        asyncio.run(verify(run, instructions, identity, helpers, report, wait_ui=args.wait_ui))
    except BaseException as exc:
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
        report["exception_tree"] = stage._exception_tree(exc)
        (run / "entity-metrics-report.error.log").write_text(traceback.format_exc(), encoding="utf-8")
        raise
    finally:
        if identity is not None and helpers is not None and report["success"] and not args.keep_ansa:
            try:
                report["cleanup"] = stage._cleanup_owned_ansa(identity, instructions, helpers)
            except Exception as exc:
                report["success"] = False
                report["cleanup_error"] = {"type": type(exc).__name__, "message": str(exc)}
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        stage._json_write(run / "entity-metrics-report.json", report)
    if not report["success"]:
        raise RuntimeError("Metrics acceptance or owned process cleanup failed")
    print(json.dumps({"success": True, "report": str(run / "entity-metrics-report.json")}), flush=True)


if __name__ == "__main__":
    main()
