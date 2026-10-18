"""Owned NASTRAN CBAR-to-CROD acceptance with independent topology readback."""

from datetime import timedelta
import hashlib
import os
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src"), str(Path(__file__).parent)]
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import stage_three_ansa as common
import stage_three_api_catalog_ansa as owned
from stage_three_connections_ansa import independent_script


def remote_fixture(expected_pid, expected_args, fixture_path):
    import os, threading
    from ansa import base, constants, session
    if (os.getpid() != expected_pid or list(session.ProgramArguments()) != expected_args
            or threading.current_thread() is not threading.main_thread()
            or base.CurrentDeck() != constants.NASTRAN):
        raise RuntimeError("Owned identity changed before element type fixture")
    if base.CollectEntities(constants.NASTRAN, None, "__ELEMENTS__"):
        raise RuntimeError("Element type fixture requires no preexisting FE")
    status = base.InputNastran(fixture_path, nodes_id="nooffset", elements_id="nooffset",
        properties_id="nooffset", materials_id="nooffset", header="skip", model_action="merge_model")
    if type(status) is not int or status != 1:
        raise RuntimeError("Element type fixture import failed")
    return {"imported": True}


def remote_oracle(expected_pid, expected_args):
    import os, threading, math
    from ansa import base, constants, session
    if (os.getpid() != expected_pid or list(session.ProgramArguments()) != expected_args
            or threading.current_thread() is not threading.main_thread()
            or base.CurrentDeck() != constants.NASTRAN):
        raise RuntimeError("Owned identity changed before independent element readback")
    deck = constants.NASTRAN
    def ref(entity):return {"deck": deck, "type": entity.ansa_type(deck), "id": entity._id}
    fields_by_type = {
        "CBAR": ("PID", "GA", "GB", "Name", "Orient", "X1", "X2", "X3", "OFFT", "PA", "PB",
                 "W1A", "W2A", "W3A", "W1B", "W2B", "W3B", "FROZEN", "FROZEN_ID", "FROZEN_DELETE"),
        "CROD": ("PID", "G1", "G2", "Name", "FROZEN", "FROZEN_ID", "FROZEN_DELETE"),
        "GRID": ("CP", "X1", "X2", "X3", "CD"),
        "PBAR": ("MID", "A", "I1", "I2", "J", "NSM"),
        "PROD": ("MID", "A", "J", "C", "NSM"),
        "MAT1": ("E", "G", "NU", "RHO"),
    }
    rows = {}
    for kind, fields in fields_by_type.items():
        rows[kind] = []
        entities = base.CollectEntities(deck, None, kind, False)
        if type(entities) not in (list, tuple):raise RuntimeError("Independent collection is incomplete")
        for entity in sorted(entities, key=lambda item: item._id):
            labels = entity.card_fields(deck)
            if not set(fields).issubset(labels):raise RuntimeError(f"Independent {kind} fields unavailable")
            values = base.GetEntityCardValues(deck, entity, fields)
            if type(values) is not dict or set(values) != set(fields):raise RuntimeError("Independent card read is incomplete")
            row = {"entity": ref(entity), "fields": values}
            if kind == "GRID":
                position = list(entity.position)
                if len(position) != 3 or any(type(x) not in (int, float) or not math.isfinite(x) for x in position):
                    raise RuntimeError("Independent node coordinates are incomplete")
                row["position"] = position
            rows[kind].append(row)
    return {"deck": deck, "database": base.DataBaseName(), "rows": rows,
            "elements": sorted([ref(item) for item in base.CollectEntities(deck, None, "__ELEMENTS__", False)], key=lambda row:(row["type"], row["id"]))}


def assert_conversion(before, after, selected):
    for kind in ("GRID", "PBAR", "PROD", "MAT1"):
        if after["rows"][kind] != before["rows"][kind]:raise AssertionError(f"Conversion changed {kind}")
    selected = set(selected)
    source_rows = {row["entity"]["id"]: row for row in before["rows"]["CBAR"]}
    expected_bars = [row for row in before["rows"]["CBAR"] if row["entity"]["id"] not in selected]
    if after["rows"]["CBAR"] != expected_bars:raise AssertionError("Conversion changed an unselected CBAR")
    expected_rods = list(before["rows"]["CROD"])
    for element_id in selected:
        source = source_rows[element_id]
        expected_rods.append({"entity": {**source["entity"], "type": "CROD"}, "fields": {
            "PID": 101, "G1": source["fields"]["GA"], "G2": source["fields"]["GB"],
            **{name: source["fields"][name] for name in ("Name", "FROZEN", "FROZEN_ID", "FROZEN_DELETE")}}})
    expected_rods.sort(key=lambda row: row["entity"]["id"])
    if after["rows"]["CROD"] != expected_rods:raise AssertionError("CROD identity, connectivity, PID or name differs")
    expected_refs = [{**row, "type": "CROD"} if row["type"] == "CBAR" and row["id"] in selected else row
                     for row in before["elements"]]
    if after["elements"] != sorted(expected_refs, key=lambda row:(row["type"], row["id"])):
        raise AssertionError("Conversion changed the FE range")


async def verify(instructions, identity, helpers, run_dir, report, path):
    one, two, _ = helpers
    fixture_path = run_dir / "element-types.nas"
    fixture_path.write_text("BEGIN BULK\nMAT1,1,210000.,,0.3,7.85E-9\n"
        "PBAR,100,1,0.01,0.0001,0.0001,0.0002,0.\nPROD,101,1,0.01,0.0001,0.\n"
        + "".join(f"GRID,{index},,{index-11}.,0.,0.\n" for index in range(11, 19))
        + "".join(f"CBAR,{element_id},100,{node},{node+1},0.,1.,0.\n" for element_id,node in ((201,11),(202,13),(203,15),(204,17)))
        + "CROD,210,101,11,18\nENDDATA\n", encoding="ascii")
    parameters = StdioServerParameters(command=sys.executable,
        args=["-B", "-m", "ansa_mcp_plugin", "--runtime-home", instructions["runtime_home"]],
        env={**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"})
    receipts = []
    async def oracle(h):return await independent_script(h, remote_oracle, identity["pid"], identity["program_args"])
    def request(snapshot, ids, backup_name):
        by_id = {row["entity"]["id"]: row["fields"] for row in snapshot["rows"]["CBAR"]}
        prop = next(row["fields"] for row in snapshot["rows"]["PROD"] if row["entity"]["id"] == 101)
        return {"deck": snapshot["deck"], "source_type": "CBAR", "target_type": "CROD",
            "elements": [{"entity_id": element_id, "expected_fields": {key: by_id[element_id][key] for key in ("PID", "GA", "GB", "Name")}} for element_id in ids],
            "target_property_id": 101, "expected_property_fields": prop, "backup_path": str(run_dir / backup_name)}
    async def convert(h, before, ids, backup_name):
        args = request(before, ids, backup_name)
        operation = "element-type-" + uuid.uuid4().hex
        submitted, completed = await h.task("ansa_element_change_type", args, operation_id=operation)
        result = completed["data"]["result"]
        if result.get("scope_verified") is not True:raise AssertionError("Product conversion scope was not verified")
        after = await oracle(h)
        assert_conversion(before, after, ids)
        backup = Path(args["backup_path"])
        if not backup.is_file() or not backup.stat().st_size:raise AssertionError("Conversion backup missing")
        receipt = {"request": args, "operation_id": operation, "task_id": submitted["task_id"],
            "result": result, "target": completed["target"], "model": completed["model"],
            "backup_sha256": hashlib.sha256(backup.read_bytes()).hexdigest()}
        receipts.append(receipt)
        return after, receipt
    with (run_dir / "element-types-backend.stderr.log").open("w", encoding="utf-8") as log:
        async with stdio_client(parameters, errlog=log) as (reader, writer):
            async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=120)) as client:
                await client.initialize()
                h = common.McpHarness(client, report, run_dir)
                report["schema"] = await h.list_schema()
                await common._bind_for_run(h, identity, one, two, identity["port"])
                await independent_script(h, remote_fixture, identity["pid"], identity["program_args"], str(fixture_path), allow_model_change=True)
                await h.snapshot_context()
                initial = await oracle(h)
                if [r["entity"]["id"] for r in initial["rows"]["CBAR"]] != [201,202,203,204] or len(initial["rows"]["GRID"]) != 9:
                    raise AssertionError("Independent fixture topology differs")
                owned.record(report, path, "owned_fixture_schema_and_independent_cards", {"independent": initial})

                bad = request(initial, [201,202], "must-not-create.ansa")
                stale = {**bad, "elements": [bad["elements"][0], {"entity_id":202,"expected_fields":{"PID":999,"GA":13,"GB":14}}]}
                wrong_property = {**bad, "expected_property_fields": {**bad["expected_property_fields"], "A": 99.0}}
                for args in (stale, wrong_property, {**bad,"target_property_id":999999}, {**bad,"deck":2}):
                    await h.task("ansa_element_change_type", args, expected="failed")
                    if Path(bad["backup_path"]).exists() or await oracle(h) != initial:
                        raise AssertionError("Preflight rejection wrote a backup or modified the model")
                invalid = ({"elements":[]}, {"elements":[bad["elements"][0]]*2}, {"target_property_id":True},
                    {"source_type":"SHELL"}, {"target_type":"CBAR"}, {"deck":"1"}, {"fix":True})
                for change in invalid:
                    response = await client.call_tool("ansa_element_change_type", {"operation_id":"invalid-"+uuid.uuid4().hex,
                        "expected_target":h.target,"expected_model":h.model,**bad,**change})
                    if not response.isError:raise AssertionError(f"Invalid conversion accepted: {change}")
                owned.record(report, path, "whole_batch_and_property_preflight_strict_inputs_without_effects", {"schema_rejections":len(invalid),"unchanged":True})

                after_one, first = await convert(h, initial, [201], "one-backup.ansa")
                owned.record(report, path, "single_conversion_and_unselected_topology_unchanged", {"receipt":first,"independent":after_one})
                after_batch, second = await convert(h, after_one, [202,203], "batch-backup.ansa")
                _, replay = await h.task("ansa_element_change_type", second["request"], operation_id=second["operation_id"])
                if replay["task_id"] != second["task_id"] or replay["data"]["result"] != second["result"]:
                    raise AssertionError("Conversion replay did not return original task")
                conflict = await h.call("ansa_element_change_type", {"operation_id":second["operation_id"],
                    "expected_target":h.target,"expected_model":h.model,**second["request"],"backup_path":str(run_dir/"conflict.ansa")},allow_error=True)
                if conflict.get("ok") is not False or conflict.get("task_id") is not None or await oracle(h) != after_batch:
                    raise AssertionError("Operation conflict or duplicate modified the model")
                owned.record(report, path, "batch_conversion_original_receipt_and_conflict", {"receipt":second,"independent":after_batch})

                saved = run_dir / "accepted-element-types.ansa"
                await h.direct_result("ansa_model_save", {"path":str(saved)})
                saved_hash = hashlib.sha256(saved.read_bytes()).hexdigest()
                await h.direct_result("ansa_model_open", {"path":first["request"]["backup_path"],"backup_path":str(run_dir/"before-restore.ansa")})
                await h.snapshot_context()
                restored = await oracle(h)
                if restored["rows"] != initial["rows"] or restored["elements"] != initial["elements"]:
                    raise AssertionError("Actual conversion backup did not restore original topology")
                for index in (1,2):
                    old_target, old_model = dict(h.target),dict(h.model)
                    await h.direct_result("ansa_model_open", {"path":str(saved),"backup_path":str(run_dir/f"reopen-{index}-backup.ansa")})
                    await h.snapshot_context()
                    reopened = await oracle(h)
                    if reopened["rows"] != after_batch["rows"] or reopened["elements"] != after_batch["elements"]:
                        raise AssertionError("Saved conversion topology did not survive reopening")
                    if h.model["epoch"] == old_model["epoch"]:raise AssertionError("Model generation unchanged on open")
                    stale_result = await h.call("ansa_element_change_type", {"operation_id":"stale-"+uuid.uuid4().hex,
                        "expected_target":old_target,"expected_model":old_model,**request(initial,[204],f"stale-{index}.ansa")},allow_error=True)
                    if stale_result.get("ok") is not False or stale_result.get("task_id") is not None:raise AssertionError("Stale conversion accepted")
                if hashlib.sha256(saved.read_bytes()).hexdigest() != saved_hash:raise AssertionError("Saved file changed during readback")
                final = await oracle(h)
                previous_backend = h.target["backend_id"]
                owned.record(report,path,"actual_backup_restore_save_reopen_and_stale_context",{"saved":str(saved),"sha256":saved_hash,"independent":final})
    with (run_dir/"element-types-restarted-backend.stderr.log").open("w",encoding="utf-8") as log:
        async with stdio_client(parameters,errlog=log) as (reader,writer):
            async with ClientSession(reader,writer,read_timeout_seconds=timedelta(seconds=120)) as client:
                await client.initialize()
                h=common.McpHarness(client,report,run_dir)
                status=await h.call("ansa_connection_status")
                if status["data"]["backend_id"]==previous_backend:raise AssertionError("Backend did not restart")
                for receipt in receipts:
                    queried=await h.call("ansa_task_query",{"operation_id":receipt["operation_id"]})
                    if any(queried[key]!=receipt[key] for key in ("task_id","target","model")) or queried["data"]["result"]!=receipt["result"]:
                        raise AssertionError("Persisted conversion receipt differs")
                    if hashlib.sha256(Path(receipt["request"]["backup_path"]).read_bytes()).hexdigest()!=receipt["backup_sha256"]:
                        raise AssertionError("Original backup was changed")
                await common._bind_for_run(h,identity,one,two,identity["port"])
                if await oracle(h)!=final:raise AssertionError("Original task queries replayed conversion")
                owned.record(report,path,"backend_restart_original_receipts_backups_and_independent_model",{"task_ids":[r["task_id"] for r in receipts],"unchanged":True})


if __name__ == "__main__":
    raise SystemExit(owned.main(report_name="element-types-report.json",
        scope="NASTRAN explicit CBAR to CROD with existing PROD and backup; no canvas acceptance",verify_callback=verify))
