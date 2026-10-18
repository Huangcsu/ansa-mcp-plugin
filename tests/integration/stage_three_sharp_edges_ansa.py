"""独立 NASTRAN 壳网格锐边检查；共边连接及夹角作独立依据，不修复。"""

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


FIXTURE_SCRIPT = """from ansa import base, constants
deck = base.CurrentDeck()
if deck != constants.NASTRAN:
    raise RuntimeError('sharp edge fixture requires NASTRAN')
for kind in ('SHELL', 'SOLID', 'CONM2', 'FACE', '__PROPERTIES__', '__MATERIALS__'):
    if base.CollectEntities(deck, None, kind):
        raise RuntimeError('sharp edge fixture requires a fresh owned seed model')
if len(base.CollectEntities(deck, None, 'GRID')) != 1:
    raise RuntimeError('sharp edge fixture requires one owned seed GRID')

def create(kind, fields):
    entity = base.CreateEntity(deck, kind, fields)
    if entity is None:
        raise RuntimeError('sharp edge fixture creation failed: '+kind)
    labels = entity.card_fields(deck)
    if any(key not in labels for key in fields):
        raise RuntimeError('sharp edge fixture fields missing: '+kind+' '+repr(labels))
    actual = base.GetEntityCardValues(deck, entity, tuple(fields))
    if any(actual.get(key) != value for key, value in fields.items()):
        raise RuntimeError('sharp edge fixture card mismatch: '+kind+' '+repr(actual))
    return entity

material = create('MAT1', {'DEFINED': 'YES', 'E': 200000., 'NU': .3, 'RHO': .01})
prop = create('PSHELL', {'MID1': material._id, 'MID2': material._id,
                        'MID3': material._id, 'T': 2., 'NSM': 0.})

def pair(offset, folded):
    points = [(offset,0.,0.), (offset+10.,0.,0.),
              (offset+10.,10.,0.), (offset,10.,0.)]
    points += ([(offset+10.,0.,10.), (offset+10.,10.,10.)] if folded
               else [(offset+20.,0.,0.), (offset+20.,10.,0.)])
    nodes = [create('GRID', dict(zip(('X1','X2','X3'), point))) for point in points]
    shells = []
    for indexes in ((0,1,2,3), (1,4,5,2)):
        fields = {'type': 'CQUAD4', 'PID': prop._id}
        fields.update({'G'+str(i+1): nodes[index]._id for i,index in enumerate(indexes)})
        shells.append(create('SHELL', fields))
    return [entity._id for entity in shells]

folded = pair(0., True)
flat = pair(40., False)
outside = pair(80., True)
triangle_points = [(120.,0.,0.), (130.,0.,0.), (130.,10.,0.), (130.,0.,10.)]
triangle_nodes = [create('GRID', dict(zip(('X1','X2','X3'), point))) for point in triangle_points]
triangles = []
for indexes in ((0,1,2), (1,3,2)):
    fields = {'type': 'CTRIA3', 'PID': prop._id}
    fields.update({'G'+str(i+1): triangle_nodes[index]._id for i,index in enumerate(indexes)})
    triangles.append(create('SHELL', fields)._id)
shells = base.CollectEntities(deck, None, 'SHELL')
if base.Or(shells) != 1:
    raise RuntimeError('sharp edge fixture display setup failed')
base.RedrawAll()
base.ZoomAll()
RESULT = {'folded_ids': folded, 'flat_ids': flat, 'outside_ids': outside,
          'tri_folded_ids': triangles}
"""


def remote_sharp_edges_readback(expected_pid, expected_arguments):
    import os
    import threading
    from ansa import base, constants, session
    if (os.getpid() != expected_pid or list(session.ProgramArguments()) != expected_arguments
            or threading.current_thread() is not threading.main_thread()):
        raise RuntimeError("Sharp edges oracle requires the owned ANSA main thread")
    deck = base.CurrentDeck()
    if deck != constants.NASTRAN:
        raise RuntimeError("Sharp edges oracle requires NASTRAN")

    def card(entity, fields):
        labels = entity.card_fields(deck)
        if any(field not in labels for field in fields):
            raise RuntimeError("Missing sharp edge fixture fields: " + repr(labels))
        values = base.GetEntityCardValues(deck, entity, fields)
        if any(field not in values or values[field] is None for field in fields):
            raise RuntimeError("Incomplete sharp edge fixture card: " + repr(values))
        return values

    cards = {}
    for kind, fields in (("GRID", ("X1", "X2", "X3")),
                         ("MAT1", ("DEFINED", "E", "NU", "RHO")),
                         ("PSHELL", ("MID1", "MID2", "MID3", "T", "NSM"))):
        cards[kind] = [{"id": entity._id, "fields": card(entity, fields)}
                       for entity in sorted(base.CollectEntities(deck, None, kind), key=lambda e: e._id)]
    cards["SHELL"] = []
    for entity in sorted(base.CollectEntities(deck, None, "SHELL"), key=lambda e: e._id):
        shell_type = card(entity, ("type",))["type"]
        if shell_type not in ("CQUAD4", "CTRIA3"):
            raise RuntimeError("Unsupported sharp edge fixture shell type")
        node_fields = ("G1","G2","G3","G4") if shell_type == "CQUAD4" else ("G1","G2","G3")
        fields = ("type","EID","PID",*node_fields)
        cards["SHELL"].append({"id":entity._id,"fields":card(entity,fields)})
    identities = {kind: sorted([e.ansa_type(deck), e._id] for e in base.CollectEntities(deck, None, kind))
                  for kind in ("FACE", "CONS", "CURVE", "SOLID", "CONM2", "__PROPERTIES__", "__MATERIALS__")}
    return {"deck": deck, "model_path": base.DataBaseName(), "cards": cards, "identities": identities,
            "visible_ids": {kind: sorted(e._id for e in base.CollectEntities(deck, None, kind, filter_visible=True))
                            for kind in ("GRID", "SHELL")}}


def analytic_edges(snapshot, ids, angle):
    points = {row["id"]: tuple(row["fields"][name] for name in ("X1", "X2", "X3"))
              for row in snapshot["cards"]["GRID"]}
    shells = {row["id"]: row["fields"] for row in snapshot["cards"]["SHELL"]}
    edges, normals = {}, {}
    for shell_id in ids:
        fields = shells[shell_id]
        assert fields["type"] in ("CQUAD4", "CTRIA3")
        node_count = 4 if fields["type"] == "CQUAD4" else 3
        nodes = [fields["G"+str(i)] for i in range(1,node_count+1)]
        p, q, r = (points[node_id] for node_id in nodes[:3])
        a, b = [q[i]-p[i] for i in range(3)], [r[i]-p[i] for i in range(3)]
        normal = (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])
        length = math.sqrt(sum(x*x for x in normal))
        assert math.isfinite(length) and length > 0
        normals[shell_id] = tuple(x/length for x in normal)
        for start, end in zip(nodes, nodes[1:]+nodes[:1]):
            edges.setdefault(tuple(sorted((start,end))), []).append(shell_id)
    result = []
    for node_ids, attached in edges.items():
        assert len(attached) <= 2
        if len(attached) != 2:
            continue
        dot = sum(a*b for a,b in zip(normals[attached[0]],normals[attached[1]]))
        normal_angle = math.degrees(math.acos(max(-1., min(1., dot))))
        result.append({"node_ids":list(node_ids), "shell_ids":sorted(attached),
                       "normal_angle":normal_angle, "fails":normal_angle > angle})
    return sorted(result, key=lambda row: row["node_ids"])


def verify_result(result, snapshot, ids, angle):
    assert result["read_only"] is True and result["auto_fix"] is False
    assert result["scope"] == "explicit" and result["entity_type"] == "SHELL"
    assert result["check"] == "sharp_edges" and result["api"] == "ansa.base.checks.mesh.SharpEdges"
    assert [row["id"] for row in result["entities"]] == ids
    assert result["options"] == {"angle":angle,"sharp_edges_type":0,"exclude_edges":0}
    expected = sorted({shell_id for edge in analytic_edges(snapshot, ids, angle)
                       if edge["fails"] for shell_id in edge["shell_ids"]})
    assert result["failed_entity_ids"] == expected, (result["failed_entity_ids"], expected)
    leaf_counts = {"ok":0,"error":0,"warning":0}
    failed_from_reports = set()
    context_nodes = set(result["connection_context"]["node_ids"])
    def visit(row):
        shell_ids = []
        for entity in row["entities"]:
            assert entity["deck"] == snapshot["deck"]
            if entity["type"] == "SHELL":
                assert entity["id"] in ids
                shell_ids.append(entity["id"])
            else:
                assert entity["type"] == "GRID" and entity["id"] in context_nodes
        if row["children"]:
            for child in row["children"]:
                visit(child)
        else:
            assert row["status"] in leaf_counts
            leaf_counts[row["status"]] += 1
            if row["status"] in ("error","warning"):
                assert shell_ids
                failed_from_reports.update(shell_ids)
    for row in result["reports"]:
        visit(row)
    assert result["failed_entity_ids"] == sorted(failed_from_reports)
    assert result["issue_counts"] == leaf_counts
    assert result["issue_count"] == sum(leaf_counts.values())
    assert result["report_count"] == len(result["reports"])
    assert result["native_report_count"] == len(result["reports"])
    assert (result["issue_counts"]["error"]+result["issue_counts"]["warning"] > 0) == bool(expected)
    return analytic_edges(snapshot, ids, angle)


async def verify(run, instructions, identity, helpers, report, *, wait_ui):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    one, two, _original_channel = helpers
    parameters = StdioServerParameters(command=sys.executable,
        args=["-B", "-m", "ansa_mcp_plugin", "--runtime-home", str(instructions["runtime_home"])],
        env={**os.environ, "PYTHONPATH": str(stage.PROJECT_ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"})

    def oracle():
        return two.rpc(identity["port"], remote_sharp_edges_readback, identity["pid"], identity["program_args"])

    def record(name, evidence):
        report["cases"].append({"name":name,"ok":True,"evidence":evidence})
        stage._json_write(run / "sharp-edges-report.json", report)
        print(json.dumps({"case":name,"ok":True}), flush=True)

    with (run / "sharp-edges-backend.stderr.log").open("w", encoding="utf-8") as log:
        async with stdio_client(parameters, errlog=log) as (reader, writer):
            async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=60)) as client:
                await client.initialize()
                h = stage.McpHarness(client, report, run)
                await stage._bind_for_run(h, identity, one, two, identity["port"])
                schemas = await h.list_schema()
                fixture = await h.direct_result("ansa_script_execute", {"script":FIXTURE_SCRIPT})
                await h.direct_result("ansa_model_save", {"path":str(run / "sharp-edges-before.ansa")})
                before = oracle()
                assert len(before["cards"]["GRID"]) == 23 and len(before["cards"]["SHELL"]) == 8
                groups = fixture["result"]
                folded, flat = groups["folded_ids"], groups["flat_ids"]
                tri_folded = groups["tri_folded_ids"]
                ids = folded + flat + tri_folded
                independent = analytic_edges(before, ids, 120.)
                assert sorted(edge["normal_angle"] for edge in independent) == [0.,90.,90.]
                record("schemas_scoped_shell_connectivity_angle_fixture_and_backup", {
                    "schemas":schemas,"fixture":fixture,"independent":before,"analytic":independent,
                    "backup":fingerprint(run / "sharp-edges-before.ansa")})
                fields = {"check":"sharp_edges","deck":before["deck"],"entity_ids":ids}
                original_target, original_model = h.target.copy(), h.model.copy()
                op = "sharp-edges-"+uuid.uuid4().hex
                submitted, completed = await h.task("ansa_mesh_check", fields, operation_id=op)
                report["original_check_task"] = {"submitted":submitted,"completed":completed}
                stage._json_write(run / "sharp-edges-report.json", report)
                result = completed["data"]["result"]
                verify_result(result, before, ids, 120.)
                assert not set(result["failed_entity_ids"]) & set(groups["outside_ids"])
                angle_80 = await h.direct_result("ansa_mesh_check", {**fields,"angle":80.})
                verify_result(angle_80, before, ids, 80.)
                assert angle_80["failed_entity_ids"] == sorted(folded+tri_folded)
                assert not set(angle_80["failed_entity_ids"]) & set(groups["outside_ids"])
                control = await h.direct_result("ansa_mesh_check", {**fields,"entity_ids":flat})
                verify_result(control, before, flat, 120.)
                triangle_default = await h.direct_result("ansa_mesh_check", {**fields,"entity_ids":tri_folded})
                verify_result(triangle_default, before, tri_folded, 120.)
                triangle_80 = await h.direct_result("ansa_mesh_check", {**fields,"entity_ids":tri_folded,"angle":80.})
                verify_result(triangle_80, before, tri_folded, 80.)
                assert oracle() == before
                record("folded_flat_threshold_and_explicit_scope_match_independent_angles", {
                    "default":result,"angle_80":angle_80,"flat":control,"analytic":independent,
                    "triangle_default":triangle_default,"triangle_80":triangle_80})

                snapshot = await h.direct_result("ansa_model_inspect", {
                    "action":"visibility_snapshot","deck":before["deck"]})
                display = {"action":"entity_visibility","deck":before["deck"],"options":{
                    "entity_type":"SHELL","entity_ids":folded+tri_folded,"action":"hide",
                    "expected_visible":snapshot["visible_ids"],"apply_on_contents":False}}
                await h.direct_result("ansa_model_inspect", display)
                hidden = oracle()
                assert not set(hidden["visible_ids"]["SHELL"]) & set(folded+tri_folded)
                assert hidden["cards"] == before["cards"]
                hidden_result = await h.direct_result("ansa_mesh_check", {**fields,"angle":80.})
                verify_result(hidden_result, hidden, ids, 80.)
                assert oracle() == hidden
                snapshot = await h.direct_result("ansa_model_inspect", {
                    "action":"visibility_snapshot","deck":before["deck"]})
                await h.direct_result("ansa_model_inspect", {**display,"options":{**display["options"],
                    "action":"show_also","expected_visible":snapshot["visible_ids"]}})
                assert oracle() == before
                record("hidden_explicit_shells_checked_without_changing_visibility", hidden_result)

                rejected = []
                for changes in ({"entity_ids":[ids[0],99999999]},
                                {"entity_ids":[ids[0],ids[0]]},{"deck":before["deck"]+1}):
                    rejected.append(await h.direct_result("ansa_mesh_check", {**fields,**changes},expected="failed"))
                for changes in ({"entity_ids":[]},{"entity_ids":[True]}, {"angle":True},
                                {"angle":"120"},{"angle":0.},{"angle":180.},{"check":"repair"}):
                    response = await client.call_tool("ansa_mesh_check", {"operation_id":uuid.uuid4().hex,
                        "expected_target":h.target,"expected_model":h.model,**fields,**changes})
                    assert response.isError is True
                    rejected.append({"input":changes,"is_error":response.isError})
                assert oracle() == before
                record("invalid_scope_ids_angle_options_and_deck_reject_without_changes", rejected)
                repeated = await h.call("ansa_mesh_check", {"operation_id":op,
                    "expected_target":original_target,"expected_model":original_model,**fields})
                assert repeated["task_id"] == submitted["task_id"]
                assert repeated["data"]["result"] == completed["data"]["result"] and oracle() == before
                record("original_sharp_edges_operation_idempotency_preserves_cards_and_visibility", {
                    "task_id":submitted["task_id"],"repeated_task_id":repeated["task_id"]})

                saved_path = run / "sharp-edges-after.ansa"
                await h.direct_result("ansa_model_save", {"path":str(saved_path)})
                saved = fingerprint(saved_path)
                await h.direct_result("ansa_model_open", {"path":str(saved_path),
                    "backup_path":str(run / "sharp-edges-reopen-backup.ansa")})
                await h.snapshot_context()
                reopened = oracle()
                assert {k:v for k,v in reopened.items() if k!="model_path"} == {
                    k:v for k,v in before.items() if k!="model_path"}
                after = await h.direct_result("ansa_mesh_check", {**fields,"angle":80.})
                verify_result(after, reopened, ids, 80.)
                assert oracle() == reopened and fingerprint(saved_path) == saved
                assert h.model["epoch"] != original_model["epoch"]
                stale = await h.call("ansa_mesh_check", {"operation_id":"sharp-edges-stale-"+uuid.uuid4().hex,
                    "expected_target":h.target,"expected_model":original_model,**fields},allow_error=True)
                assert stale["ok"] is False and stale["task_id"] is None
                record("saved_model_reopen_preserves_sharp_edges_and_rejects_old_context", {
                    "independent":reopened,"saved":saved,"stale":stale})
                report["core_acceptance"] = True
                stage._json_write(run / "sharp-edges-report.json", report)
                if wait_ui:
                    marker = run / "sharp-edges-ui-confirmation.json"
                    if marker.exists():
                        raise RuntimeError("Refuse stale sharp edge GUI evidence")
                    stage._json_write(run / "sharp-edges-ui-instruction.json", {"identity":identity,
                        "model_path":reopened["model_path"],"visible_ids":reopened["visible_ids"],
                        "instruction":"所属主窗口显示两组四边形折壳、一组平面四边形及一组三角形折壳；GRID 23、ELEMENT 8，保持原可见状态。"})
                    print(json.dumps({"status":"waiting_for_sharp_edges_ui","run_dir":str(run)}),flush=True)
                    deadline = asyncio.get_running_loop().time()+stage.PANEL_WAIT_SECONDS
                    while not marker.is_file():
                        if asyncio.get_running_loop().time() >= deadline:
                            raise TimeoutError("Keep original sharp edge task and owned process for GUI inspection")
                        await asyncio.sleep(.5)
                    visual = json.loads(marker.read_text(encoding="utf-8"))
                    if (visual.get("checked") is not True or visual.get("pid") != identity["pid"]
                            or visual.get("process_start") != identity["process_start"]
                            or visual.get("model_path") != reopened["model_path"]
                            or visual.get("fixture_visible") is not True):
                        raise RuntimeError("Sharp edge GUI evidence mismatches the owned model")
                    assert oracle() == reopened
                    report["visual_acceptance"] = True
                    record("owned_main_window_shell_fixture_and_post_ui_independent_readback", visual)
    report["success"] = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ansa-root",required=True,type=Path)
    parser.add_argument("--runtime-home",required=True,type=Path)
    parser.add_argument("--run-dir",required=True,type=Path)
    parser.add_argument("--wait-ui",action="store_true")
    parser.add_argument("--keep-ansa",action="store_true")
    args = parser.parse_args()
    run, instructions = stage._load_run(args)
    if (run / "seed-baseline.ansa").exists() or (run / "sharp-edges-report.json").exists():
        raise ValueError("只接受新准备目录；未知结果先查询原任务，不重放夹具")
    report = {"scope":"ANSA 2026.1 Windows GUI/NASTRAN 显式 SHELL 锐边检查",
              "started_at":datetime.now(timezone.utc).isoformat(),"success":False,"cases":[],
              "core_acceptance":False,"visual_acceptance":False}
    identity = helpers = None
    try:
        identity, _ansa, _grpc, helpers = stage._prepare_owned_ansa(run, instructions)
        report["identity"] = identity
        stage._json_write(run / "sharp-edges-report.json", report)
        report["fixture"] = helpers[1].rpc(identity["port"],remote_initialize_fixture,
            identity["pid"],identity["program_args"],identity["seed_path"],identity["seed_entity_id"])
        asyncio.run(verify(run,instructions,identity,helpers,report,wait_ui=args.wait_ui))
    except BaseException as exc:
        report["error"] = {"type":type(exc).__name__,"message":str(exc)}
        report["exception_tree"] = stage._exception_tree(exc)
        (run / "sharp-edges-report.error.log").write_text(traceback.format_exc(),encoding="utf-8")
        raise
    finally:
        if identity is not None and helpers is not None and report["success"] and not args.keep_ansa:
            try:
                report["cleanup"] = stage._cleanup_owned_ansa(identity,instructions,helpers)
            except Exception as exc:
                report["success"] = False
                report["cleanup_error"] = {"type":type(exc).__name__,"message":str(exc)}
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        stage._json_write(run / "sharp-edges-report.json",report)
    if not report["success"]:
        raise RuntimeError("Sharp edge acceptance or owned process cleanup failed")
    print(json.dumps({"success":True,"report":str(run / "sharp-edges-report.json")}),flush=True)


if __name__ == "__main__":
    main()
