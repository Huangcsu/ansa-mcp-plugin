"""独立 NASTRAN 自由节点检查；连接卡片作独立依据，不自动修复。"""

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


FIXTURE_SCRIPT = """from ansa import base, constants
deck = base.CurrentDeck()
if deck != constants.NASTRAN:
    raise RuntimeError('free node fixture requires NASTRAN')
for kind in ('SHELL', 'SOLID', 'CONM2', 'FACE', '__PROPERTIES__', '__MATERIALS__'):
    if base.CollectEntities(deck, None, kind):
        raise RuntimeError('free node fixture requires a fresh owned seed model')
if len(base.CollectEntities(deck, None, 'GRID')) != 1:
    raise RuntimeError('free node fixture requires one owned seed GRID')

def create(kind, fields):
    entity = base.CreateEntity(deck, kind, fields)
    if entity is None:
        raise RuntimeError('free node fixture creation failed: '+kind)
    labels = entity.card_fields(deck)
    if any(key not in labels for key in fields):
        raise RuntimeError('free node fixture fields missing: '+kind+' '+repr(labels))
    actual = base.GetEntityCardValues(deck, entity, tuple(fields))
    if any(actual.get(key) != value for key, value in fields.items()):
        raise RuntimeError('free node fixture card mismatch: '+kind+' '+repr(actual))
    return entity

material = create('MAT1', {'DEFINED': 'YES', 'E': 200000., 'NU': .3, 'RHO': .01})
prop = create('PSHELL', {'MID1': material._id, 'MID2': material._id,
                        'MID3': material._id, 'T': 2., 'NSM': 0.})
nodes = [create('GRID', dict(zip(('X1','X2','X3'), point))) for point in
         ((0.,0.,0.), (20.,0.,0.), (20.,10.,0.), (0.,10.,0.))]
fields = {'type': 'CQUAD4', 'PID': prop._id}
fields.update({'G'+str(i+1): node._id for i, node in enumerate(nodes)})
quad = create('SHELL', fields)
free = create('GRID', {'X1': 30., 'X2': 0., 'X3': 0.})
mass_node = create('GRID', {'X1': 40., 'X2': 0., 'X3': 0.})
mass = create('CONM2', {'G': mass_node._id, 'M': 2.})
all_nodes = base.CollectEntities(deck, None, 'GRID')
if base.Or([quad, mass] + all_nodes) != 1:
    raise RuntimeError('free node fixture display setup failed')
base.RedrawAll()
base.ZoomAll()
RESULT = {'quad_id': quad._id, 'mass_id': mass._id, 'free_id': free._id,
          'mass_node_id': mass_node._id, 'connected_ids': [node._id for node in nodes]}
"""


def remote_free_nodes_readback(expected_pid, expected_arguments):
    import os
    import threading
    from ansa import base, constants, session
    if (os.getpid() != expected_pid or list(session.ProgramArguments()) != expected_arguments
            or threading.current_thread() is not threading.main_thread()):
        raise RuntimeError("Free nodes oracle requires the owned ANSA main thread")
    deck = base.CurrentDeck()
    if deck != constants.NASTRAN:
        raise RuntimeError("Free nodes oracle requires NASTRAN")

    def card(entity, fields):
        labels = entity.card_fields(deck)
        if any(field not in labels for field in fields):
            raise RuntimeError("Missing free node fixture fields: " + repr(labels))
        values = base.GetEntityCardValues(deck, entity, fields)
        if any(field not in values or values[field] is None for field in fields):
            raise RuntimeError("Incomplete free node fixture card: " + repr(values))
        return values

    cards = {}
    for kind, fields in (
        ("GRID", ("X1", "X2", "X3")),
        ("MAT1", ("MID", "DEFINED", "E", "NU", "RHO")),
        ("PSHELL", ("PID", "MID1", "MID2", "MID3", "T", "NSM")),
        ("SHELL", ("type", "EID", "PID", "G1", "G2", "G3", "G4")),
        ("CONM2", ("EID", "G", "M")),
    ):
        cards[kind] = [{"id": entity._id, "fields": card(entity, fields)}
                       for entity in sorted(base.CollectEntities(deck, None, kind), key=lambda e: e._id)]
    identities = {kind: sorted([e.ansa_type(deck), e._id] for e in base.CollectEntities(deck, None, kind))
                  for kind in ("FACE", "CONS", "CURVE", "SOLID", "__PROPERTIES__", "__MATERIALS__")}
    return {"deck": deck, "model_path": base.DataBaseName(), "cards": cards, "identities": identities,
            "visible_ids": {kind: sorted(e._id for e in base.CollectEntities(deck, None, kind, filter_visible=True))
                            for kind in ("GRID", "SHELL", "CONM2")}}


def expected_free_ids(snapshot, ids, mass_free):
    cards = snapshot["cards"]
    node_ids = {row["id"] for row in cards["GRID"]}
    connected = {row["fields"]["G" + str(index)] for row in cards["SHELL"] for index in range(1, 5)}
    if not mass_free:
        connected.update(row["fields"]["G"] for row in cards["CONM2"])
    assert set(ids) <= node_ids and connected <= node_ids
    return sorted(set(ids) - connected)


def verify_result(result, snapshot, ids, mass_free):
    assert result["read_only"] is True and result["auto_fix"] is False
    assert result["scope"] == "explicit" and result["entity_type"] == "GRID"
    assert result["check"] == "free_nodes" and result["api"] == "ansa.base.checks.general.FreeNodes"
    assert [row["id"] for row in result["entities"]] == ids
    assert result["options"]["consider_nodes_with_mass_free"] is mass_free
    assert result["options"]["do_not_report_free_nodes_attached_to_non_visible_entities"] is False
    assert result["options"]["report_scalar_nodes"] is False
    assert result["failed_entity_ids"] == expected_free_ids(snapshot, ids, mass_free)
    assert result["issue_count"] == sum(result["issue_counts"].values())
    assert result["report_count"] == len(result["reports"])
    if result["failed_entity_ids"]:
        assert result["issue_counts"]["error"] + result["issue_counts"]["warning"] > 0
    else:
        assert result["issue_counts"]["error"] + result["issue_counts"]["warning"] == 0
    json.dumps(result, ensure_ascii=False, allow_nan=False)


async def verify(run, instructions, identity, helpers, report, *, wait_ui):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    one, two, _original = helpers
    parameters = StdioServerParameters(command=sys.executable,
        args=["-B", "-m", "ansa_mcp_plugin", "--runtime-home", instructions["runtime_home"]],
        env={**os.environ, "PYTHONPATH": str(stage.PROJECT_ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"})

    def oracle():
        return two.rpc(identity["port"], remote_free_nodes_readback, identity["pid"], identity["program_args"])

    def record(name, evidence):
        report["cases"].append({"name": name, "ok": True, "evidence": evidence})
        stage._json_write(run / "free-nodes-report.json", report)
        print(json.dumps({"case": name, "ok": True}), flush=True)

    with (run / "free-nodes-backend.stderr.log").open("w", encoding="utf-8") as log:
        async with stdio_client(parameters, errlog=log) as (reader, writer):
            async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=60)) as client:
                await client.initialize()
                h = stage.McpHarness(client, report, run)
                await stage._bind_for_run(h, identity, one, two, identity["port"])
                schemas = await h.list_schema()
                fixture = await h.direct_result("ansa_script_execute", {"script": FIXTURE_SCRIPT})
                await h.direct_result("ansa_model_save", {"path": str(run / "free-nodes-before.ansa")})
                before = oracle()
                assert len(before["cards"]["GRID"]) == 7 and len(before["cards"]["SHELL"]) == 1
                assert len(before["cards"]["CONM2"]) == 1
                ids = [row["id"] for row in before["cards"]["GRID"]]
                connected = fixture["result"]["connected_ids"]
                assert expected_free_ids(before, ids, True) == sorted([
                    identity["seed_entity_id"], fixture["result"]["free_id"], fixture["result"]["mass_node_id"]])
                record("schemas_scoped_connectivity_fixture_and_backup", {
                    "schemas": schemas, "fixture": fixture, "independent": before,
                    "backup": fingerprint(run / "free-nodes-before.ansa")})

                fields = {"check": "free_nodes", "deck": before["deck"], "entity_ids": ids}
                original_target, original_model = h.target.copy(), h.model.copy()
                op = "free-nodes-" + uuid.uuid4().hex
                submitted, completed = await h.task("ansa_check_run", fields, operation_id=op)
                report["original_check_task"] = {"submitted": submitted, "completed": completed}
                stage._json_write(run / "free-nodes-report.json", report)
                result = completed["data"]["result"]
                verify_result(result, before, ids, True)
                assert oracle() == before
                mass_connected = await h.direct_result("ansa_check_run", {
                    **fields, "consider_nodes_with_mass_free": False})
                verify_result(mass_connected, before, ids, False)
                subset = [ids[-1], ids[0]]
                selected = await h.direct_result("ansa_check_run", {**fields, "entity_ids": subset})
                verify_result(selected, before, subset, True)
                empty = await h.direct_result("ansa_check_run", {**fields, "entity_ids": connected})
                verify_result(empty, before, connected, True)
                single_connected = await h.direct_result("ansa_check_run", {
                    **fields, "entity_ids": [connected[0]]})
                verify_result(single_connected, before, [connected[0]], True)
                assert single_connected["failed_entity_ids"] == []
                assert single_connected["connection_context"]["node_ids"] == sorted(connected)
                assert single_connected["connection_context"]["entities"] == [{
                    "deck": before["deck"], "type": "SHELL", "id": fixture["result"]["quad_id"]}]
                assert oracle() == before
                record("free_mass_only_explicit_subset_and_no_findings_match_connectivity_cards", {
                    "default": result, "mass_connected": mass_connected, "subset": selected, "empty": empty,
                    "single_connected_node": single_connected})

                snapshot = await h.direct_result("ansa_model_inspect", {
                    "action": "visibility_snapshot", "deck": before["deck"]})
                display = {"action": "entity_visibility", "deck": before["deck"], "options": {
                    "entity_type": "SHELL", "entity_ids": [fixture["result"]["quad_id"]],
                    "action": "hide", "expected_visible": snapshot["visible_ids"], "apply_on_contents": False}}
                await h.direct_result("ansa_model_inspect", display)
                hidden = oracle()
                assert hidden["visible_ids"]["SHELL"] == [] and hidden["cards"] == before["cards"]
                hidden_result = await h.direct_result("ansa_check_run", fields)
                verify_result(hidden_result, hidden, ids, True)
                assert oracle() == hidden
                snapshot = await h.direct_result("ansa_model_inspect", {
                    "action": "visibility_snapshot", "deck": before["deck"]})
                await h.direct_result("ansa_model_inspect", {**display, "options": {**display["options"],
                    "action": "show_also", "expected_visible": snapshot["visible_ids"]}})
                assert oracle() == before
                record("hidden_shell_connections_are_retained_and_check_preserves_visibility", {
                    "hidden_independent": hidden, "result": hidden_result})

                rejected = []
                for changes in ({"entity_ids": [ids[0], 99999999]},
                                {"entity_ids": [ids[0], ids[0]]}, {"deck": before["deck"] + 1}):
                    rejected.append(await h.direct_result("ansa_check_run", {**fields, **changes}, expected="failed"))
                for changes in ({"entity_ids": []}, {"entity_ids": [True]},
                                {"consider_nodes_with_mass_free": 1}, {"check": "repair"}):
                    response = await client.call_tool("ansa_check_run", {"operation_id": uuid.uuid4().hex,
                        "expected_target": h.target, "expected_model": h.model, **fields, **changes})
                    assert response.isError is True
                    rejected.append({"input": changes, "is_error": response.isError,
                        "text": [item.text for item in response.content if getattr(item, "type", None) == "text"]})
                assert oracle() == before
                record("invalid_scope_ids_options_and_deck_reject_without_changes", rejected)

                repeated = await h.call("ansa_check_run", {"operation_id": op,
                    "expected_target": original_target, "expected_model": original_model, **fields})
                assert repeated["task_id"] == submitted["task_id"]
                assert repeated["data"]["result"] == completed["data"]["result"] and oracle() == before
                record("original_check_operation_idempotency_preserves_cards_and_visibility", {
                    "task_id": submitted["task_id"], "repeated_task_id": repeated["task_id"]})

                saved_path = run / "free-nodes-after.ansa"
                await h.direct_result("ansa_model_save", {"path": str(saved_path)})
                saved = fingerprint(saved_path)
                await h.direct_result("ansa_model_open", {"path": str(saved_path),
                    "backup_path": str(run / "free-nodes-reopen-backup.ansa")})
                await h.snapshot_context()
                reopened = oracle()
                assert {k: v for k, v in reopened.items() if k != "model_path"} == {
                    k: v for k, v in before.items() if k != "model_path"}
                for flag in (True, False):
                    after = await h.direct_result("ansa_check_run", {**fields, "consider_nodes_with_mass_free": flag})
                    verify_result(after, reopened, ids, flag)
                assert oracle() == reopened and fingerprint(saved_path) == saved
                assert h.model["epoch"] != original_model["epoch"]
                stale = await h.call("ansa_check_run", {"operation_id": "free-nodes-stale-" + uuid.uuid4().hex,
                    "expected_target": h.target, "expected_model": original_model, **fields}, allow_error=True)
                assert stale["ok"] is False and stale["task_id"] is None
                record("saved_model_reopen_preserves_free_nodes_and_rejects_old_context", {
                    "independent": reopened, "saved": saved, "stale": stale})
                report["core_acceptance"] = True
                stage._json_write(run / "free-nodes-report.json", report)

                if wait_ui:
                    marker = run / "free-nodes-ui-confirmation.json"
                    if marker.exists():
                        raise RuntimeError("Refuse stale free node GUI evidence")
                    stage._json_write(run / "free-nodes-ui-instruction.json", {"identity": identity,
                        "model_path": reopened["model_path"], "visible_ids": reopened["visible_ids"],
                        "instruction": "所属主窗口保持四边形和质量标记可见；数据库 GRID 7、ELEMENT 2，GRID 显示开关保持独立回读时的状态。"})
                    print(json.dumps({"status": "waiting_for_free_nodes_ui", "run_dir": str(run)}), flush=True)
                    deadline = asyncio.get_running_loop().time() + stage.PANEL_WAIT_SECONDS
                    while not marker.is_file():
                        if asyncio.get_running_loop().time() >= deadline:
                            raise TimeoutError("Keep original check task and owned process for GUI inspection")
                        await asyncio.sleep(.5)
                    visual = json.loads(marker.read_text(encoding="utf-8"))
                    if (visual.get("checked") is not True or visual.get("pid") != identity["pid"]
                            or visual.get("process_start") != identity["process_start"]
                            or visual.get("model_path") != reopened["model_path"]
                            or visual.get("fixture_visible") is not True):
                        raise RuntimeError("Free node GUI evidence mismatches the owned model")
                    assert oracle() == reopened
                    report["visual_acceptance"] = True
                    record("owned_main_window_fixture_and_post_ui_independent_readback", visual)
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
    if (run / "seed-baseline.ansa").exists() or (run / "free-nodes-report.json").exists():
        raise ValueError("只接受新准备目录；未知结果先查询原任务，不重放夹具")
    report = {"scope": "ANSA 2026.1 Windows GUI/NASTRAN 显式 GRID 自由节点检查",
              "started_at": datetime.now(timezone.utc).isoformat(), "success": False, "cases": [],
              "core_acceptance": False, "visual_acceptance": False}
    identity = helpers = None
    try:
        identity, _ansa, _grpc, helpers = stage._prepare_owned_ansa(run, instructions)
        report["identity"] = identity
        stage._json_write(run / "free-nodes-report.json", report)
        report["fixture"] = helpers[1].rpc(identity["port"], remote_initialize_fixture,
            identity["pid"], identity["program_args"], identity["seed_path"], identity["seed_entity_id"])
        asyncio.run(verify(run, instructions, identity, helpers, report, wait_ui=args.wait_ui))
    except BaseException as exc:
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
        report["exception_tree"] = stage._exception_tree(exc)
        (run / "free-nodes-report.error.log").write_text(traceback.format_exc(), encoding="utf-8")
        raise
    finally:
        if identity is not None and helpers is not None and report["success"] and not args.keep_ansa:
            try:
                report["cleanup"] = stage._cleanup_owned_ansa(identity, instructions, helpers)
            except Exception as exc:
                report["success"] = False
                report["cleanup_error"] = {"type": type(exc).__name__, "message": str(exc)}
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        stage._json_write(run / "free-nodes-report.json", report)
    if not report["success"]:
        raise RuntimeError("Free node acceptance or owned process cleanup failed")
    print(json.dumps({"success": True, "report": str(run / "free-nodes-report.json")}), flush=True)


if __name__ == "__main__":
    main()
