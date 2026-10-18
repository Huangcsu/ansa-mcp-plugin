"""Owned MCP acceptance for one existing, closed NASTRAN VOLUME."""

from datetime import timedelta
import hashlib
import math
import os
from pathlib import Path
import sys
import uuid

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import stage_three_ansa as common
import stage_three_api_catalog_ansa as owned
from stage_three_connections_ansa import independent_script

ROOT = Path(__file__).resolve().parents[2]
BOUNDARY_IDS = list(range(2101, 2107))


def remote_fixture(expected_pid, expected_arguments, nas_path):
    import os
    import threading
    from ansa import base, constants, mesh, session
    if (os.getpid() != expected_pid or list(session.ProgramArguments()) != expected_arguments
            or threading.current_thread() is not threading.main_thread()):
        raise RuntimeError("Volume fixture requires its owned ANSA main thread")
    deck = base.CurrentDeck()
    if deck != constants.NASTRAN or any(base.CollectEntities(deck, None, kind) for kind in ("SHELL", "VOLUME", "SOLID")):
        raise RuntimeError("Volume fixture requires the empty owned NASTRAN seed")
    rc = base.InputNastran(nas_path, nodes_id="offset-freeid", elements_id="offset-freeid",
        properties_id="offset-freeid", materials_id="offset-freeid", header="merge", model_action="merge_model")
    if type(rc) is not int or rc != 1:
        raise RuntimeError("Volume fixture import did not return int 1")
    shells = [base.GetEntity(deck, "SHELL", entity_id) for entity_id in range(2101, 2107)]
    if any(entity is None for entity in shells):
        raise RuntimeError("Volume fixture boundary SHELL missing")
    volumes = mesh.VolumesDetect(1, True, False, False, shells)
    if type(volumes) is not list or len(volumes) != 1:
        raise RuntimeError("Volume fixture must detect exactly one explicit closed volume")
    volume = volumes[0]
    labels = volume.card_fields(deck)
    if type(labels) is not list or "PID" not in labels:
        raise RuntimeError("Volume fixture PID field unavailable")
    rc = base.SetEntityCardValues(deck, volume, {"PID": 3102})
    if type(rc) is not int or rc != 0 or base.GetEntityCardValues(deck, volume, ("PID",))["PID"] != 3102:
        raise RuntimeError("Volume fixture could not assign its existing defined PSOLID")
    return {"deck": deck, "volume_id": volume._id, "boundary_shell_ids": list(range(2101, 2107)),
            "psolid_id": 3102, "mat1_id": 4101, "fixture_detect_only": True}


def remote_oracle(expected_pid, expected_arguments):
    import math
    import os
    import threading
    from ansa import base, session
    if (os.getpid() != expected_pid or list(session.ProgramArguments()) != expected_arguments
            or threading.current_thread() is not threading.main_thread()):
        raise RuntimeError("Volume oracle requires its owned ANSA main thread")
    deck = base.CurrentDeck()

    def reference(entity):
        return {"deck": deck, "type": entity.ansa_type(deck), "id": entity._id}

    def safe(value):
        if value is None or type(value) in (str, bool, int):
            return value
        if type(value) is float and math.isfinite(value):
            return value
        if isinstance(value, base.Entity):
            return reference(value)
        if type(value) in (list, tuple):
            return [safe(item) for item in value]
        if type(value) is dict and all(type(key) is str for key in value):
            return {key: safe(item) for key, item in value.items()}
        raise RuntimeError("Independent volume oracle received an unsupported card value")

    def collect(kind):
        entities = base.CollectEntities(deck, None, kind, recursive=False)
        if type(entities) is not list or len(entities) > 10000:
            raise RuntimeError("Independent volume oracle collection incomplete")
        return sorted(entities, key=lambda entity: (entity.ansa_type(deck), entity._id))

    rows = {}
    for kind in ("GRID", "SHELL", "SOLID", "VOLUME", "PSOLID", "MAT1", "PSHELL"):
        rows[kind] = []
        for entity in collect(kind):
            labels = entity.card_fields(deck)
            if type(labels) is not list or not labels:
                raise RuntimeError("Independent volume oracle card fields incomplete")
            values = base.GetEntityCardValues(deck, entity, tuple(labels))
            if type(values) is not dict or set(values) != set(labels):
                raise RuntimeError("Independent volume oracle card values incomplete")
            row = {"entity": reference(entity), "fields": safe(values)}
            if kind == "VOLUME":
                row["shell_ids"] = sorted(e._id for e in base.CollectEntities(deck, entity, "SHELL"))
            if kind in ("SHELL", "SOLID"):
                row["node_ids"] = sorted(e._id for e in base.CollectEntities(deck, entity, "GRID"))
            rows[kind].append(row)
    return {"deck": deck, "database": base.DataBaseName(), "rows": rows,
        "references": {kind: [reference(e) for e in collect(kind)]
            for kind in ("__ALL_ENTITIES__", "__ELEMENTS__", "__PROPERTIES__", "__MATERIALS__")}}


def write_fixture(path):
    points = ((0,0,0),(1,0,0),(1,1,0),(0,1,0),(0,0,1),(1,0,1),(1,1,1),(0,1,1),
              (5,0,0),(6,0,0),(6,1,0),(5,1,0))
    faces = ((1101,1104,1103,1102),(1105,1106,1107,1108),(1101,1102,1106,1105),
             (1104,1108,1107,1103),(1101,1105,1108,1104),(1102,1103,1107,1106),(1109,1110,1111,1112))
    lines = ["SOL 101", "CEND", "BEGIN BULK", "MAT1,4101,210000.,,0.3,1.",
             "PSHELL,3101,4101,0.1,4101,,4101", "PSOLID,3102,4101"]
    lines.extend("GRID,%d,,%s,%s,%s" % (1101+i, *point) for i, point in enumerate(points))
    lines.extend("CQUAD4,%d,3101,%s" % (2101+i, ",".join(map(str, face))) for i, face in enumerate(faces))
    path.write_text("\n".join(lines + ["ENDDATA"]) + "\n", encoding="ascii")


def content(value):
    return {key: data for key, data in value.items() if key != "database"}


def row_map(rows):
    return {row["entity"]["id"]: row for row in rows}


def independent_mesh_checks(before, after):
    old, new = before["rows"], after["rows"]
    if old["SHELL"] != new["SHELL"] or old["PSHELL"] != new["PSHELL"] or old["MAT1"] != new["MAT1"]:
        raise AssertionError("Volume meshing changed source/decoy shells or property/material definitions")
    for left, right in zip(old["PSOLID"], new["PSOLID"]):
        without_count = lambda row: {**row, "fields": {key: value for key, value in row["fields"].items() if key != "Num.Elem"}}
        if without_count(left) != without_count(right):
            raise AssertionError("Volume meshing changed a PSOLID definition")
    old_grids, grids = row_map(old["GRID"]), row_map(new["GRID"])
    if any(grids.get(key) != row for key, row in old_grids.items()):
        raise AssertionError("Volume meshing changed original GRID cards")
    solids = new["SOLID"]
    if old["SOLID"] or len(solids) != 6:
        raise AssertionError("Expected six new PYRAMID elements from the verified default")
    coords = {entity_id: [row["fields"][name] for name in ("X1", "X2", "X3")] for entity_id, row in grids.items()}
    def tetra(a, b, c, d):
        u = [b[i]-a[i] for i in range(3)]
        v = [c[i]-a[i] for i in range(3)]
        w = [d[i]-a[i] for i in range(3)]
        return (u[0]*(v[1]*w[2]-v[2]*w[1])-u[1]*(v[0]*w[2]-v[2]*w[0])+u[2]*(v[0]*w[1]-v[1]*w[0]))/6
    volume = 0.
    for row in solids:
        values = row["fields"]
        if values["type"] != "PYRAMID" or values["PID"] != 3102:
            raise AssertionError("Unexpected new SOLID type or PSOLID")
        ids = [values[f"G{i}"] for i in range(1, 6)]
        if len(set(ids)) != 5 or sorted(ids) != row["node_ids"] or any(node not in coords for node in ids):
            raise AssertionError("Invalid independently read PYRAMID connectivity")
        a,b,c,d,apex = (coords[node] for node in ids)
        first, second = tetra(a,b,c,apex), tetra(a,c,d,apex)
        if not math.isfinite(first) or not math.isfinite(second) or min(first, second) <= 1e-12:
            raise AssertionError("Independent PYRAMID has non-positive signed volume")
        volume += first + second
    if not math.isclose(volume, 1., rel_tol=1e-9, abs_tol=0.):
        raise AssertionError("Independent pyramid volume sum is not 1 mm^3")
    new_ids = sorted(set(grids)-set(old_grids))
    used_ids = {node for row in solids for node in row["node_ids"]}
    if (len(new_ids) != 1 or new_ids[0] not in used_ids
            or any(type(value) not in (int, float) or not math.isfinite(value)
                   or not 0. < value < 1. for value in coords[new_ids[0]])):
        raise AssertionError("Independent added GRID is not a used interior point of the cube")
    old_volumes, new_volumes = old["VOLUME"], new["VOLUME"]
    if len(old_volumes) != 1 or len(new_volumes) != 1 or old_volumes[0]["entity"] != new_volumes[0]["entity"]:
        raise AssertionError("Independent VOLUME identity set changed")
    volume_row = new_volumes[0]
    values = volume_row["fields"]
    expected_volume = {"PID":3102,"Type":"Tetra Rapid","Order":"1st","Status":"Meshed",
        "Frozen":"NO","Tetras":0,"Pyramids":6,"Pentas":0,"Hexas":0,"Polyhedrals":0,
        "Grids":len(used_ids),"Internal grids":len(new_ids)}
    for name, expected in expected_volume.items():
        if type(values.get(name)) is not type(expected) or values[name] != expected:
            raise AssertionError("Independent VOLUME field differs: " + name)
    if volume_row["shell_ids"] != BOUNDARY_IDS or old_volumes[0]["shell_ids"] != BOUNDARY_IDS:
        raise AssertionError("Independent VOLUME boundary membership changed")
    if not math.isclose(values["Volume"],1.,rel_tol=1e-9,abs_tol=0.):
        raise AssertionError("Independent VOLUME size differs from the cube volume")
    for name in ("ID","Name","FROZEN_ID","FROZEN_DELETE","Comment"):
        if name not in old_volumes[0]["fields"] or name not in values or old_volumes[0]["fields"][name] != values[name]:
            raise AssertionError("Independent stable VOLUME field changed: " + name)
    def keys(references):
        return {(row["type"],row["id"]) for row in references}
    new_solid_keys = {(row["entity"]["type"],row["entity"]["id"]) for row in solids}
    if keys(after["references"]["__ALL_ENTITIES__"]) != keys(before["references"]["__ALL_ENTITIES__"]) | new_solid_keys | {("GRID",entity_id) for entity_id in new_ids}:
        raise AssertionError("Independent complete entity references changed outside new SOLID/GRID")
    if keys(after["references"]["__ELEMENTS__"]) != keys(before["references"]["__ELEMENTS__"]) | new_solid_keys:
        raise AssertionError("Independent FE references changed outside new SOLID")
    if before["references"]["__PROPERTIES__"] != after["references"]["__PROPERTIES__"] or before["references"]["__MATERIALS__"] != after["references"]["__MATERIALS__"]:
        raise AssertionError("Volume meshing added or removed properties/materials")
    return {"pyramid_count":6, "new_grid_ids":new_ids, "independent_volume_sum":volume,
            "source_and_decoy_shells_unchanged":True, "original_grids_unchanged":True}


async def verify(instructions, identity, helpers, run_dir, report, path):
    one, two, _ = helpers
    fixture_path = run_dir/"volume-mesh-fixture.nas"
    write_fixture(fixture_path)
    fixture_hash = hashlib.sha256(fixture_path.read_bytes()).hexdigest()
    parameters = StdioServerParameters(command=sys.executable,
        args=["-B","-m","ansa_mcp_plugin","--runtime-home",instructions["runtime_home"]],
        env={**os.environ,"PYTHONPATH":str(ROOT/"src"),"PYTHONDONTWRITEBYTECODE":"1"})
    receipts = []

    async def oracle(h):
        return await independent_script(h,remote_oracle,identity["pid"],identity["program_args"])

    def request(snapshot, backup_name):
        volume = snapshot["rows"]["VOLUME"][0]
        return {"deck":snapshot["deck"],"volume_id":volume["entity"]["id"],
            "expected_fields":{name:volume["fields"][name] for name in ("PID","Type","Order","Status","Frozen")},
            "expected_shell_ids":BOUNDARY_IDS,"backup_path":str(run_dir/backup_name)}

    with (run_dir/"volume-meshing-backend.stderr.log").open("w",encoding="utf-8") as log:
        async with stdio_client(parameters,errlog=log) as (reader,writer):
            async with ClientSession(reader,writer,read_timeout_seconds=timedelta(seconds=180)) as client:
                await client.initialize()
                h = common.McpHarness(client,report,run_dir)
                report["schema"] = await h.list_schema()
                await common._bind_for_run(h,identity,one,two,identity["port"])
                fixture = await independent_script(h,remote_fixture,identity["pid"],identity["program_args"],str(fixture_path),allow_model_change=True)
                await h.snapshot_context()
                before = await oracle(h)
                if (len(before["rows"]["GRID"]) != 13 or len(before["rows"]["SHELL"]) != 7
                        or len(before["rows"]["VOLUME"]) != 1 or before["rows"]["SOLID"]
                        or len(before["references"]["__ELEMENTS__"]) != 7):
                    raise AssertionError("Independent volume fixture identity counts differ")
                material = row_map(before["rows"]["MAT1"])[4101]["fields"]
                volume = before["rows"]["VOLUME"][0]
                if material["DEFINED"] != "YES" or volume["fields"]["PID"] != 3102 or volume["shell_ids"] != BOUNDARY_IDS:
                    raise AssertionError("Fixture must use its existing defined PSOLID/MAT1 and exact boundary")
                common._json_write(run_dir/"volume-meshing-independent-baseline.json",before)
                owned.record(report,path,"owned_closed_cube_existing_defined_property_boundary_and_schema",{"fixture":fixture,"independent":before,"fixture_sha256":fixture_hash})

                first_request = request(before,"first-volume-backup.ansa")
                rejected = []
                for index,change in enumerate(({"volume_id":2147483646}, {"expected_fields":{**first_request["expected_fields"],"PID":3101}},
                        {"expected_shell_ids":BOUNDARY_IDS[:-1]}, {"deck":2})):
                    invalid = {**first_request,**change,"backup_path":str(run_dir/f"rejected-{index}.ansa")}
                    _,failed = await h.task("ansa_mesh_volume",invalid,expected="failed")
                    if Path(invalid["backup_path"]).exists() or await oracle(h) != before:
                        raise AssertionError("Rejected volume task created a backup or changed the model")
                    rejected.append({"task_id":failed["task_id"],"error":failed["error"]})
                schema_rejections = ({"volume_id":True},{"volume_id":"1"},{"deck":True},{"expected_shell_ids":[]},
                    {"expected_shell_ids":[2101,2101]},{"fix":True},{"scope":"all"},{"backup_path":"relative.ansa"})
                for change in schema_rejections:
                    value = await client.call_tool("ansa_mesh_volume",{"operation_id":"invalid-"+uuid.uuid4().hex,
                        "expected_target":h.target,"expected_model":h.model,**first_request,**change})
                    if not value.isError:
                        raise AssertionError("Invalid volume arguments were accepted")
                if await oracle(h) != before:
                    raise AssertionError("Rejected volume inputs changed the model")
                owned.record(report,path,"strict_schema_stale_boundary_property_deck_and_no_effect_rejections",{"failed":rejected,"schema_rejections":len(schema_rejections)})

                async def mesh_once(arguments):
                    operation_id = "volume-mesh-"+uuid.uuid4().hex
                    submitted,completed = await h.task("ansa_mesh_volume",arguments,operation_id=operation_id)
                    result = completed["data"]["result"]
                    if result["scope_verified"] is not True or result["api_return_code"] != 1 or result["psolid_id"] != 3102 or result["mat1_id"] != 4101:
                        raise AssertionError("Volume task did not verify its scope and material ownership")
                    receipts.append({"request":arguments,"operation_id":operation_id,"task_id":submitted["task_id"],
                        "result":result,"target":completed["target"],"model":completed["model"]})
                    return result

                first = await mesh_once(first_request)
                after = await oracle(h)
                independent = independent_mesh_checks(before,after)
                backup = Path(first_request["backup_path"])
                backup_hash = hashlib.sha256(backup.read_bytes()).hexdigest()
                owned.record(report,path,"single_volume_native_pyramids_center_grid_volume_and_outside_preservation",{"result":first,"independent":independent,"backup_sha256":backup_hash})

                original = receipts[0]
                _,replayed = await h.task("ansa_mesh_volume",first_request,operation_id=original["operation_id"])
                conflict = await h.call("ansa_mesh_volume",{"operation_id":original["operation_id"],"expected_target":h.target,
                    "expected_model":h.model,**first_request,"backup_path":str(run_dir/"conflict.ansa")},allow_error=True)
                already = request(after,"already-meshed.ansa")
                _,failed = await h.task("ansa_mesh_volume",already,expected="failed")
                if (replayed["task_id"] != original["task_id"] or replayed["data"]["result"] != first
                        or conflict.get("ok") is not False or conflict.get("task_id") is not None
                        or Path(already["backup_path"]).exists() or await oracle(h) != after
                        or hashlib.sha256(backup.read_bytes()).hexdigest() != backup_hash):
                    raise AssertionError("Repeated/conflicting/already-meshed request changed the original mesh")
                owned.record(report,path,"original_receipt_conflict_and_already_meshed_guard_without_replay",{"original_task_id":original["task_id"],"already_meshed_error":failed["error"]})

                await h.direct_result("ansa_model_open",{"path":str(backup),"backup_path":str(run_dir/"before-backup-restore.ansa")})
                await h.snapshot_context()
                restored = await oracle(h)
                if content(restored) != content(before):
                    raise AssertionError("Actual volume backup restore did not recover the unmeshed fixture")
                second_request = request(restored,"second-volume-backup.ansa")
                second = await mesh_once(second_request)
                current = await oracle(h)
                second_independent = independent_mesh_checks(restored,current)
                saved = run_dir/"accepted-volume-mesh.ansa"
                await h.direct_result("ansa_model_save",{"path":str(saved)})
                saved_hash = hashlib.sha256(saved.read_bytes()).hexdigest()
                current = await oracle(h)
                for index in (1,2):
                    old_target,old_model = dict(h.target),dict(h.model)
                    await h.direct_result("ansa_model_open",{"path":str(saved),"backup_path":str(run_dir/f"volume-reopen-{index}-backup.ansa")})
                    await h.snapshot_context()
                    reopened = await oracle(h)
                    if h.model["epoch"] == old_model["epoch"] or content(reopened) != content(current):
                        raise AssertionError("Saved volume mesh readback or model epoch differs after reopen")
                    current = reopened
                    stale = await h.call("ansa_mesh_volume",{"operation_id":"stale-"+uuid.uuid4().hex,
                        "expected_target":old_target,"expected_model":old_model,**second_request},allow_error=True)
                    if stale.get("ok") is not False or stale.get("task_id") is not None:
                        raise AssertionError("Stale volume model context accepted")
                if hashlib.sha256(saved.read_bytes()).hexdigest() != saved_hash or hashlib.sha256(fixture_path.read_bytes()).hexdigest() != fixture_hash:
                    raise AssertionError("Volume task modified its input or saved file")
                previous_backend = h.target["backend_id"]
                owned.record(report,path,"actual_backup_restore_second_mesh_saved_reopens_and_stale_context",{"second_result":second,"independent":second_independent,"saved":str(saved),"sha256":saved_hash})

    with (run_dir/"volume-meshing-restarted-backend.stderr.log").open("w",encoding="utf-8") as log:
        async with stdio_client(parameters,errlog=log) as (reader,writer):
            async with ClientSession(reader,writer,read_timeout_seconds=timedelta(seconds=180)) as client:
                await client.initialize()
                h = common.McpHarness(client,report,run_dir)
                status = await h.call("ansa_connection_status")
                if status["data"]["backend_id"] == previous_backend:
                    raise AssertionError("Backend did not restart")
                for receipt in receipts:
                    value = await h.call("ansa_task_query",{"operation_id":receipt["operation_id"]})
                    if any(value[key] != receipt[key] for key in ("task_id","target","model")) or value["data"]["result"] != receipt["result"]:
                        raise AssertionError("Persistent volume mesh receipt changed")
                await common._bind_for_run(h,identity,one,two,identity["port"])
                if await oracle(h) != current or hashlib.sha256(saved.read_bytes()).hexdigest() != saved_hash:
                    raise AssertionError("Original receipt query changed the model or saved mesh")
                owned.record(report,path,"backend_restart_original_mesh_receipts_and_independent_no_replay",{"original_task_ids":[receipt["task_id"] for receipt in receipts],"unchanged":True})


if __name__ == "__main__":
    raise SystemExit(owned.main(report_name="volume-meshing-report.json",
        scope="Existing NASTRAN closed six-quad box VOLUME; guarded TETRA RAPID, actual PYRAMID readback and backup recovery; no canvas acceptance",verify_callback=verify))
