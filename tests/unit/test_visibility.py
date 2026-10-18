"""Focused checks for the NASTRAN FACE/SHELL visibility operations."""

import sys
import types
import unittest
from unittest.mock import patch

from ansa_mcp_bridge.operations import inspection, visibility


class FakeEntity:
    def __init__(self, entity_type, entity_id):
        self._type = entity_type
        self._id = entity_id


class FakeBase:
    def __init__(self):
        self.entities = {}
        self.visible = {"FACE": {1}, "SHELL": {10}}
        self.calls = []
        self.collect_calls = []
        self.fail_shell_after_write = False
        self.raise_after_write_api = None
        self.change_identity_after_write = False
        self.bad_collection = None
        self.add("FACE", 1)
        self.add("FACE", 2)
        self.add("FACE", 3)
        self.add("SHELL", 10)
        self.add("SHELL", 11)

    def add(self, entity_type, entity_id):
        entity = FakeEntity(entity_type, entity_id)
        self.entities[(entity_type, entity_id)] = entity
        return entity

    def GetEntity(self, deck, entity_type, entity_id):
        return self.entities.get((entity_type, entity_id))

    def GetEntityCardValues(self, deck, entity, fields):
        metadata = {"__id__": entity._id, "__type__": entity._type}
        return {field: metadata[field] for field in fields if field in metadata}

    def CollectEntities(self, deck, containers, search_types, recursive=False,
                        filter_visible=False):
        entity_type = search_types
        self.collect_calls.append((entity_type, filter_visible))
        if self.fail_shell_after_write and self.calls and entity_type == "SHELL":
            raise RuntimeError("late SHELL readback failed")
        if self.bad_collection == (entity_type, filter_visible):
            return {"invalid": "collection"}
        entities = [entity for (kind, _), entity in self.entities.items()
                    if kind == entity_type]
        if filter_visible:
            entities = [entity for entity in entities
                        if entity._id in self.visible[entity_type]]
        if self.bad_collection == (entity_type, filter_visible, "duplicate") and entities:
            entities.append(entities[0])
        return entities

    def _write(self, name, entities, apply_on_contents, new_visible):
        selected = list(entities)
        self.calls.append((name, selected, apply_on_contents))
        if selected:
            entity_type = selected[0]._type
            self.visible[entity_type] = set(new_visible)
        if self.change_identity_after_write:
            self.add("FACE", 99)
            self.change_identity_after_write = False
        if self.raise_after_write_api == name:
            raise RuntimeError(f"{name} raised after changing visibility")
        return 1

    def Or(self, entities, apply_on_contents=True):
        selected = list(entities)
        entity_type = selected[0]._type
        return self._write("Or", selected, apply_on_contents,
                           {entity._id for entity in selected})

    def And(self, entities, apply_on_contents=True):
        selected = list(entities)
        entity_type = selected[0]._type
        return self._write("And", selected, apply_on_contents,
                           self.visible[entity_type] | {entity._id for entity in selected})

    def Not(self, entities, apply_on_contents=True):
        selected = list(entities)
        entity_type = selected[0]._type
        return self._write("Not", selected, apply_on_contents,
                           self.visible[entity_type] - {entity._id for entity in selected})

    def Near(self, radius, dense_search=False):
        self.calls.append(("Near", radius, dense_search))
        return 0

    def Neighb(self, steps):
        self.calls.append(("Neighb", steps))
        self.visible["SHELL"].add(11)
        return 0


class FakeState:
    def __init__(self):
        self.main_checks = 0

    def assert_main(self):
        self.main_checks += 1


class VisibilityChecks(unittest.TestCase):
    def setUp(self):
        self.base = FakeBase()
        self.constants = types.SimpleNamespace(NASTRAN=1)
        self.state = FakeState()
        ansa = types.ModuleType("ansa")
        ansa.base = self.base
        ansa.constants = self.constants
        self.module_patch = patch.dict(sys.modules, {"ansa": ansa})
        self.module_patch.start()
        self.addCleanup(self.module_patch.stop)

    @staticmethod
    def expected(face=(1,), shell=(10,)):
        return {"FACE": list(face), "SHELL": list(shell)}

    def request(self, action, **values):
        return {"deck": 1, "action": action, **values}

    def test_snapshot_is_sorted_read_only_strict_and_bounded(self):
        self.base.visible = {"FACE": {3, 1}, "SHELL": {11}}
        result = visibility.execute(self.state, "visibility_snapshot", {"deck": 1})
        self.assertEqual(result["monitored_types"], ["FACE", "SHELL"])
        self.assertEqual(result["ids"], {"FACE": [1, 2, 3], "SHELL": [10, 11]})
        self.assertEqual(result["visible_ids"], {"FACE": [1, 3], "SHELL": [11]})
        self.assertIs(result["read_only"], True)
        self.assertEqual(self.base.collect_calls, [
            ("FACE", False), ("FACE", True), ("SHELL", False), ("SHELL", True)
        ])
        self.assertEqual(self.state.main_checks, 1)

        for invalid_mode in ("wrong_type", "duplicate", "missing_visible"):
            with self.subTest(invalid_mode=invalid_mode):
                base = FakeBase()
                if invalid_mode == "wrong_type":
                    base.bad_collection = ("FACE", False)
                elif invalid_mode == "duplicate":
                    base.bad_collection = ("FACE", False, "duplicate")
                else:
                    base.add("FACE", 99)
                    base.visible["FACE"].add(99)
                    original_collect = base.CollectEntities

                    def omit_visible_from_all(deck, containers, kind, recursive=False,
                                              filter_visible=False):
                        values = original_collect(deck, containers, kind, recursive,
                                                  filter_visible)
                        if kind == "FACE" and not filter_visible:
                            return [item for item in values if item._id != 99]
                        return values

                    base.CollectEntities = omit_visible_from_all
                with self.assertRaises(RuntimeError):
                    visibility.snapshot_base(base, 1, {"deck": 1})

        oversized = FakeBase()
        oversized.entities = {}
        for entity_id in range(1, 5002):
            oversized.add("FACE", entity_id)
        for entity_id in range(1, 5002):
            oversized.add("SHELL", entity_id)
        with self.assertRaisesRegex(RuntimeError, "exceeds 10000"):
            visibility.snapshot_base(oversized, 1, {"deck": 1})

    def test_explicit_actions_call_and_verify_the_documented_set_apis(self):
        result = visibility.execute(self.state, "entity_visibility", self.request(
            "show_also", entity_type="FACE", entity_ids=[2],
            expected_visible=self.expected(),
        ))
        self.assertTrue(result["ok"])
        self.assertEqual(result["api"], "ansa.base.And")
        self.assertEqual(result["added_visible_ids"]["FACE"], [2])

        result = visibility.execute_base(self.base, 1, self.request(
            "hide", entity_type="FACE", entity_ids=[1],
            expected_visible=self.expected(face=(1, 2)),
        ))
        self.assertTrue(result["ok"])
        self.assertEqual(result["api"], "ansa.base.Not")
        self.assertEqual(result["hidden_ids"]["FACE"], [1])

        result = visibility.execute_base(self.base, 1, self.request(
            "show_only", entity_type="SHELL", entity_ids=[11],
            expected_visible=self.expected(face=(2,), shell=(10,)),
            apply_on_contents=False,
        ))
        self.assertTrue(result["ok"])
        self.assertEqual(result["api"], "ansa.base.Or")
        self.assertEqual(result["after"]["visible_ids"]["SHELL"], [11])
        self.assertEqual([call[0] for call in self.base.calls], ["And", "Not", "Or"])
        self.assertFalse(self.base.calls[-1][2])

    def test_stale_missing_duplicate_boolean_and_unknown_inputs_never_write(self):
        cases = [
            ("stale", {"entity_type": "FACE", "entity_ids": [2],
                       "expected_visible": self.expected(face=(2,))}),
            ("missing", {"entity_type": "FACE", "entity_ids": [999],
                         "expected_visible": self.expected()}),
            ("duplicate", {"entity_type": "FACE", "entity_ids": [2, 2],
                            "expected_visible": self.expected()}),
            ("boolean", {"entity_type": "FACE", "entity_ids": [True],
                          "expected_visible": self.expected()}),
            ("unknown", {"entity_type": "FACE", "entity_ids": [2],
                         "expected_visible": self.expected(), "unexpected": True}),
        ]
        for label, values in cases:
            with self.subTest(label=label):
                with self.assertRaises((ValueError, LookupError)):
                    visibility.execute_base(
                        self.base, 1, self.request("show_also", **values)
                    )
                self.assertEqual(self.base.calls, [])

        with self.assertRaises(inspection.CapabilityUnavailableError):
            visibility.execute_base(self.base, 2, {
                "deck": 2, "action": "near", "radius": 1,
                "expected_visible": self.expected(),
            })
        self.assertEqual(self.base.calls, [])

    def test_near_and_neighb_use_their_real_zero_status_and_visible_seed(self):
        near = visibility.execute_base(self.base, 1, {
            "deck": 1, "action": "near", "radius": 7.5,
            "expected_visible": self.expected(),
        })
        self.assertTrue(near["ok"])
        self.assertEqual(near["return_code"], 0)
        self.assertEqual(self.base.calls[0], ("Near", 7.5, False))
        self.assertEqual(near["added_visible_ids"], {"FACE": [], "SHELL": []})
        self.assertIn("没有观察到新增", near["message"])

        neighb = visibility.execute_base(self.base, 1, {
            "deck": 1, "action": "neighb", "steps": 2,
            "expected_visible": self.expected(),
        })
        self.assertTrue(neighb["ok"])
        self.assertEqual(neighb["return_code"], 0)
        self.assertEqual(self.base.calls[1], ("Neighb", "2"))
        self.assertEqual(neighb["added_visible_ids"]["SHELL"], [11])
        self.assertEqual([call[0] for call in self.base.calls], ["Near", "Neighb"])

    def test_api_and_partial_readback_failures_keep_observed_evidence(self):
        self.base.raise_after_write_api = "Or"
        self.base.fail_shell_after_write = True
        result = visibility.execute_base(self.base, 1, self.request(
            "show_only", entity_type="FACE", entity_ids=[2],
            expected_visible=self.expected(),
        ))
        self.assertFalse(result["ok"])
        self.assertFalse(result["applied"])
        self.assertTrue(result["possible_effects"])
        self.assertFalse(result["readback_complete"])
        self.assertEqual(result["after"]["visible_ids"]["FACE"], [2])
        self.assertEqual(result["unconfirmed_types"], ["SHELL"])
        self.assertIsNone(result["added_visible_ids"]["SHELL"])
        self.assertIn("Or raised", result["error"])
        self.assertIn("late SHELL readback failed", result["error"])

        self.base = FakeBase()
        self.base.change_identity_after_write = True
        result = visibility.execute_base(self.base, 1, self.request(
            "show_also", entity_type="FACE", entity_ids=[2],
            expected_visible=self.expected(),
        ))
        self.assertFalse(result["ok"])
        self.assertTrue(result["readback_complete"])
        self.assertTrue(result["possible_effects"])
        self.assertIn(99, result["after"]["ids"]["FACE"])
        self.assertIn("FACE identities changed", result["error"])


if __name__ == "__main__":
    unittest.main()
