"""Owned native connection checks, with independent point and FE readback."""

import hashlib
import math
import os
from pathlib import Path
import sys
from datetime import timedelta
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
        raise RuntimeError("Owned identity changed before connection-check fixture")
    deck = constants.NASTRAN
    if base.CollectEntities(deck, None, "__ELEMENTS__"):
        raise RuntimeError("Connection-check fixture requires no preexisting FE")
    if base.InputNastran(fixture_path, nodes_id="nooffset", elements_id="nooffset",
            properties_id="nooffset", materials_id="nooffset", header="skip", model_action="merge_model") != 1:
        raise RuntimeError("Connection-check FE input failed")
    parts = []
    for element_id, module_id in ((101, "check-shell-a"), (102, "check-shell-b")):
        shell = base.GetEntity(deck, "SHELL", element_id)
        part = base.NewPart("connection check " + module_id, module_id)
        if shell is None or part is None or base.SetEntityPart(shell, part) != 1:
            raise RuntimeError("Connection-check Part assignment failed")
        if [item._id for item in base.CollectEntities(deck, part, "SHELL")] != [element_id]:
            raise RuntimeError("Connection-check Part assignment did not read back")
        parts.append({"deck": deck, "type": part.ansa_type(deck), "id": part._id})
    return {"parts": parts}


def remote_oracle(expected_pid, expected_args):
    import os, threading, math
    from ansa import base, constants, connections, session
    if (os.getpid() != expected_pid or list(session.ProgramArguments()) != expected_args
            or threading.current_thread() is not threading.main_thread()
            or base.CurrentDeck() != constants.NASTRAN):
        raise RuntimeError("Owned identity changed before independent connection-check readback")
    deck = constants.NASTRAN
    def ref(entity):return {"deck": deck, "type": entity.ansa_type(deck), "id": entity._id}
    rows = {}
    for kind, fields in (("GRID", ("CP", "X1", "X2", "X3", "CD")),
                         ("SHELL", ("type", "PID", "G1", "G2", "G3", "G4")),
                         ("PSHELL", ("MID1", "T")), ("MAT1", ("E", "NU", "RHO")),
                         ("SpotweldPoint_Type", ("Name", "X", "Y", "Z", "D", "P1", "P2", "P3", "Status", "Error Class", "FE Rep Type"))):
        rows[kind] = []
        for entity in sorted(base.CollectEntities(deck, None, kind), key=lambda item: item._id):
            labels = entity.card_fields(deck)
            requested = tuple(name for name in fields if name in labels)
            values = base.GetEntityCardValues(deck, entity, requested)
            if type(values) is not dict or set(values) != set(requested):
                raise RuntimeError("Independent oracle received incomplete card values")
            row = {"entity": ref(entity), "card": values}
            if kind in ("GRID", "SpotweldPoint_Type"):
                position = list(entity.position)
                if len(position) != 3 or any(type(x) not in (int, float) or not math.isfinite(x) for x in position):
                    raise RuntimeError("Independent oracle position is incomplete")
                row["position"] = position
            if kind == "SpotweldPoint_Type":
                row["parts"] = [[ref(part) for part in connections.ConnectivityStringToEnts(values[field])]
                                for field in ("P1", "P2")]
            rows[kind].append(row)
    return {"deck": deck, "database": base.DataBaseName(), "rows": rows,
            "element_ids": sorted(item._id for item in base.CollectEntities(deck, None, "__ELEMENTS__")),
            "connection_ids": sorted(item._id for item in base.CollectEntities(deck, None, "SpotweldPoint_Type"))}


async def verify(instructions, identity, helpers, run_dir, report, path):
    one, two, _ = helpers
    fixture_path = run_dir / "connection-checks.nas"
    fixture_path.write_text("BEGIN BULK\nMAT1,101,210000.,,0.3,7.85E-9\n"
        "PSHELL,101,101,1.\nPSHELL,102,101,1.\n"
        "GRID,101,,0.,0.,0.\nGRID,102,,10.,0.,0.\nGRID,103,,10.,10.,0.\nGRID,104,,0.,10.,0.\n"
        "GRID,105,,0.,0.,2.\nGRID,106,,10.,0.,2.\nGRID,107,,10.,10.,2.\nGRID,108,,0.,10.,2.\n"
        "CQUAD4,101,101,101,102,103,104\nCQUAD4,102,102,105,106,107,108\nENDDATA\n", encoding="ascii")
    parameters = StdioServerParameters(command=sys.executable,
        args=["-B", "-m", "ansa_mcp_plugin", "--runtime-home", instructions["runtime_home"]],
        env={**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"})
    receipts = []
    async def oracle(h):
        return await independent_script(h, remote_oracle, identity["pid"], identity["program_args"])
    async def check(h, point_ids, expected_ids, distance=0.1):
        request = {"deck": baseline["deck"], "point_ids": point_ids, "distance": distance}
        operation_id = "connection-check-" + uuid.uuid4().hex
        submitted, completed = await h.task("ansa_connection_check", request, operation_id=operation_id)
        result = completed["data"]["result"]
        if result["failed_point_ids"] != sorted(expected_ids):
            raise AssertionError(f"Connection check scope mismatch: {result}")
        if await oracle(h) != baseline:
            raise AssertionError("Connection check changed point definitions, FE or coordinates")
        receipt = {"request": request, "operation_id": operation_id, "task_id": submitted["task_id"],
                   "result": result, "target": completed["target"], "model": completed["model"]}
        receipts.append(receipt)
        return result
    with (run_dir / "connection-checks-backend.stderr.log").open("w", encoding="utf-8") as log:
        async with stdio_client(parameters, errlog=log) as (reader, writer):
            async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=120)) as client:
                await client.initialize()
                h = common.McpHarness(client, report, run_dir)
                report["schema"] = await h.list_schema()
                await common._bind_for_run(h, identity, one, two, identity["port"])
                fixture = await independent_script(h, remote_fixture, identity["pid"], identity["program_args"], str(fixture_path), allow_model_change=True)
                await h.snapshot_context()
                positions = ([5., 5., 1.], [5.001, 5., 1.], [8., 8., 1.], [100., 100., 1.])
                for index, position in enumerate(positions):
                    await h.direct_result("ansa_connection_create", {"position": position, "parts": fixture["parts"],
                        "diameter": 3., "connection_type": "SpotweldPoint_Type", "entity_id": 100000 + index})
                baseline = await oracle(h)
                for index, row in enumerate(baseline["rows"]["SpotweldPoint_Type"]):
                    if row["entity"]["id"] != 100000 + index or row["position"] != list(positions[index]) or row["parts"] != [[fixture["parts"][0]], [fixture["parts"][1]]]:
                        raise AssertionError("Independent fixture identity, coordinates or connectivity differ")
                if not math.isclose(math.dist(positions[0], positions[1]), .001, abs_tol=1e-12):
                    raise AssertionError("Independent duplicate distance differs")
                common._json_write(run_dir / "connection-checks-independent-baseline.json", baseline)
                owned.record(report, path, "owned_fixture_point_part_and_distance_readback", {"fixture": fixture, "independent": baseline, "duplicate_distance": .001})

                positive = await check(h, [100000, 100001, 100002], [100000, 100001])
                if positive["issue_counts"]["error"] != 1:
                    raise AssertionError("Expected exactly one duplicate-connection leaf issue")
                clean = await check(h, [100000, 100002], [])
                strict_distance = await check(h, [100000, 100001], [], .0005)
                single = await check(h, [100000], [])
                if any(result["issue_counts"]["error"] or result["issue_counts"]["warning"] for result in (clean, strict_distance, single)):
                    raise AssertionError("Clean selected scopes reported unexpected issues")
                owned.record(report, path, "duplicate_positive_clean_single_and_distance_threshold_scopes", {"positive": positive, "clean": clean, "strict_distance": strict_distance, "single": single})

                projection = await check(h, [100003], [100003])
                mixed = await check(h, [100000, 100001, 100002, 100003], [100000, 100001, 100003])
                if projection["issue_counts"]["error"] != 1 or mixed["issue_counts"]["error"] != 2:
                    raise AssertionError("Native projection precheck or leaf accounting differs")
                owned.record(report, path, "native_projection_precheck_and_mixed_report_leaf_accounting", {"projection": projection, "mixed": mixed})

                failed = []
                for change in ({"point_ids": [2147483646]}, {"deck": 2}):
                    _, value = await h.task("ansa_connection_check", {**receipts[0]["request"], **change}, expected="failed")
                    failed.append({"task_id": value["task_id"], "error": value["error"]})
                invalid = ({"point_ids": [True]}, {"point_ids": ["100000"]}, {"point_ids": []},
                           {"point_ids": [100000, 100000]}, {"distance": 0}, {"distance": "0.1"},
                           {"distance": True}, {"fix": True}, {"scope": "all"}, {"deck": "1"})
                for change in invalid:
                    response = await client.call_tool("ansa_connection_check", {"operation_id": "rejected-" + uuid.uuid4().hex,
                        "expected_target": h.target, "expected_model": h.model, **receipts[0]["request"], **change})
                    if not response.isError:raise AssertionError(f"Invalid connection-check argument accepted: {change}")
                first = receipts[0]
                _, replay = await h.task("ansa_connection_check", first["request"], operation_id=first["operation_id"])
                if replay["task_id"] != first["task_id"] or replay["data"]["result"] != first["result"]:
                    raise AssertionError("Original check receipt was not replayed")
                conflict = await h.call("ansa_connection_check", {"operation_id": first["operation_id"],
                    "expected_target": h.target, "expected_model": h.model, **first["request"], "distance": .2}, allow_error=True)
                if conflict.get("ok") is not False or conflict.get("task_id") is not None or await oracle(h) != baseline:
                    raise AssertionError("Rejected or duplicate check changed the model")
                owned.record(report, path, "strict_inputs_preflight_original_receipt_and_conflict_without_effects", {"failed_tasks": failed, "schema_rejections": len(invalid), "same_task_id": first["task_id"]})

                saved = run_dir / "accepted-connection-checks.ansa"
                await h.direct_result("ansa_model_save", {"path": str(saved)})
                saved_hash = hashlib.sha256(saved.read_bytes()).hexdigest()
                for index in (1, 2):
                    old_target, old_model = dict(h.target), dict(h.model)
                    await h.direct_result("ansa_model_open", {"path": str(saved), "backup_path": str(run_dir / f"checks-reopen-{index}-backup.ansa")})
                    await h.snapshot_context()
                    if h.model["epoch"] == old_model["epoch"]:raise AssertionError("Open did not change model epoch")
                    reopened = await oracle(h)
                    if reopened["rows"] != baseline["rows"] or reopened["element_ids"] != baseline["element_ids"]:
                        raise AssertionError("Independent point/FE data differs after save/reopen")
                    baseline = reopened
                    await check(h, [100000, 100001, 100002], [100000, 100001])
                    stale = await h.call("ansa_connection_check", {"operation_id": "stale-" + uuid.uuid4().hex,
                        "expected_target": old_target, "expected_model": old_model, **first["request"]}, allow_error=True)
                    if stale.get("ok") is not False or stale.get("task_id") is not None:raise AssertionError("Stale check context accepted")
                if hashlib.sha256(saved.read_bytes()).hexdigest() != saved_hash:raise AssertionError("Check modified saved fixture")
                previous_backend = h.target["backend_id"]
                owned.record(report, path, "save_same_path_reopen_scoped_checks_and_stale_context", {"saved": str(saved), "sha256": saved_hash, "independent": baseline})
    with (run_dir / "connection-checks-restarted-backend.stderr.log").open("w", encoding="utf-8") as log:
        async with stdio_client(parameters, errlog=log) as (reader, writer):
            async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=120)) as client:
                await client.initialize()
                h = common.McpHarness(client, report, run_dir)
                status = await h.call("ansa_connection_status")
                if status["data"]["backend_id"] == previous_backend:raise AssertionError("Backend did not restart")
                for receipt in receipts:
                    queried = await h.call("ansa_task_query", {"operation_id": receipt["operation_id"]})
                    if any(queried[key] != receipt[key] for key in ("task_id", "target", "model")) or queried["data"]["result"] != receipt["result"]:
                        raise AssertionError("Persisted check receipt differs after restart")
                await common._bind_for_run(h, identity, one, two, identity["port"])
                if await oracle(h) != baseline:raise AssertionError("Original-task queries changed model data")
                owned.record(report, path, "backend_restart_original_check_receipts_and_independent_unchanged_model", {"original_task_ids": [x["task_id"] for x in receipts], "unchanged": True})


if __name__ == "__main__":
    raise SystemExit(owned.main(report_name="connection-checks-report.json",
        scope="NASTRAN explicit Spotweld connection checks; native projection and duplicate issues; no fixes or canvas acceptance", verify_callback=verify))
