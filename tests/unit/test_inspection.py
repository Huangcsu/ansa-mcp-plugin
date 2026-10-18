"""Focused checks for bounded inspection and read-only ANSA checks."""

import json
import sys
import types
import unittest
from unittest.mock import patch

from ansa_mcp_bridge.operations import inspection


class FakeEntity:
    def __init__(self, entity_id, entity_type):
        self._id = entity_id
        self._type = entity_type

    def ansa_type(self):
        return self._type


class FakeCheckReport:
    def __init__(self, entity):
        self.name = "sample"
        self.status = "failed"
        self.errors = [entity]
        self.try_fix_calls = 0

    def try_fix(self):
        self.try_fix_calls += 1


class FakeCheck:
    def __init__(self, report):
        self.report = report
        self.calls = []

    def execute(self, **kwargs):
        self.calls.append(kwargs)
        return [self.report]


class FakeBase:
    Entity = FakeEntity

    def __init__(self):
        self.entities = {}
        self.types = ["FACE", "CONS", "GRID", "SHELL", "SOLID"]
        self.visibility_calls = []
        self.mass_calls = []
        self.checks_created = []
        self.deck = 1

        def make_check():
            check = FakeCheck(self.report)
            self.checks_created.append(check)
            return check

        self.report = FakeCheckReport(FakeEntity(3, "GRID"))
        self.Check = types.SimpleNamespace(EXEC_ON_SELECTED=4, REPORT_NONE=0)
        self.checks = types.SimpleNamespace(
            mesh=types.SimpleNamespace(MeshQuality=make_check),
            geometry=types.SimpleNamespace(Basic=make_check, Extensive=make_check),
        )

    def add(self, entity_type, entity_id):
        entity = FakeEntity(entity_id, entity_type)
        self.entities[(entity_type, entity_id)] = entity
        return entity

    def CurrentDeck(self):
        return self.deck

    def TypesInCategory(self, deck, category):
        return list(self.types)

    def CollectEntities(self, deck, containers, search_types, recursive=False):
        if type(search_types) is str:
            search_types = [search_types]
        return [entity for (entity_type, _), entity in self.entities.items()
                if entity_type in search_types]

    def GetEntity(self, deck, entity_type, entity_id):
        return self.entities.get((entity_type, entity_id))

    def GetEntityCardValues(self, deck, entity, fields):
        values = {"__id__": entity._id, "__type__": entity._type}
        return {field: values[field] for field in fields if field in values}

    def GetFaceArea(self, face):
        return 12.5

    def Cog(self, entity):
        return (1.0, 2.0, 3.0)

    def CalcSolidVolume(self, entity):
        return 25.0

    def GPWGInfo(self, apply_on, custom_entities):
        self.mass_calls.append((apply_on, custom_entities))
        return types.SimpleNamespace(mass=(3.5, 0.0, 0.0))

    def SetEntityVisibilityValues(self, deck, fields):
        self.visibility_calls.append((deck, dict(fields)))
        return 1


class FakeState:
    def __init__(self):
        self.main_calls = 0

    def assert_main(self):
        self.main_calls += 1


class InspectionChecks(unittest.TestCase):
    def setUp(self):
        self.base = FakeBase()
        self.constants = types.SimpleNamespace(NASTRAN=1, OPTISTRUCT=2)
        self.ansa = types.ModuleType("ansa")
        self.ansa.base = self.base
        self.ansa.constants = self.constants
        self.state = FakeState()
        self.module_patch = patch.dict(sys.modules, {"ansa": self.ansa})
        self.module_patch.start()
        self.addCleanup(self.module_patch.stop)

    def run_tool(self, tool, params):
        return inspection.execute(self.state, tool, params)

    def test_geometry_inventory_pages_types_and_counts(self):
        self.base.add("FACE", 1)
        self.base.add("CONS", 2)
        self.base.add("CONS", 3)

        result = self.run_tool("geometry_inventory", {
            "deck": 1, "offset": 1, "limit": 1,
        })

        self.assertEqual(result["total_types"], 5)
        self.assertEqual(result["counts"], [{"entity_type": "CONS", "count": 2}])
        self.assertTrue(result["has_more"])

    def test_face_properties_return_area_and_centroid_as_strict_json(self):
        self.base.add("FACE", 8)

        result = self.run_tool("face_properties", {"deck": 1, "face_ids": [8, 9]})

        self.assertEqual(result["faces"][0]["face"], {"deck": 1, "type": "FACE", "id": 8})
        self.assertEqual(result["faces"][0]["area"], 12.5)
        self.assertEqual(result["faces"][0]["center"], [1.0, 2.0, 3.0])
        self.assertFalse(result["faces"][1]["found"])
        self.assertEqual(json.loads(json.dumps(result, allow_nan=False)), result)
        disabled_result = self.run_tool("face_properties", {
            "deck": 1, "face_ids": [8, 9], "include_geometry": False,
        })
        self.assertEqual(disabled_result, result)

    def test_face_properties_include_geometry_preserves_orientation_and_normalizes_safely(self):
        self.base.add("FACE", 8)
        self.base.add("FACE", 9)
        orientations = {
            8: [3e300, 4e300, 0.0],
            9: [3e-300, -4e-300, 0.0],
        }
        self.base.GetFaceOrientation = lambda face: orientations[face._id]
        self.base.BoundBox = lambda faces: [-1, -2, -3, 4, 5, 6]

        result = self.run_tool("face_properties", {
            "deck": 1, "face_ids": [8, 9], "include_geometry": True,
        })

        self.assertTrue(result["read_only"])
        self.assertEqual(result["scope"], "explicit")
        self.assertTrue(result["include_geometry"])
        self.assertTrue(result["complete"])
        for row, expected_orientation, expected_normal in (
            (result["faces"][0], orientations[8], [0.6, 0.8, 0.0]),
            (result["faces"][1], orientations[9], [0.6, -0.8, 0.0]),
        ):
            self.assertTrue(row["complete"])
            self.assertEqual(row["orientation"], expected_orientation)
            for actual, expected in zip(row["unit_normal"], expected_normal):
                self.assertAlmostEqual(actual, expected)
            self.assertEqual(row["bounds"], [-1.0, -2.0, -3.0, 4.0, 5.0, 6.0])
        self.assertEqual(json.loads(json.dumps(result, allow_nan=False)), result)

        missing_result = self.run_tool("face_properties", {
            "deck": 1, "face_ids": [8, 10], "include_geometry": True,
        })
        self.assertFalse(missing_result["complete"])
        self.assertFalse(missing_result["faces"][1]["found"])
        self.assertFalse(missing_result["faces"][1]["complete"])

    def test_face_properties_geometry_reports_partial_failures_and_rejects_early(self):
        face = self.base.add("FACE", 8)
        calls = []

        def record(name, value):
            def api(_argument):
                calls.append(name)
                if isinstance(value, Exception):
                    raise value
                return value
            return api

        self.base.GetFaceArea = record("area", 12.5)
        self.base.Cog = record("center", (1.0, 2.0, 3.0))
        self.base.GetFaceOrientation = record("orientation", [1.0, 2.0, 3.0])
        self.base.BoundBox = record("bounds", [0, 0, 0, 4, 5, 6])

        for invalid_option in (1, 0, None, "true"):
            with self.subTest(invalid_option=invalid_option):
                with self.assertRaisesRegex(ValueError, "include_geometry must be a boolean"):
                    self.run_tool("face_properties", {
                        "deck": 1, "face_ids": [8], "include_geometry": invalid_option,
                    })
        self.base.deck = 2
        with self.assertRaisesRegex(ValueError, "only for the NASTRAN deck"):
            self.run_tool("face_properties", {
                "deck": 2, "face_ids": [8], "include_geometry": True,
            })
        self.assertEqual(calls, [])
        self.base.deck = 1

        valid_bounds = [0, 0, 0, 4, 5, 6]
        invalid_orientations = (
            ("tuple", (1.0, 2.0, 3.0)),
            ("wrong_length", [1.0, 2.0]),
            ("zero", [0.0, 0.0, 0.0]),
            ("boolean", [1.0, True, 0.0]),
            ("nan", [float("nan"), 1.0, 0.0]),
            ("infinity", [0.0, float("inf"), 1.0]),
        )
        for label, orientation in invalid_orientations:
            with self.subTest(orientation=label):
                self.base.GetFaceOrientation = record("orientation", orientation)
                self.base.BoundBox = record("bounds", valid_bounds)
                row = self.run_tool("face_properties", {
                    "deck": 1, "face_ids": [8], "include_geometry": True,
                })["faces"][0]
                self.assertIsNone(row["orientation"])
                self.assertIn("orientation_error", row)
                self.assertIsNone(row["unit_normal"])
                self.assertIn("unit_normal_error", row)
                self.assertEqual(row["bounds"], [0.0, 0.0, 0.0, 4.0, 5.0, 6.0])
                self.assertFalse(row["complete"])

        if hasattr(self.base, "GetFaceOrientation"):
            del self.base.GetFaceOrientation
        self.base.BoundBox = record("bounds", valid_bounds)
        missing_orientation = self.run_tool("face_properties", {
            "deck": 1, "face_ids": [8], "include_geometry": True,
        })["faces"][0]
        self.assertIsNone(missing_orientation["orientation"])
        self.assertIn("orientation_error", missing_orientation)
        self.assertIsNone(missing_orientation["unit_normal"])
        self.assertIn("unit_normal_error", missing_orientation)
        self.assertEqual(missing_orientation["bounds"], [0.0, 0.0, 0.0, 4.0, 5.0, 6.0])

        self.base.GetFaceOrientation = record("orientation", RuntimeError("orientation failed"))
        self.base.BoundBox = record("bounds", valid_bounds)
        failed_orientation = self.run_tool("face_properties", {
            "deck": 1, "face_ids": [8], "include_geometry": True,
        })["faces"][0]
        self.assertIsNone(failed_orientation["orientation"])
        self.assertIn("orientation_error", failed_orientation)
        self.assertEqual(failed_orientation["bounds"], [0.0, 0.0, 0.0, 4.0, 5.0, 6.0])

        self.base.GetFaceOrientation = record("orientation", [1.0, 2.0, 3.0])
        invalid_bounds = (
            ("missing", None),
            ("raises", RuntimeError("bounds failed")),
            ("reversed", [4, 0, 0, 0, 5, 6]),
            ("partial", [0, 0, 0, 4, float("nan"), 6]),
            ("boolean", [0, 0, 0, 4, True, 6]),
        )
        for label, bounds in invalid_bounds:
            with self.subTest(bounds=label):
                if label == "missing":
                    if hasattr(self.base, "BoundBox"):
                        del self.base.BoundBox
                else:
                    self.base.BoundBox = record("bounds", bounds)
                row = self.run_tool("face_properties", {
                    "deck": 1, "face_ids": [8], "include_geometry": True,
                })["faces"][0]
                self.assertEqual(row["orientation"], [1.0, 2.0, 3.0])
                self.assertIsNotNone(row["unit_normal"])
                self.assertIsNone(row["bounds"])
                self.assertIn("bounds_error", row)
                self.assertFalse(row["complete"])

        self.base.BoundBox = record("bounds", valid_bounds)
        self.base.GetFaceArea = record("area", -1.0)
        partial = self.run_tool("face_properties", {
            "deck": 1, "face_ids": [8], "include_geometry": True,
        })["faces"][0]
        self.assertIsNone(partial["area"])
        self.assertIn("area_error", partial)
        self.assertEqual(partial["center"], [1.0, 2.0, 3.0])
        self.assertIsNotNone(partial["unit_normal"])
        self.assertEqual(partial["bounds"], [0.0, 0.0, 0.0, 4.0, 5.0, 6.0])
        self.assertFalse(partial["complete"])

    def test_mesh_quality_executes_selected_check_without_fixing(self):
        self.base.add("SHELL", 7)

        result = self.run_tool("mesh_quality", {
            "deck": 1, "entity_type": "SHELL", "entity_ids": [7],
        })

        check = self.base.checks_created[0]
        self.assertEqual(check.calls, [{
            "exec_mode": self.base.Check.EXEC_ON_SELECTED,
            "report": self.base.Check.REPORT_NONE,
            "entities": [self.base.GetEntity(1, "SHELL", 7)],
        }])
        self.assertEqual(self.base.report.try_fix_calls, 0)
        self.assertEqual(result["reports"][0]["errors"], [
            {"deck": 1, "type": "GRID", "id": 3},
        ])
        self.assertFalse(result["auto_fix"])

    def test_unknown_autofix_parameter_is_rejected_before_check_execution(self):
        self.base.add("SHELL", 7)

        with self.assertRaisesRegex(ValueError, "unsupported parameters.*auto_fix"):
            self.run_tool("mesh_quality", {
                "deck": 1, "entity_type": "SHELL", "entity_ids": [7],
                "auto_fix": True,
            })
        self.assertEqual(self.base.checks_created, [])
        self.assertEqual(self.base.report.try_fix_calls, 0)

    def test_type_visibility_remains_available_and_near_requires_preconditions(self):
        result = self.run_tool("entity_visibility", {
            "deck": 1, "action": "show_only", "entity_type": "FACE",
        })
        self.assertTrue(result["applied"])
        self.assertEqual(self.base.visibility_calls, [(1, {"all": "off", "FACE": "on"})])

        with self.assertRaisesRegex(ValueError, "missing required parameters"):
            self.run_tool("entity_visibility", {"deck": 1, "action": "near"})
        self.assertEqual(len(self.base.visibility_calls), 1)

    def test_volume_metric_requires_explicit_solid_reference(self):
        self.base.add("SOLID", 11)

        result = self.run_tool("entity_metrics", {
            "deck": 1, "action": "volume", "entity_type": "SOLID", "entity_id": 11,
        })

        self.assertEqual(result, {
            "deck": 1, "entity": {"deck": 1, "type": "SOLID", "id": 11},
            "action": "volume", "api": "ansa.base.CalcSolidVolume", "value": 25.0,
        })

    def test_mass_metric_scopes_gpwg_calculation_to_the_requested_entity(self):
        entity = self.base.add("CONM1", 14)

        result = self.run_tool("entity_metrics", {
            "deck": 1, "action": "mass", "entity_type": "CONM1", "entity_id": 14,
        })

        self.assertEqual(self.base.mass_calls, [("custom", entity)])
        self.assertEqual(result["value"], [3.5, 0.0, 0.0])

    def test_mass_metric_rejects_invalid_gpwg_results_and_missing_api(self):
        self.base.add("CONM1", 14)
        params = {
            "deck": 1, "action": "mass", "entity_type": "CONM1", "entity_id": 14,
        }
        invalid_masses = (
            ("none", None),
            ("scalar", 3.5),
            ("dict", {"x": 3.5, "y": 0.0, "z": 0.0}),
            ("wrong_length", (3.5, 0.0)),
            ("string_component", ("3.5", 0.0, 0.0)),
            ("boolean_component", (3.5, True, 0.0)),
            ("nan_component", (3.5, float("nan"), 0.0)),
            ("infinite_component", (3.5, 0.0, float("inf"))),
        )
        for label, mass in invalid_masses:
            with self.subTest(mass=label):
                with patch.object(
                    self.base, "GPWGInfo", return_value=types.SimpleNamespace(mass=mass),
                ):
                    with self.assertRaisesRegex(RuntimeError, "GPWGInfo mass"):
                        self.run_tool("entity_metrics", params)

        for label, result in (("none", None), ("missing_mass", types.SimpleNamespace())):
            with self.subTest(result=label):
                with patch.object(self.base, "GPWGInfo", return_value=result):
                    with self.assertRaisesRegex(RuntimeError, "GPWGInfo returned no mass result"):
                        self.run_tool("entity_metrics", params)

        with patch.object(self.base, "GPWGInfo", None):
            with self.assertRaisesRegex(
                inspection.CapabilityUnavailableError, "ansa.base.GPWGInfo is missing",
            ):
                self.run_tool("entity_metrics", params)


if __name__ == "__main__":
    unittest.main()
