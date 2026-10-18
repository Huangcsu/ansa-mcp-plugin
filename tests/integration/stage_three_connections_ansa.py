"""Owned ANSA acceptance: six point definitions and bounded connection inventory."""

from datetime import timedelta
import hashlib
import inspect
import os
from pathlib import Path
import sys
import uuid

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import stage_three_ansa as common
import stage_three_api_catalog_ansa as owned


POINT_TYPES = ("SpotweldPoint_Type", "Bolt_Type", "GumDrop_Type", "Rivet_Type", "Screw_Type", "Clip_Type")


def remote_connector_fixture(expected_pid, expected_arguments, fixture_path):
    import os
    import threading
    from ansa import base, connections, constants, session

    if (os.getpid() != expected_pid or list(session.ProgramArguments()) != expected_arguments
            or threading.current_thread() is not threading.main_thread()
            or base.CurrentDeck() != constants.NASTRAN):
        raise RuntimeError("Owned identity changed before connector fixture creation")
    deck = constants.NASTRAN

    def ref(entity):
        return {"deck": deck, "type": entity.ansa_type(deck), "id": entity._id}

    before = {item._id for item in base.CollectEntities(deck, None, "CONNECTOR_ENTITY")}
    if base.CollectEntities(deck, None, "RBE2"):
        raise RuntimeError("Connector fixture requires no preexisting rigid elements")
    status = base.InputNastran(fixture_path, nodes_id="nooffset", elements_id="nooffset",
                              header="skip", model_action="merge_model")
    if type(status) is not int or status != 1:
        raise RuntimeError("Owned connector fixture input failed")
    for index, rigid_id in enumerate((2001, 2002)):
        rigid = base.GetEntity(deck, "RBE2", rigid_id)
        part = base.NewPart("connector fixture part " + str(index), "connector-owned-" + str(index))
        if rigid is None or part is None or base.SetEntityPart(rigid, part) != 1:
            raise RuntimeError("Owned connector rigid Part assignment failed")
        if [item._id for item in base.CollectEntities(deck, part, "RBE2")] != [rigid_id]:
            raise RuntimeError("Owned connector rigid Part assignment did not read back")
    points = []
    for node_id, position in ((1001, [30.0, 0.0, 0.0]), (1003, [31.0, 0.0, 0.0])):
        created = connections.CreateInterfacePoint(entities=None, convert_type="Node", node_id=node_id)
        if type(created) not in (list, tuple) or len(created) != 1 or created[0].ansa_type(deck) != "A_POINT":
            raise RuntimeError("Native A_POINT fixture creation failed")
        if list(created[0].position) != position:
            raise RuntimeError("Native A_POINT coordinates did not read back")
        points.append(created[0])
    created = connections.CreateConnectorFromInterfacePoints(
        interface_points=points, match_only_identical_names=False, matching_proximity=5.0,
        create_external_connectors_only=False, representation="CBUSH", max_number_of_connected_points=2,
        settings={}, template_id=0)
    if type(created) is not dict or len(created) != 1:
        raise RuntimeError("Native connector fixture creation did not return one connector")
    entity, sources = next(iter(created.items()))
    if entity.ansa_type(deck) != "CONNECTOR_ENTITY" or {item._id for item in sources} != {item._id for item in points}:
        raise RuntimeError("Native connector fixture identity or source points differ")
    after = {item._id for item in base.CollectEntities(deck, None, "CONNECTOR_ENTITY")}
    if after - before != {entity._id} or before - after:
        raise RuntimeError("Independent connector fixture range differs")
    fresh = base.GetEntity(deck, "CONNECTOR_ENTITY", entity._id)
    if fresh is None or fresh.ansa_type(deck) != "CONNECTOR_ENTITY":
        raise RuntimeError("Native connector fixture cannot be freshly resolved")
    return {"connector": ref(fresh), "position": list(fresh.position), "card_fields": list(fresh.card_fields(deck)),
            "source_points": [ref(point) for point in points]}


def remote_connection_oracle(expected_pid, expected_arguments):
    import json
    import math
    import os
    from pathlib import Path
    import threading
    from ansa import base, connections, constants, session

    if (os.getpid() != expected_pid or list(session.ProgramArguments()) != expected_arguments
            or threading.current_thread() is not threading.main_thread()):
        raise RuntimeError("Owned process identity changed before connection readback")
    deck = base.CurrentDeck()
    if deck != constants.NASTRAN:
        raise RuntimeError("Connection fixture requires NASTRAN")
    trace = Path(expected_arguments[expected_arguments.index("-translfp") + 1]).parent / "connections-oracle-progress.jsonl"

    def mark(step, **fields):
        with trace.open("a", encoding="utf-8") as log:
            log.write(json.dumps({"step": step, **fields}) + "\n")

    kinds = ("CONNECTOR_ENTITY", "SpotweldPoint_Type", "Bolt_Type", "GumDrop_Type", "Rivet_Type", "Screw_Type", "Clip_Type")

    def collect(kind):
        mark("CollectEntities", kind=kind)
        entities = base.CollectEntities(deck, None, kind, False, filter_visible=False, hidden_entities=True)
        if type(entities) not in (list, tuple):
            raise RuntimeError("Unavailable independent collection: " + kind)
        return entities

    def ref(entity):
        return {"deck": deck, "type": entity.ansa_type(deck), "id": entity._id}

    rows = []
    for kind in kinds:
        for entity in collect(kind):
            mark("fresh_collection_entity", kind=kind, entity_id=entity._id)
            fresh = entity
            if fresh is None or fresh.ansa_type(deck) != kind:
                raise RuntimeError("Independent connection identity cannot be resolved")
            mark("card_fields", kind=kind, entity_id=entity._id)
            labels = fresh.card_fields(deck)
            if type(labels) not in (list, tuple):
                raise RuntimeError("Independent connection card fields unavailable")
            requested = [field for field in ("Name", "D", "X", "Y", "Z", *("P" + str(i) for i in range(1, 11))) if field in labels]
            mark("GetEntityCardValues", kind=kind, entity_id=entity._id, fields=requested)
            values = base.GetEntityCardValues(deck, fresh, tuple(requested)) if requested else {}
            if type(values) is not dict or set(values) != set(requested):
                raise RuntimeError("Independent card read is incomplete")
            mark("Entity.position", kind=kind, entity_id=entity._id)
            position = list(fresh.position)
            if len(position) != 3 or any(type(n) not in (int, float) or not math.isfinite(n) for n in position):
                raise RuntimeError("Independent connection position is invalid")
            links = []
            for field in requested:
                if not field.startswith("P") or not field[1:].isdigit():
                    continue
                text = values[field]
                if text is None or text == "":
                    continue
                if type(text) is not str:
                    raise RuntimeError("Independent connection field is not text")
                mark("ConnectivityStringToEnts", kind=kind, entity_id=entity._id, field=field)
                entities = connections.ConnectivityStringToEnts(text)
                if type(entities) not in (list, tuple):
                    raise RuntimeError("Independent connectivity cannot be resolved")
                links.append({"field": field, "text": text, "entities": [ref(item) for item in entities]})
            rows.append({"entity": ref(fresh), "position": position, "name": values.get("Name"),
                         "diameter": values.get("D"), "connectivity": links, "card": values})
    rows.sort(key=lambda row: (row["entity"]["type"], row["entity"]["id"]))
    return {"pid": os.getpid(), "main_thread": True, "version": str(constants.app_version),
            "deck": deck, "alternate_deck": constants.ABAQUS, "database": base.DataBaseName(),
            "connections": rows, "parts": sorted([ref(item) for item in collect("ANSAPART")], key=lambda item: item["id"]),
            "fe_counts": {kind: len(collect(kind)) for kind in ("GRID", "SHELL", "RBE2")}}


def assert_row(actual, independent):
    if actual["complete"] is not True or actual.get("errors"):
        raise AssertionError(f"Incomplete connection row: {actual}")
    for key in ("entity", "position", "name", "diameter", "connectivity"):
        if actual[key] != independent[key]:
            raise AssertionError(f"Independent connection mismatch for {key}: {actual[key]} != {independent[key]}")


async def independent_script(harness, function, *arguments, allow_model_change=False):
    source = inspect.getsource(function) + "\nRESULT = " + function.__name__ + "(" + ", ".join(repr(value) for value in arguments) + ")\n"
    value = await harness.direct_result("ansa_script_execute", {"script": source, "allow_model_change": allow_model_change})
    if value.get("main_thread") is not True or value.get("executor", {}).get("pid") != arguments[0]:
        raise AssertionError("Independent script did not execute in the owned ANSA main thread")
    result = value.get("result")
    if type(result) is not dict:
        raise AssertionError("Independent script did not return its JSON evidence")
    return result


async def verify(instructions, identity, helpers, run_dir, report, path):
    one, two, _ = helpers

    async def oracle(harness):
        return await independent_script(harness, remote_connection_oracle, identity["pid"], identity["program_args"])

    parameters = StdioServerParameters(command=sys.executable,
        args=["-B", "-m", "ansa_mcp_plugin", "--runtime-home", instructions["runtime_home"]],
        env={**os.environ, "PYTHONPATH": str(common.PROJECT_ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"})
    receipts = []
    with (run_dir / "connections-backend.stderr.log").open("w", encoding="utf-8") as log:
        async with stdio_client(parameters, errlog=log) as (reader, writer):
            async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=60)) as client:
                await client.initialize()
                h = common.McpHarness(client, report, run_dir)
                report["schema"] = await h.list_schema()
                await common._bind_for_run(h, identity, one, two, identity["port"])
                baseline = await oracle(h)
                empty = await h.direct_result("ansa_connection_list", {})
                if (baseline["connections"] or empty["count"] != 0 or empty["connections"]
                        or empty["complete"] is not True or empty["has_more"] is not False):
                    raise AssertionError("Owned seed connection inventory is not empty and complete")
                if await oracle(h) != baseline:
                    raise AssertionError("Empty inventory changed the model")
                owned.record(report, path, "empty_inventory_schema_and_owned_baseline", {"schema": report["schema"], "inventory": empty, "independent": baseline})

                fixture_path = run_dir / "connector-parts.nas"
                fixture_path.write_text(
                    "BEGIN BULK\nGRID,1001,,30.,0.,0.\nGRID,1002,,30.,1.,0.\n"
                    "GRID,1003,,31.,0.,0.\nGRID,1004,,31.,1.,0.\n"
                    "RBE2,2001,1001,123456,1002\nRBE2,2002,1003,123456,1004\nENDDATA\n", encoding="ascii")
                fixture = await independent_script(h, remote_connector_fixture, identity["pid"], identity["program_args"], str(fixture_path), allow_model_change=True)
                await h.snapshot_context()
                common._json_write(run_dir / "connections-connector-fixture.json", fixture)
                point_baseline = await oracle(h)
                positive_connector = await h.direct_result("ansa_connection_list", {"kind": "connector"})
                if positive_connector["count"] != 1 or positive_connector["complete"] is not True:
                    raise AssertionError(f"Positive CONNECTOR_ENTITY inventory failed: {positive_connector}")
                independent_connector = next(row for row in point_baseline["connections"] if row["entity"] == fixture["connector"])
                assert_row(positive_connector["connections"][0], independent_connector)

                parts = [await h.direct_result("ansa_part_create", {"name": "connection fixture " + value,
                         "module_id": "connection-fixture-" + value}) for value in ("a", "b")]
                part_refs = [part["entity"] for part in parts]
                for index, kind in enumerate(POINT_TYPES):
                    fields = {"position": [float(index * 2), 1.25, -0.5], "parts": part_refs,
                              "diameter": 3.0 + index, "connection_type": kind,
                              "entity_id": 100100 + index, "name": "accepted connection " + str(index)}
                    operation_id = "connection-create-" + uuid.uuid4().hex
                    submitted, completed = await h.task("ansa_connection_create", fields, operation_id=operation_id)
                    result = completed["data"]["result"]
                    independent = await oracle(h)
                    expected_ref = {"deck": baseline["deck"], "type": kind, "id": 100100 + index}
                    if (result["created"] != expected_ref or result["added"] != [expected_ref]
                            or result["removed"] or len(independent["connections"]) != index + 2):
                        raise AssertionError(f"Creation scope or identity mismatch: {result}")
                    row = next(item for item in independent["connections"] if item["entity"] == expected_ref)
                    assert_row(result["connection"], row)
                    if (row["position"] != fields["position"] or row["diameter"] != fields["diameter"]
                            or row["name"] != fields["name"] or [link["entities"] for link in row["connectivity"]] != [[part_refs[0]], [part_refs[1]]]
                            or independent["fe_counts"] != point_baseline["fe_counts"]):
                        raise AssertionError("Created point dimensions, connectivity or FE scope differ")
                    receipts.append({"operation_id": operation_id, "task_id": submitted["task_id"],
                                     "request": fields, "result": result, "target": completed["target"], "model": completed["model"]})
                created_oracle = await oracle(h)
                common._json_write(run_dir / "connections-created-oracle.json", created_oracle)
                owned.record(report, path, "six_point_types_coordinates_diameter_name_and_part_readback", {"receipts": list(receipts), "independent": created_oracle})

                auto_fields = {"position": [40.0, 1.25, -0.5], "diameter": 2.5, "parts": part_refs}
                auto_operation = "connection-auto-after-connector-" + uuid.uuid4().hex
                auto_submitted, auto_completed = await h.task("ansa_connection_create", auto_fields, operation_id=auto_operation)
                auto_result = auto_completed["data"]["result"]
                auto_reference = auto_result["created"]
                after_auto = await oracle(h)
                if (auto_reference["type"] != "Bolt_Type" or type(auto_reference["id"]) is not int or auto_reference["id"] <= 0
                        or auto_result["added"] != [auto_reference] or auto_result["removed"]
                        or after_auto["fe_counts"] != created_oracle["fe_counts"]):
                    raise AssertionError("Default point creation in the mixed model failed scope verification")
                auto_row = next(row for row in after_auto["connections"] if row["entity"] == auto_reference)
                assert_row(auto_result["connection"], auto_row)
                if auto_row["diameter"] != 2.5 or auto_row["position"] != auto_fields["position"]:
                    raise AssertionError("Default point dimensions did not read back")
                receipts.append({"operation_id": auto_operation, "task_id": auto_submitted["task_id"], "request": auto_fields,
                    "result": auto_result, "target": auto_completed["target"], "model": auto_completed["model"]})
                created_oracle = after_auto
                owned.record(report, path, "positive_connector_and_default_point_creation_in_mixed_model", {
                    "fixture": fixture, "inventory": positive_connector, "independent": independent_connector, "auto_point": auto_result})

                all_rows = await h.direct_result("ansa_connection_list", {})
                points = await h.direct_result("ansa_connection_list", {"kind": "point"})
                connectors = await h.direct_result("ansa_connection_list", {"kind": "connector"})
                pages = [await h.direct_result("ansa_connection_list", {"kind": "point", "offset": offset, "limit": 2}) for offset in (0, 2, 4, 6, 8)]
                if (all_rows["count"] != 8 or points["count"] != 7 or connectors["count"] != 1
                        or not all(row["complete"] for row in (all_rows, points, connectors, *pages))
                        or [row["entity"] for page in pages for row in page["connections"]]
                        != [row["entity"] for row in created_oracle["connections"] if row["entity"]["type"] != "CONNECTOR_ENTITY"]
                        or [page["has_more"] for page in pages] != [True, True, True, False, False]):
                    raise AssertionError("Stable pagination or connector/point classification mismatch")
                for actual, independent in zip(all_rows["connections"], created_oracle["connections"], strict=True):
                    assert_row(actual, independent)
                if await oracle(h) != created_oracle:
                    raise AssertionError("Connection inventory changed cards or visibility-independent membership")
                owned.record(report, path, "typed_inventory_stable_pagination_and_read_only_scope", {"all": all_rows, "points": points, "connectors": connectors, "pages": pages})

                invalid_schema = ({"position": [True, 0, 0]}, {"diameter": "3"}, {"entity_id": "0"},
                                  {"parts": "[]"}, {"card_values": {"D": 3}}, {"deck": "null"})
                for change in invalid_schema:
                    response = await client.call_tool("ansa_connection_create", {
                        "operation_id": "connection-invalid-" + uuid.uuid4().hex,
                        "expected_target": h.target, "expected_model": h.model, **receipts[0]["request"], **change})
                    if not response.isError:
                        raise AssertionError(f"Strict MCP argument unexpectedly accepted: {change}")
                rejected = []
                for change in ({"parts": [part_refs[0], part_refs[0]]},
                               {"parts": [part_refs[0], {**part_refs[1], "id": 2147483646}]},
                               {"deck": baseline["alternate_deck"]}):
                    _, failed = await h.task("ansa_connection_create", {**receipts[0]["request"], **change}, expected="failed")
                    rejected.append({"task_id": failed["task_id"], "error": failed["error"]})
                # The original ID remains occupied even for another point type.
                _, occupied = await h.task("ansa_connection_create", {**receipts[0]["request"], "connection_type": "Bolt_Type"}, expected="failed")
                original = receipts[0]
                _, replay = await h.task("ansa_connection_create", original["request"], operation_id=original["operation_id"])
                if replay["task_id"] != original["task_id"] or replay["data"]["result"] != original["result"]:
                    raise AssertionError("Connection creation did not replay the durable original result")
                conflict = await h.call("ansa_connection_create", {"operation_id": original["operation_id"],
                    "expected_target": h.target, "expected_model": h.model,
                    **original["request"], "diameter": 99}, allow_error=True)
                if conflict.get("ok") is not False or conflict.get("task_id") is not None or await oracle(h) != created_oracle:
                    raise AssertionError("Rejected or duplicate creation changed the model")
                owned.record(report, path, "strict_preflight_occupied_id_and_idempotent_creation_without_extra_effects", {"schema_rejections": len(invalid_schema), "failed": rejected, "occupied_id_task": occupied["task_id"], "original_task_id": original["task_id"], "same_task_replayed": True, "conflict_rejected": True})

                saved = run_dir / "accepted-connections.ansa"
                await h.direct_result("ansa_model_save", {"path": str(saved)})
                if not saved.is_file() or saved.stat().st_size == 0 or await oracle(h) != created_oracle:
                    raise AssertionError("Save copy failed or changed the current model")
                saved_hash = hashlib.sha256(saved.read_bytes()).hexdigest()
                for index in (1, 2):
                    old_target, old_model = dict(h.target), dict(h.model)
                    await h.direct_result("ansa_model_open", {"path": str(saved), "backup_path": str(run_dir / f"connections-open-{index}-backup.ansa")})
                    await h.snapshot_context()
                    if h.model["epoch"] == old_model["epoch"]:
                        raise AssertionError("Open did not replace the model epoch")
                    reopened = await oracle(h)
                    if (reopened["connections"] != created_oracle["connections"] or reopened["parts"] != created_oracle["parts"]
                            or reopened["fe_counts"] != created_oracle["fe_counts"]
                            or Path(reopened["database"]).resolve() != saved.resolve()):
                        raise AssertionError("Saved connection definitions did not survive independent reopen")
                    stale = await h.call("ansa_connection_create", {"operation_id": "connection-stale-" + uuid.uuid4().hex,
                        "expected_target": old_target, "expected_model": old_model, **receipts[0]["request"]}, allow_error=True)
                    if stale.get("ok") is not False or stale.get("task_id") is not None:
                        raise AssertionError("Stale connection write context was accepted")
                reopened_list = await h.direct_result("ansa_connection_list", {})
                for actual, independent in zip(reopened_list["connections"], reopened["connections"], strict=True):
                    assert_row(actual, independent)
                if hashlib.sha256(saved.read_bytes()).hexdigest() != saved_hash:
                    raise AssertionError("Open modified the saved fixture")
                final_oracle = await oracle(h)
                prior_backend = h.target["backend_id"]
                owned.record(report, path, "save_copy_same_path_reopen_and_stale_write_rejection", {"saved": str(saved), "sha256": saved_hash, "independent": final_oracle, "inventory": reopened_list})

    with (run_dir / "connections-restarted-backend.stderr.log").open("w", encoding="utf-8") as log:
        async with stdio_client(parameters, errlog=log) as (reader, writer):
            async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=60)) as client:
                await client.initialize()
                h = common.McpHarness(client, report, run_dir)
                status = await h.call("ansa_connection_status")
                if status["data"]["backend_id"] == prior_backend:
                    raise AssertionError("Connection backend did not restart")
                queries = []
                for receipt in receipts:
                    queried = await h.call("ansa_task_query", {"operation_id": receipt["operation_id"]})
                    if (queried["task_id"] != receipt["task_id"] or queried["data"]["result"] != receipt["result"]
                            or queried["target"] != receipt["target"] or queried["model"] != receipt["model"]):
                        raise AssertionError("Original creation evidence changed after backend restart")
                    queries.append(queried["task_id"])
                await common._bind_for_run(h, identity, one, two, identity["port"])
                if await oracle(h) != final_oracle:
                    raise AssertionError("Restarted original-task queries replayed a model write")
                owned.record(report, path, "backend_restart_original_creation_receipts_without_replay", {"original_task_ids": queries, "independent_unchanged": True})


if __name__ == "__main__":
    raise SystemExit(owned.main(report_name="connections-report.json",
        scope="NASTRAN six point definitions and bounded connector/point inventory; no FE realization or new visual acceptance",
        verify_callback=verify))
