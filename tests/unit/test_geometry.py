"""Focused tests for read-only FACE ranking and deletion previews."""

import json
import sys
import types
import unittest
from unittest.mock import patch

from ansa_mcp_bridge.operations import geometry
from ansa_mcp_shared.framing import MAX_FRAME_BYTES


class FakeEntity:
    def __init__(self, entity_id, entity_type="FACE", area=1.0, center=(0.0, 0.0, 0.0),
                 normal=(0.0, 0.0, 1.0)):
        self._id = entity_id
        self._type = entity_type
        self.area = area
        self.center = center
        self.normal = normal


class FakeBase:
    Entity = FakeEntity

    def __init__(self):
        self.deck = 1
        self.entities = {}
        self.collected_override = None
        self.bounds_override = None
        self.read_calls = []
        self.write_calls = []
        self.bound_entities = None

    def add_face(self, entity_id, area=1.0, center=(0.0, 0.0, 0.0),
                 normal=(0.0, 0.0, 1.0)):
        entity = FakeEntity(entity_id, area=area, center=center, normal=normal)
        self.entities[("FACE", entity_id)] = entity
        return entity

    def CurrentDeck(self):
        self.read_calls.append("CurrentDeck")
        return self.deck

    def CollectEntities(self, deck, containers, entity_type):
        self.read_calls.append(("CollectEntities", deck, containers, entity_type))
        if self.collected_override is not None:
            return self.collected_override
        return [entity for (kind, _), entity in self.entities.items() if kind == entity_type]

    def GetEntity(self, deck, entity_type, entity_id):
        self.read_calls.append(("GetEntity", deck, entity_type, entity_id))
        return self.entities.get((entity_type, entity_id))

    def GetEntityCardValues(self, deck, entity, fields):
        self.read_calls.append(("GetEntityCardValues", deck, tuple(fields)))
        values = {"__id__": entity._id, "__type__": entity._type}
        return {field: values[field] for field in fields if field in values}

    def GetFaceArea(self, face):
        self.read_calls.append(("GetFaceArea", face._id))
        return face.area

    def Cog(self, face):
        self.read_calls.append(("Cog", face._id))
        return face.center

    def GetFaceOrientation(self, face):
        self.read_calls.append(("GetFaceOrientation", face._id))
        return face.normal

    def BoundBox(self, entities):
        self.read_calls.append(("BoundBox", tuple(entity._id for entity in entities)))
        self.bound_entities = list(entities)
        if self.bounds_override is not None:
            return self.bounds_override
        points = [entity.center for entity in entities]
        return [
            min(point[0] for point in points),
            min(point[1] for point in points),
            min(point[2] for point in points),
            max(point[0] for point in points),
            max(point[1] for point in points),
            max(point[2] for point in points),
        ]

    def SetEntityVisibilityValues(self, *args, **kwargs):
        self.write_calls.append(("SetEntityVisibilityValues", args, kwargs))

    def SetCurrentSelection(self, *args, **kwargs):
        self.write_calls.append(("SetCurrentSelection", args, kwargs))

    def DeleteEntity(self, *args, **kwargs):
        self.write_calls.append(("DeleteEntity", args, kwargs))


class FakeState:
    def __init__(self):
        self.main_calls = 0

    def assert_main(self):
        self.main_calls += 1


class GeometryChecks(unittest.TestCase):
    def setUp(self):
        self.base = FakeBase()
        self.state = FakeState()
        ansa = types.ModuleType("ansa")
        ansa.base = self.base
        self.ansa = ansa
        self.ansa_patch = patch.dict(sys.modules, {"ansa": ansa})
        self.ansa_patch.start()
        self.addCleanup(self.ansa_patch.stop)

    def run_tool(self, tool="geometry_select", **params):
        return geometry.execute(self.state, tool, {"deck": 1, **params})

    def test_all_rankings_use_stable_id_ties_and_nearest_euclidean_cog_distance(self):
        faces = [
            (1, 1.0, (0.0, 0.0, 2.0)),
            (2, 5.0, (1.0, 0.0, 0.0)),
            (3, 5.0, (-1.0, 0.0, 0.0)),
            (4, 4.0, (1.0, 1.0, 1.0)),
            (5, 3.0, (1.0, 1.0, 0.0)),
            (6, 4.0, (-1.0, -1.0, 1.0)),
        ]
        for face_id, area, center in faces:
            self.base.add_face(face_id, area, center)
        self.base.collected_override = [self.base.entities[("FACE", face_id)]
                                        for face_id in reversed(range(1, 7))]

        expected = {
            "largest_area": [2, 3, 4, 6, 5, 1],
            "smallest_area": [1, 5, 4, 6, 2, 3],
            "highest_z": [1, 4, 6, 2, 3, 5],
            "lowest_z": [2, 3, 5, 4, 6, 1],
        }
        for query, face_ids in expected.items():
            with self.subTest(query=query):
                result = self.run_tool(query=query, limit=10)
                self.assertEqual(result["face_ids"], face_ids)
                self.assertEqual(result["matched_count"], result["source_count"])
                self.assertEqual(result["scope"], "all")

        default_limit = self.run_tool(query="largest_area")
        self.assertEqual(default_limit["limit"], 5)
        self.assertEqual(len(default_limit["candidates"]), 5)

        nearest = self.run_tool(query="nearest_center", point=[0, 0, 0], limit=10)
        self.assertEqual(nearest["face_ids"], [2, 3, 5, 4, 6, 1])
        self.assertAlmostEqual(nearest["candidates"][3]["score"], 3 ** 0.5)
        self.assertEqual(nearest["candidates"][3]["face"]["id"], 4)

    def test_normal_z_rankings_normalize_keep_both_signs_and_break_ties_by_id(self):
        normals = {
            1: [3.0, 0.0, 4.0],
            2: [0.0, 0.0, 9.0],
            3: [-4.0, 0.0, 3.0],
            4: [0.0, 0.0, -2.0],
            5: [0.0, 0.0, -1.0],
            6: [0.0, 0.0, 1.0],
        }
        for face_id, normal in normals.items():
            self.base.add_face(face_id, normal=normal)

        expected = {
            "normal_pos_z": [2, 6, 1, 3, 4, 5],
            "normal_neg_z": [4, 5, 3, 1, 2, 6],
        }
        for query, face_ids in expected.items():
            with self.subTest(query=query):
                result = self.run_tool(query=query, limit=10)
                self.assertEqual(result["face_ids"], face_ids)
                self.assertEqual(result["source_count"], len(normals))
                self.assertEqual(result["matched_count"], len(normals))
                self.assertTrue(all("normal" in item for item in result["candidates"]))
                candidate = next(item for item in result["candidates"]
                                 if item["face"]["id"] == 1)
                self.assertEqual(candidate["normal"], [0.6, 0.0, 0.8])
                self.assertAlmostEqual(candidate["score"], 0.8)

        self.assertEqual(sum(call[0] == "GetFaceOrientation" for call in self.base.read_calls), 12)

    def test_bad_normals_fail_and_existing_queries_never_read_orientation(self):
        invalid_normals = [None, [0.0, 0.0, 0.0], [0.0, float("nan"), 1.0],
                           [float("inf"), 0.0, 1.0], [1.0, 2.0], "invalid"]
        for invalid in invalid_normals:
            with self.subTest(normal=invalid):
                self.base.entities.clear()
                self.base.read_calls.clear()
                self.base.add_face(1, normal=[0.0, 0.0, 1.0])
                self.base.add_face(2, normal=invalid)
                with self.assertRaisesRegex(RuntimeError, "orientation"):
                    self.run_tool(query="normal_pos_z")
                self.assertEqual(self.base.write_calls, [])

        self.base.entities.clear()
        self.base.add_face(3)
        queries = ("largest_area", "smallest_area", "highest_z", "lowest_z", "nearest_center")
        for query in queries:
            with self.subTest(query=query):
                self.base.read_calls.clear()
                options = {"query": query}
                if query == "nearest_center":
                    options["point"] = [0.0, 0.0, 0.0]
                result = self.run_tool(**options)
                self.assertFalse(any(call[0] == "GetFaceOrientation"
                                     for call in self.base.read_calls))
                self.assertEqual(set(result["candidates"][0]), {"face", "area", "center", "score"})

    def test_delete_condition_preview_option_is_strict_lazy_and_merged(self):
        self.base.add_face(7)
        basic = self.run_tool("geometry_preview", query="largest_area", limit=200)
        self.assertNotIn("delete_ready", basic)
        self.assertNotIn("expected_cons_ids", basic)

        for invalid in (1, "true", None):
            with self.subTest(include_delete_conditions=invalid):
                with self.assertRaisesRegex(ValueError, "must be a boolean"):
                    self.run_tool("geometry_preview", query="largest_area",
                                  include_delete_conditions=invalid)
        with self.assertRaisesRegex(ValueError, "at most 100"):
            self.run_tool("geometry_preview", query="largest_area", limit=101,
                          include_delete_conditions=True)
        with self.assertRaisesRegex(ValueError, "unsupported parameter"):
            self.run_tool("geometry_select", query="largest_area",
                          include_delete_conditions=False)

        self.ansa.mesh = object()
        self.ansa.constants = object()
        helper_result = {
            "delete_ready": True,
            "delete_conditions": [{"condition": "opaque"}],
            "delete_rejections": [],
            "expected_cons_ids": [19],
        }
        calls = []

        def prepare_preview(base, mesh, constants, deck, face_ids):
            calls.append((base, mesh, constants, deck, face_ids))
            return helper_result

        from ansa_mcp_bridge.operations import geometry_delete
        with patch.object(geometry_delete, "prepare_preview", side_effect=prepare_preview):
            result = self.run_tool(
                "geometry_preview", query="largest_area", limit=100,
                include_delete_conditions=True,
            )

        self.assertEqual(result["delete_ready"], True)
        self.assertEqual(result["delete_conditions"], helper_result["delete_conditions"])
        self.assertEqual(result["delete_rejections"], [])
        self.assertEqual(result["expected_cons_ids"], [19])
        self.assertEqual(calls, [(self.base, self.ansa.mesh, self.ansa.constants, 1, [7])])

    def test_invalid_parameters_are_rejected_without_write_api_calls(self):
        invalid = [
            ({"query": "largest_area", "unexpected": True}, ValueError),
            ({"query": "not_allowed"}, ValueError),
            ({"query": "nearest_center"}, ValueError),
            ({"query": "nearest_center", "point": [0, float("nan"), 0]}, ValueError),
            ({"query": "nearest_center", "point": [0, True, 0]}, ValueError),
            ({"query": "highest_z", "point": [0, 0, 0]}, ValueError),
            ({"query": "largest_area", "face_ids": []}, ValueError),
            ({"query": "largest_area", "face_ids": [1, 1]}, ValueError),
            ({"query": "largest_area", "face_ids": [True]}, ValueError),
            ({"query": "largest_area", "face_ids": [0]}, ValueError),
            ({"query": "largest_area", "limit": True}, ValueError),
            ({"query": "largest_area", "limit": 201}, ValueError),
        ]
        for options, error in invalid:
            with self.subTest(options=options):
                with self.assertRaises(error):
                    self.run_tool(**options)
        with self.assertRaisesRegex(ValueError, "at most 10000"):
            self.run_tool(query="largest_area", face_ids=list(range(1, 10002)))
        self.assertEqual(self.base.write_calls, [])

    def test_global_limit_and_duplicate_or_wrong_collectentities_identity_are_rejected(self):
        self.base.collected_override = [FakeEntity(index + 1) for index in range(10001)]
        with self.assertRaisesRegex(ValueError, "10000"):
            self.run_tool(query="largest_area")
        self.assertFalse(any(call[0] == "GetFaceArea" for call in self.base.read_calls))

        self.base.read_calls.clear()
        duplicate = FakeEntity(7)
        self.base.collected_override = [duplicate, FakeEntity(7)]
        with self.assertRaisesRegex(RuntimeError, "duplicate FACE IDs"):
            self.run_tool(query="largest_area")

        self.base.collected_override = [FakeEntity(8, entity_type="CONS")]
        with self.assertRaisesRegex(RuntimeError, "type does not match"):
            self.run_tool(query="largest_area")
        self.assertEqual(self.base.write_calls, [])

    def test_bad_metrics_and_missing_explicit_faces_fail_instead_of_using_defaults(self):
        self.base.add_face(1, area=-1.0, center=(0, 0, 0))
        with self.assertRaisesRegex(RuntimeError, "negative area"):
            self.run_tool(query="largest_area")

        self.base.entities[("FACE", 1)].area = 1.0
        self.base.entities[("FACE", 1)].center = None
        with self.assertRaisesRegex(RuntimeError, "invalid center"):
            self.run_tool(query="largest_area")

        self.base.entities[("FACE", 1)].center = (0.0, float("inf"), 0.0)
        with self.assertRaisesRegex(RuntimeError, "finite number"):
            self.run_tool(query="largest_area")

        with self.assertRaisesRegex(LookupError, "FACE 2 does not exist"):
            self.run_tool(query="largest_area", face_ids=[2])
        self.assertEqual(self.base.write_calls, [])

    def test_preview_summarizes_only_bounded_candidates_and_has_no_model_or_ui_side_effects(self):
        for face_id in range(1, 251):
            self.base.add_face(
                face_id,
                area=float(face_id),
                center=(float(face_id), -float(face_id), float(face_id % 7)),
            )

        result = self.run_tool(
            "geometry_preview",
            query="largest_area",
            face_ids=list(range(1, 251)),
            limit=200,
        )
        expected_ids = list(range(250, 50, -1))
        self.assertEqual(result["scope"], "explicit")
        self.assertEqual(result["face_ids"], expected_ids)
        self.assertEqual(result["would_delete_face_ids"], expected_ids)
        self.assertEqual(len(result["candidates"]), 200)
        self.assertTrue(result["preview_only"])
        self.assertTrue(result["requires_confirmation"])
        self.assertTrue(result["read_only"])
        self.assertFalse(result["selection_changed"])
        self.assertEqual(result["total_area"], sum(float(value) for value in expected_ids))
        self.assertEqual(self.base.bound_entities, [self.base.entities[("FACE", i)] for i in expected_ids])
        self.assertEqual(result["bounds"], [51.0, -250.0, 0.0, 250.0, -51.0, 6.0])
        payload = json.dumps(result, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        self.assertLessEqual(len(payload), MAX_FRAME_BYTES)
        self.assertEqual(self.base.write_calls, [])

        self.base.entities.clear()
        empty = self.run_tool("geometry_preview", query="smallest_area")
        self.assertEqual(empty["face_ids"], [])
        self.assertEqual(empty["total_area"], 0.0)
        self.assertIsNone(empty["bounds"])

        self.base.add_face(300, area=1.0, center=(0, 0, 0))
        self.base.bounds_override = [0, 0, 0, 1, 1, float("nan")]
        with self.assertRaisesRegex(RuntimeError, "finite number"):
            self.run_tool("geometry_preview", query="largest_area")
        self.assertEqual(self.base.write_calls, [])


class GeometrySchemaChecks(unittest.IsolatedAsyncioTestCase):
    async def test_mcp_geometry_schema_validates_queries_and_preview_option_before_admission(self):
        from mcp.server.fastmcp.exceptions import ToolError
        from ansa_mcp_plugin.__main__ import create_server

        class Service:
            calls = []

            def submit_task(self, tool, params, operation_id, target, model, ttl):
                self.calls.append((tool, params))
                return {"task": {"target": target, "model": model}, "state": "queued",
                        "error": None, "result": None, "dispatch": "not_sent", "acked": False,
                        "cancel_pending": False, "operation_id": operation_id, "task_id": "schema-task"}

        service = Service()
        server = create_server(service)
        args = {"operation_id": "geometry-schema", "expected_target": {
            "backend_id": "b", "instance_id": "i", "session_id": "s", "generation": 1},
            "expected_model": {"epoch": 1}, "query": "largest_area"}
        for tool in ("ansa_geometry_select", "ansa_geometry_preview"):
            for invalid in (True, "1", 1.0, -1):
                with self.subTest(tool=tool, deck=invalid):
                    previous = len(service.calls)
                    with self.assertRaises(ToolError):
                        await server.call_tool(tool, {**args, "deck": invalid})
                    self.assertEqual(len(service.calls), previous)
            await server.call_tool(tool, {**args, "deck": 1})
            self.assertEqual(service.calls[-1][1]["deck"], 1)

        for query in ("normal_pos_z", "normal_neg_z"):
            await server.call_tool("ansa_geometry_select", {**args, "deck": 1, "query": query})
            self.assertEqual(service.calls[-1][1]["query"], query)

        await server.call_tool("ansa_geometry_preview", {
            **args, "deck": 1, "limit": 200,
        })
        self.assertFalse(service.calls[-1][1]["include_delete_conditions"])
        await server.call_tool("ansa_geometry_preview", {
            **args, "deck": 1, "limit": 100, "include_delete_conditions": True,
        })
        self.assertTrue(service.calls[-1][1]["include_delete_conditions"])

        for tool_args in (
            {"tool": "ansa_geometry_preview", "include_delete_conditions": 1},
            {"tool": "ansa_geometry_preview", "include_delete_conditions": True, "limit": 101},
        ):
            tool = tool_args["tool"]
            extra = {key: value for key, value in tool_args.items() if key != "tool"}
            with self.subTest(tool_args=tool_args):
                previous = len(service.calls)
                with self.assertRaises(ToolError):
                    await server.call_tool(tool, {**args, "deck": 1, **extra})
                self.assertEqual(len(service.calls), previous)


if __name__ == "__main__":
    unittest.main()
