"""Unit checks for bounded connection listing and point creation."""

import sys
import types
import unittest
from unittest.mock import Mock, patch

from ansa_mcp_bridge.operations import connections


POINT_TYPES = connections._POINT_TYPES


class FakeEntity:
    def __init__(self, entity_id, entity_type, fields=None, position=None):
        self._id = entity_id
        self._type = entity_type
        self.fields = dict(fields or {})
        self.position = position

    def ansa_type(self):
        return self._type

    def card_fields(self, _deck):
        return list(self.fields)


class FakeBase:
    def __init__(self):
        self.deck = 1
        self.entities = {}
        self.available = [connections._CONNECTOR_TYPE, *POINT_TYPES, "ConnectionLine_Type"]
        self.collect_calls = []
        self.collect_results = {}
        self.fail_collect_calls = set()
        self.fail_collect_after_set = False
        self.invalid_metadata = set()
        self.set_calls = []
        self.set_status = 0
        self.next_id = 900

    def add(self, entity_type, entity_id, fields=None, position=None, **kwargs):
        values = dict(fields or {})
        values.update(kwargs)
        if position is None and all(key in values for key in ("X", "Y", "Z")):
            position = (values["X"], values["Y"], values["Z"])
        entity = FakeEntity(entity_id, entity_type, values, position)
        self.entities[(entity_type, entity_id)] = entity
        return entity

    def CurrentDeck(self):
        return self.deck

    def TypesInCategory(self, _deck, category):
        if category != "__CONNECTIONS__":
            raise AssertionError("unexpected entity category")
        return list(self.available)

    def CollectEntities(self, deck, containers, entity_type, recursive=False):
        self.collect_calls.append((deck, containers, entity_type, recursive))
        if len(self.collect_calls) in self.fail_collect_calls:
            raise RuntimeError("collection failed after write")
        if self.fail_collect_after_set and self.set_calls and entity_type == "CONNECTOR_ENTITY":
            raise RuntimeError("connector range failed after setting point fields")
        if entity_type in self.collect_results:
            return self.collect_results[entity_type]
        if containers is not None or recursive is not False:
            raise AssertionError("connection scans must be direct database queries")
        return [entity for (kind, _), entity in self.entities.items() if kind == entity_type]

    def GetEntity(self, _deck, entity_type, entity_id):
        return self.entities.get((entity_type, entity_id))

    def GetEntityCardValues(self, _deck, entity, fields):
        if (entity._type, entity._id) in self.invalid_metadata:
            return {key: entity.fields[key] for key in fields if key in entity.fields}
        available = {"__id__": entity._id, "__type__": entity._type, **entity.fields}
        return {key: available[key] for key in fields if key in available}

    def SetEntityCardValues(self, _deck, entity, fields):
        self.set_calls.append((entity, dict(fields)))
        entity.fields.update(fields)
        return self.set_status


class FakeConnections:
    def __init__(self, base):
        self.base = base
        self.create_calls = []
        self.create_behavior = "ok"

    def EntsToConnectivityString(self, entities):
        if len(entities) != 1:
            raise AssertionError("expected one Part")
        return entities[0].fields["Module Id"]

    def ConnectivityStringToEnts(self, value):
        return [entity for (kind, _), entity in self.base.entities.items()
                if kind == "ANSAPART" and entity.fields.get("Module Id") == value]

    def CreateConnectionPoint(self, entity_type, position, entity_id, connectivity):
        self.create_calls.append((entity_type, list(position), entity_id, list(connectivity)))
        if entity_id == 0:
            self.base.next_id += 1
            entity_id = self.base.next_id
        fields = {"Name": "", "D": 0.0, "X": position[0], "Y": position[1], "Z": position[2],
                  "P1": connectivity[0], "P2": connectivity[1]}
        if entity_type != "Clip_Type":
            fields.update({f"P{index}": "" for index in range(3, 11)})
        entity = self.base.add(entity_type, entity_id, fields, position)
        if self.create_behavior == "none_after_add":
            return None
        if self.create_behavior == "raise_after_add":
            raise RuntimeError("creation raised after adding the point")
        return entity


class FakeState:
    def __init__(self):
        self.assertions = 0
        self.error = None

    def assert_main(self):
        self.assertions += 1
        if self.error is not None:
            raise self.error


class ConnectionOperationsChecks(unittest.TestCase):
    def setUp(self):
        self.base = FakeBase()
        self.api = FakeConnections(self.base)
        self.state = FakeState()
        self.ansa = types.ModuleType("ansa")
        self.ansa.base = self.base
        self.ansa.connections = self.api
        self.ansa.constants = types.SimpleNamespace(NASTRAN=1)
        self.module_patch = patch.dict(sys.modules, {"ansa": self.ansa})
        self.module_patch.start()
        self.addCleanup(self.module_patch.stop)

    def run_tool(self, tool, params):
        return connections.execute(self.state, tool, params)

    def add_part(self, entity_id, module_id, name=None):
        fields = {"Module Id": module_id, "Name": name or f"Part {entity_id}"}
        return self.base.add("ANSAPART", entity_id, fields)

    def add_point(self, entity_type, entity_id, first="part-a", second="part-b"):
        fields = {
            "Name": f"{entity_type} {entity_id}",
            "D": 3.0,
            "X": float(entity_id),
            "Y": 1.0,
            "Z": -2.0,
            "P1": first,
            "P2": second,
        }
        if entity_type != "Clip_Type":
            fields.update({f"P{index}": "" for index in range(3, 11)})
        return self.base.add(
            entity_type, entity_id, fields, (fields["X"], fields["Y"], fields["Z"])
        )

    def create_params(self, **overrides):
        params = {
            "position": [1.25, -2.5, 3.75],
            "parts": [
                {"deck": 1, "type": "ANSAPART", "id": 1},
                {"deck": 1, "type": "ANSAPART", "id": 2},
            ],
            "diameter": 4.5,
            "connection_type": "Bolt_Type",
            "entity_id": 77,
            "name": "bolt-77",
        }
        params.update(overrides)
        return params

    def seed_parts(self):
        self.add_part(1, "part-a")
        self.add_part(2, "part-b")

    def test_list_reads_all_six_point_types_and_connector_scope(self):
        self.seed_parts()
        for index, entity_type in enumerate(POINT_TYPES, start=10):
            self.add_point(entity_type, index)
        self.base.add("CONNECTOR_ENTITY", 40, {"Name": "connector"}, (9.0, 8.0, 7.0))
        self.base.add("ConnectionLine_Type", 50, {"Name": "out of scope"})

        get_entity = self.base.GetEntity

        def point_lookup_only(deck, entity_type, entity_id):
            if entity_type in ("CONNECTOR_ENTITY", *POINT_TYPES):
                raise AssertionError("connection handles must come from a fresh typed collection")
            return get_entity(deck, entity_type, entity_id)

        with patch.object(self.base, "GetEntity", side_effect=point_lookup_only):
            result = self.run_tool("connection_list", {})

        self.assertEqual(result["count"], 7)
        self.assertEqual(result["returned"], 7)
        self.assertTrue(result["complete"])
        self.assertEqual({row["entity"]["type"] for row in result["connections"]},
                         {*POINT_TYPES, "CONNECTOR_ENTITY"})
        rows = {row["entity"]["type"]: row for row in result["connections"]}
        for entity_type in POINT_TYPES:
            row = rows[entity_type]
            self.assertEqual(row["kind"], "point")
            self.assertEqual(row["diameter"], 3.0)
            self.assertEqual([item["field"] for item in row["connectivity"][:2]], ["P1", "P2"])
            self.assertEqual(row["connectivity"][0]["entities"], [
                {"deck": 1, "type": "ANSAPART", "id": 1},
            ])
            self.assertEqual(row["connectivity"][1]["entities"], [
                {"deck": 1, "type": "ANSAPART", "id": 2},
            ])
        connector = rows["CONNECTOR_ENTITY"]
        self.assertEqual(connector["kind"], "connector")
        self.assertIsNone(connector["diameter"])
        self.assertEqual(connector["position"], [9.0, 8.0, 7.0])
        self.assertEqual(connector["connectivity"], [])
        self.assertTrue(connector["complete"])
        self.assertNotIn("ConnectionLine_Type", [call[2] for call in self.base.collect_calls])

    def test_list_paginates_and_rejects_unobservable_ranges(self):
        self.add_point("Bolt_Type", 9)
        self.add_point("Bolt_Type", 7)
        self.add_point("SpotweldPoint_Type", 3)
        result = self.run_tool("connection_list", {"kind": "point", "offset": 1, "limit": 1})
        self.assertEqual(result["count"], 3)
        self.assertEqual(result["returned"], 1)
        self.assertEqual(result["connections"][0]["entity"]["id"], 9)
        self.assertTrue(result["has_more"])

        empty_base = self.base
        empty_base.entities.clear()
        empty = self.run_tool("connection_list", {"kind": "connector"})
        self.assertEqual((empty["count"], empty["returned"], empty["connections"]), (0, 0, []))
        self.assertTrue(empty["complete"])

        self.base.collect_results["Bolt_Type"] = None
        with self.assertRaisesRegex(RuntimeError, "CollectEntities returned NoneType"):
            self.run_tool("connection_list", {"kind": "point"})

        self.base.collect_results.clear()
        point = self.add_point("Bolt_Type", 12)
        self.base.invalid_metadata.add((point._type, point._id))
        with self.assertRaisesRegex(RuntimeError, "entity ID and type metadata"):
            self.run_tool("connection_list", {"kind": "point"})

        self.base.available.remove("Screw_Type")
        collect_count = len(self.base.collect_calls)
        with self.assertRaisesRegex(RuntimeError, "omitted supported connection type.*Screw_Type"):
            self.run_tool("connection_list", {"kind": "point"})
        self.assertEqual(len(self.base.collect_calls), collect_count)
        connector_only = self.run_tool("connection_list", {"kind": "connector"})
        self.assertEqual(connector_only["count"], 0)
        self.assertTrue(connector_only["complete"])

        self.base.available.append("Screw_Type")
        self.base.invalid_metadata.clear()
        self.base.collect_results["SpotweldPoint_Type"] = []
        self.base.collect_results["Bolt_Type"] = [
            FakeEntity(entity_id, "Bolt_Type")
            for entity_id in range(1, connections._MAX_CONNECTIONS + 2)
        ]
        with self.assertRaisesRegex(RuntimeError, "range exceeds 10000"):
            self.run_tool("connection_list", {"kind": "point"})

    def test_create_sets_diameter_and_reads_back_exact_parts_and_range(self):
        self.seed_parts()

        for entity_id, entity_type in enumerate(POINT_TYPES, start=77):
            name = None if entity_type == "Clip_Type" else f"{entity_type}-{entity_id}"
            result = self.run_tool(
                "connection_create",
                self.create_params(connection_type=entity_type, entity_id=entity_id, name=name),
            )

            expected = {"deck": 1, "type": entity_type, "id": entity_id}
            self.assertTrue(result["ok"])
            self.assertEqual(result["created"], expected)
            self.assertEqual(result["added"], [expected])
            self.assertEqual(result["removed"], [])
            self.assertTrue(result["connection"]["complete"])
            self.assertEqual(result["connection"]["position"], [1.25, -2.5, 3.75])
            self.assertEqual(result["connection"]["diameter"], 4.5)
            self.assertEqual(result["connection"]["name"], name or "")
            self.assertEqual(result["connection"]["connectivity"], [
                {
                    "field": "P1",
                    "text": "part-a",
                    "entities": [{"deck": 1, "type": "ANSAPART", "id": 1}],
                },
                {
                    "field": "P2",
                    "text": "part-b",
                    "entities": [{"deck": 1, "type": "ANSAPART", "id": 2}],
                },
            ])

        self.assertEqual(len(self.api.create_calls), len(POINT_TYPES))
        self.assertTrue(all(len(call) == 4 for call in self.api.create_calls))
        self.assertEqual(
            [fields for _entity, fields in self.base.set_calls],
            [
                ({"D": 4.5} if entity_type == "Clip_Type" else
                 {"D": 4.5, "Name": f"{entity_type}-{entity_id}"})
                for entity_id, entity_type in enumerate(POINT_TYPES, start=77)
            ],
        )

    def test_create_rejects_invalid_inputs_parts_and_occupied_ids_before_write(self):
        self.seed_parts()
        cases = (
            (self.create_params(extra=True), "unsupported key"),
            (self.create_params(position=[True, 0, 0]), "finite number"),
            (self.create_params(entity_id=True), "entity_id must be an integer"),
            (self.create_params(parts=[
                {"deck": 1, "type": "ANSAPART", "id": 1},
                {"deck": 1, "type": "ANSAPART", "id": 1},
            ]), "two different ANSAPART"),
        )
        for params, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                self.run_tool("connection_create", params)

        self.base.add("CONNECTOR_ENTITY", 77, {"Name": "occupied"})
        with self.assertRaisesRegex(ValueError, "already used"):
            self.run_tool("connection_create", self.create_params())
        self.assertEqual(self.api.create_calls, [])
        self.assertEqual(self.base.set_calls, [])

        self.add_part(3, "part-a-duplicate")
        self.base.entities[("ANSAPART", 3)].fields["Module Id"] = "part-a"
        with self.assertRaisesRegex(RuntimeError, "exactly one entity"):
            self.run_tool("connection_create", self.create_params(entity_id=0))
        self.assertEqual(self.api.create_calls, [])

    def test_create_failures_keep_created_and_observed_diff_evidence(self):
        self.seed_parts()
        self.api.create_behavior = "none_after_add"
        none_result = self.run_tool("connection_create", self.create_params())
        self.assertFalse(none_result["ok"])
        self.assertEqual(none_result["created"]["id"], 77)
        self.assertEqual(none_result["connection"]["entity"], none_result["created"])
        self.assertEqual(none_result["added"], [none_result["created"]])
        self.assertEqual(none_result["removed"], [])
        self.assertIn("write", none_result["observation_errors"])

        self.base.entities.clear()
        self.base.collect_calls.clear()
        self.base.set_calls.clear()
        self.seed_parts()
        self.api.create_behavior = "raise_after_add"
        raised_result = self.run_tool("connection_create", self.create_params(entity_id=78))
        self.assertFalse(raised_result["ok"])
        self.assertEqual(raised_result["created"]["id"], 78)
        self.assertEqual(raised_result["added"], [raised_result["created"]])
        self.assertEqual(self.base.set_calls, [])

        self.base.entities.clear()
        self.base.collect_calls.clear()
        self.seed_parts()
        self.api.create_behavior = "ok"
        self.base.fail_collect_after_set = True
        partial_result = self.run_tool("connection_create", self.create_params(entity_id=79))
        self.assertFalse(partial_result["ok"])
        self.assertEqual(partial_result["created"]["id"], 79)
        self.assertEqual(partial_result["added"], [partial_result["created"]])
        self.assertIsNone(partial_result["removed"])
        self.assertIn("range", partial_result["observation_errors"])

        self.base.entities.clear()
        self.base.collect_calls.clear()
        self.base.fail_collect_calls.clear()
        self.base.fail_collect_after_set = False
        self.seed_parts()
        self.base.set_status = 1
        status_result = self.run_tool("connection_create", self.create_params(entity_id=80))
        self.assertFalse(status_result["ok"])
        self.assertEqual(status_result["created"]["id"], 80)
        self.assertEqual(status_result["connection"]["diameter"], 4.5)
        self.assertIn("write", status_result["observation_errors"])

        self.base.entities.clear()
        self.base.collect_calls.clear()
        self.base.set_status = 0
        self.seed_parts()
        original_create = self.api.CreateConnectionPoint

        def create_with_partial_extra(*args):
            entity = original_create(*args)
            self.add_point("Bolt_Type", 88)
            self.add_point("Bolt_Type", 89)
            self.base.invalid_metadata.add(("Bolt_Type", 89))
            return entity

        with patch.object(self.api, "CreateConnectionPoint", side_effect=create_with_partial_extra):
            extra_result = self.run_tool("connection_create", self.create_params(entity_id=77))
        self.assertFalse(extra_result["ok"])
        self.assertEqual([ref["id"] for ref in extra_result["added"]], [77, 88])
        self.assertIsNone(extra_result["removed"])
        self.assertIn("Bolt_Type", extra_result["observation_errors"]["range"])

        self.base.entities.clear()
        self.base.collect_calls.clear()
        self.base.invalid_metadata.clear()
        self.seed_parts()
        original_snapshot = connections._snapshot
        snapshot_calls = 0

        def fail_post_snapshot(*args, **kwargs):
            nonlocal snapshot_calls
            snapshot_calls += 1
            if snapshot_calls == 2:
                raise RuntimeError("unexpected post-write snapshot failure")
            return original_snapshot(*args, **kwargs)

        with patch.object(connections, "_snapshot", side_effect=fail_post_snapshot):
            snapshot_result = self.run_tool("connection_create", self.create_params(entity_id=90))
        self.assertFalse(snapshot_result["ok"])
        self.assertEqual(snapshot_result["created"]["id"], 90)
        self.assertEqual([ref["id"] for ref in snapshot_result["added"]], [90])
        self.assertIsNone(snapshot_result["removed"])
        self.assertIn("post_create", snapshot_result["observation_errors"])

    def test_list_identity_revalidation_failure_finishes_failed_with_retained_rows(self):
        from ansa_mcp_bridge.executor import MainThreadExecutor

        self.seed_parts()
        self.add_point("Bolt_Type", 77)
        original_read = self.base.GetEntityCardValues
        metadata_reads = 0

        def fail_second_identity(deck, entity, fields):
            nonlocal metadata_reads
            if "__id__" in fields:
                metadata_reads += 1
                if metadata_reads >= 2:
                    return {}
            return original_read(deck, entity, fields)

        task = {"tool": "connection_list", "params": {"kind": "point"},
                "task_id": "identity-failure", "model": {"epoch": 1}}
        state = types.SimpleNamespace(assert_main=Mock(), journal=Mock(blocked=False),
            stopped=Mock(is_set=Mock(return_value=False)), connected=Mock(is_set=Mock(return_value=True)),
            authorization={"backend_id": "backend"}, connected_backend_id="backend", validate_task=Mock(),
            probe=Mock(return_value={"model": {"epoch": 1}}), identity=Mock(return_value={"pid": 1}),
            _witness=None, main_thread_id=1)
        state.journal.next_queued.return_value = task
        state.journal.claim.return_value = {"state": "running"}
        with patch.object(self.base, "GetEntityCardValues", side_effect=fail_second_identity):
            MainThreadExecutor(state)._task_tick()
        finish = state.journal.finish.call_args
        self.assertIn("identity", finish.kwargs["error"])
        result = finish.kwargs["result"]["operation_result"]
        self.assertFalse(result["ok"])
        self.assertFalse(result["complete"])
        self.assertEqual(result["connections"][0]["entity"]["id"], 77)
        self.assertIn("entity", result["connections"][0]["errors"])

        self.base.set_status = 0
        with patch.object(connections, "_connection_row", side_effect=RuntimeError("readback unavailable")):
            unreadable_result = self.run_tool(
                "connection_create", self.create_params(entity_id=81)
            )
        self.assertFalse(unreadable_result["ok"])
        self.assertEqual(unreadable_result["created"]["id"], 81)
        self.assertIsNone(unreadable_result["connection"])
        self.assertEqual(unreadable_result["added"], [unreadable_result["created"]])
        self.assertIn("connection", unreadable_result["observation_errors"])
        self.assertIn("verification", unreadable_result["observation_errors"])

        original_snapshot = connections._snapshot

        def fail_after_snapshot(base, deck, kind="all", **kwargs):
            if kwargs.get("tolerate_errors"):
                raise RuntimeError("post-create range observer failed")
            return original_snapshot(base, deck, kind, **kwargs)

        with patch.object(connections, "_snapshot", side_effect=fail_after_snapshot):
            observer_result = self.run_tool(
                "connection_create", self.create_params(entity_id=82)
            )
        self.assertFalse(observer_result["ok"])
        self.assertEqual(observer_result["created"]["id"], 82)
        self.assertEqual(observer_result["added"], [observer_result["created"]])
        self.assertIsNone(observer_result["removed"])
        self.assertIn("post_create", observer_result["observation_errors"])

    def test_wrong_thread_and_wrong_deck_are_rejected_before_collection(self):
        self.state.error = RuntimeError("not the ANSA main thread")
        with self.assertRaisesRegex(RuntimeError, "main thread"):
            self.run_tool("connection_list", {})
        self.assertEqual(self.base.collect_calls, [])

        self.state.error = None
        self.base.deck = 2
        with self.assertRaisesRegex(ValueError, "require the current NASTRAN deck"):
            self.run_tool("connection_create", self.create_params())
        self.assertEqual(self.api.create_calls, [])
        self.assertEqual(self.base.collect_calls, [])


if __name__ == "__main__":
    unittest.main()
