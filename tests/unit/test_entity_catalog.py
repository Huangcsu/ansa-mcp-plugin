"""Safety checks for generic entity queries and guarded card operations."""

import json
import sys
import types
import unittest
from unittest.mock import patch

from ansa_mcp_bridge.operations import entity_catalog


class FakeEntity:
    def __init__(self, entity_id, entity_type, fields):
        self._id = entity_id
        self._type = entity_type
        self.fields = dict(fields)

    def ansa_type(self):
        return self._type

    def card_fields(self, deck):
        return list(self.fields)


class FakeBase:
    Entity = FakeEntity

    def __init__(self):
        self.deck = 1
        self.labels = {"GRID": [], "MAT1": []}
        self.known_types = {1: ["GRID", "MAT1", "INCLUDE"],
                            2: ["NODE", "MATERIAL"]}
        self.entities = {}
        self.set_calls = 0
        self.delete_calls = 0
        self.create_calls = 0
        self.label_calls = 0
        self.ignore_create_fields = False
        self.create_id = 100

    def add(self, entity_type, entity_id, **fields):
        entity = FakeEntity(entity_id, entity_type, fields)
        self.entities[(entity_type, entity_id)] = entity
        return entity

    def CurrentDeck(self):
        return self.deck

    def GetKeywordFieldLabels(self, deck, entity_type):
        self.label_calls += 1
        return self.labels.get(entity_type)

    def TypesInCategory(self, deck, category):
        self.asserted_category = category
        return self.known_types.get(deck)

    def GetEntity(self, deck, entity_type, entity_id):
        return self.entities.get((entity_type, entity_id))

    def GetEntityCardValues(self, deck, entity, fields):
        values = {"__id__": entity._id, "__type__": entity._type}
        values.update(entity.fields)
        return {field: values[field] for field in fields if field in values}

    def CollectEntities(self, deck, containers, entity_type, recursive=False):
        if entity_type == "__ALL_ENTITIES__":
            return list(self.entities.values())
        return [entity for (kind, _), entity in self.entities.items() if kind == entity_type]

    def CreateEntity(self, deck, entity_type, fields):
        self.create_calls += 1
        self.create_id += 1
        if self.ignore_create_fields:
            fields = {}
        return self.add(entity_type, self.create_id, **fields)

    def SetEntityCardValues(self, deck, entity, fields):
        self.set_calls += 1
        entity.fields.update(fields)
        return 0

    def DeleteEntity(self, entities, force=False, compress=True):
        self.delete_calls += 1
        for entity in entities:
            self.entities.pop((entity._type, entity._id), None)
        return 0

    def NameToEnts(self, pattern, deck, match):
        return []

    def DataBaseName(self):
        return "unit.ansa"

    def BoundBox(self, entities):
        return [0, 0, 0, 1, 1, 1]


class FakeState:
    def __init__(self):
        self.assertions = 0

    def assert_main(self):
        self.assertions += 1


class EntityCatalogChecks(unittest.TestCase):
    def setUp(self):
        self.base = FakeBase()
        self.constants = types.SimpleNamespace(
            NASTRAN=1,
            ABAQUS=2,
            ENM_REGEX=1,
            ENM_EXACT=2,
            ENM_SUBSTRING=3,
            ENM_SUBSTRING_IGNORECASE=4,
        )
        self.ansa = types.ModuleType("ansa")
        self.ansa.base = self.base
        self.ansa.constants = self.constants
        self.state = FakeState()
        self.module_patch = patch.dict(sys.modules, {"ansa": self.ansa})
        self.module_patch.start()
        self.addCleanup(self.module_patch.stop)

    def run_tool(self, tool, params):
        return entity_catalog.execute(self.state, tool, params)

    def test_include_pages_have_stable_identity_order_and_optional_name(self):
        self.base.add("INCLUDE", 9, Name="parent.nas")
        self.base.add("INCLUDE", 3, Name="child.nas")
        self.base.add("INCLUDE", 7)
        first = self.run_tool("model_includes", {"limit": 2})
        self.assertEqual(first["total"], 3)
        self.assertEqual(first["includes"], [
            {"entity": {"deck": 1, "type": "INCLUDE", "id": 3}, "fields": {"Name": "child.nas"}},
            {"entity": {"deck": 1, "type": "INCLUDE", "id": 7}, "fields": {}},
        ])
        last = self.run_tool("model_includes", {"offset": 2, "limit": 2})
        self.assertEqual([row["entity"]["id"] for row in last["includes"]], [9])
        beyond = self.run_tool("model_includes", {"offset": 3, "limit": 2})
        self.assertEqual(beyond["total"], 3)
        self.assertEqual(beyond["includes"], [])

    def test_duplicate_include_identities_fail_instead_of_paging_ambiguous_rows(self):
        include = self.base.add("INCLUDE", 1, Name="duplicate.nas")
        with patch.object(self.base, "CollectEntities", return_value=[include, include]):
            with self.assertRaisesRegex(RuntimeError, "duplicate INCLUDE identities"):
                self.run_tool("model_includes", {"offset": 1, "limit": 1})

    def test_entity_list_paginates_and_caps_page_size(self):
        for entity_id in (1, 2, 3):
            self.base.add("GRID", entity_id, X1=float(entity_id), X2=0.0, X3=0.0)

        result = self.run_tool("entity_list", {
            "entity_type": "GRID", "fields": ["X1"], "offset": 1, "limit": 1,
        })

        self.assertEqual(result["total"], 3)
        self.assertEqual(result["entities"][0]["entity"]["id"], 2)
        self.assertEqual(result["entities"][0]["fields"], {"X1": 2.0})
        with self.assertRaisesRegex(ValueError, "limit must be between"):
            self.run_tool("entity_list", {"entity_type": "GRID", "limit": 201})
        self.assertEqual(self.state.assertions, 2)

    def test_stale_expected_fields_prevent_update_and_batch_delete(self):
        first = self.base.add("GRID", 1, X1=1.0, X2=0.0, X3=0.0)
        second = self.base.add("GRID", 2, X1=2.0, X2=0.0, X3=0.0)

        with self.assertRaisesRegex(RuntimeError, "changed since they were read"):
            self.run_tool("entity_update", {
                "entity_type": "GRID", "entity_id": 1,
                "expected_fields": {"X1": 99.0}, "fields": {"X1": 4.0},
            })
        self.assertEqual(self.base.set_calls, 0)
        self.assertEqual(first.fields["X1"], 1.0)

        with self.assertRaisesRegex(RuntimeError, "changed since they were read"):
            self.run_tool("entity_delete", {
                "entity_type": "GRID",
                "entities": [
                    {"entity_id": 1, "expected_fields": {"X1": 1.0}},
                    {"entity_id": 2, "expected_fields": {"X1": -1.0}},
                ],
            })
        self.assertEqual(self.base.delete_calls, 0)
        self.assertIs(self.base.GetEntity(1, "GRID", 1), first)
        self.assertIs(self.base.GetEntity(1, "GRID", 2), second)

    def test_card_serialization_preserves_vectors_and_entity_references(self):
        target = self.base.add("MAT1", 7, MID=7, E=210000.0, NU=0.3)
        self.base.add("GRID", 1, X1=0.0, X2=1.0, X3=2.0,
                      Vector=(1.0, 2.0, 3.0), Reference=target)

        result = self.run_tool("entity_get", {
            "entity_type": "GRID", "entity_id": 1,
            "fields": ["Vector", "Reference"],
        })

        self.assertEqual(result["fields"]["Vector"], [1.0, 2.0, 3.0])
        self.assertEqual(result["fields"]["Reference"], {
            "deck": 1, "type": "MAT1", "id": 7,
        })
        self.assertEqual(json.loads(json.dumps(result, allow_nan=False)), result)

    def test_empty_keyword_labels_do_not_block_count_or_claim_list_fields(self):
        count = self.run_tool("entity_count", {"entity_type": "MAT1"})
        listing = self.run_tool("entity_list", {
            "entity_type": "MAT1", "fields": ["NOT_A_FIELD"],
        })

        self.assertEqual(count["count"], 0)
        self.assertEqual(listing["entities"], [])
        self.assertFalse(listing["fields_validated"])
        self.assertEqual(self.base.label_calls, 0)

    def test_existing_mat1_and_abaqus_node_fields_come_from_entities(self):
        self.base.add("MAT1", 7, MID=7, E=210000.0, NU=0.3)
        material = self.run_tool("entity_get", {
            "entity_type": "MAT1", "entity_id": 7, "fields": ["E", "NU"],
        })
        self.assertEqual(material["fields"], {"E": 210000.0, "NU": 0.3})

        self.base.deck = self.constants.ABAQUS
        self.base.add("NODE", 8, NSET="NODES", X=1.0, Y=2.0, Z=3.0)
        node = self.run_tool("entity_get", {
            "entity_type": "NODE", "entity_id": 8, "fields": ["NSET", "X"],
        })
        self.assertEqual(node["fields"], {"NSET": "NODES", "X": 1.0})
        self.assertEqual(self.base.label_calls, 0)

    def test_update_uses_current_entity_fields_and_confirms_readback(self):
        material = self.base.add("MAT1", 7, MID=7, E=210000.0, NU=0.3)

        result = self.run_tool("entity_update", {
            "entity_type": "MAT1", "entity_id": 7,
            "expected_fields": {"NU": 0.3}, "fields": {"NU": 0.31},
        })

        self.assertEqual(result["fields"], {"NU": 0.31})
        self.assertEqual(material.fields["NU"], 0.31)
        self.assertEqual(self.base.set_calls, 1)
        self.assertEqual(self.base.label_calls, 0)

    def test_first_mat1_nonempty_create_is_refused_without_field_definition(self):
        with self.assertRaisesRegex(ValueError, r"create with fields=\{\} first"):
            self.run_tool("entity_create", {
                "entity_type": "MAT1", "fields": {"MID": 1},
            })

        self.assertEqual(self.base.create_calls, 0)
        self.assertIsNone(self.base.GetEntity(1, "MAT1", 101))

    def test_first_mat1_empty_create_uses_one_native_call_and_readback(self):
        result = self.run_tool("entity_create", {
            "entity_type": "MAT1", "fields": {},
        })

        self.assertEqual(result["entity"], {"deck": 1, "type": "MAT1", "id": 101})
        self.assertEqual(result["fields"], {})
        self.assertEqual(self.base.create_calls, 1)
        self.assertEqual(self.base.label_calls, 0)
        self.assertIsNotNone(self.base.GetEntity(1, "MAT1", 101))

    def test_create_prevalidates_against_existing_entity_card_fields(self):
        self.base.add("MAT1", 7, MID=7, E=210000.0, NU=0.3)

        result = self.run_tool("entity_create", {
            "entity_type": "MAT1", "fields": {"MID": 8, "E": 200000.0},
        })
        self.assertEqual(result["fields"], {"MID": 8, "E": 200000.0})
        self.assertEqual(self.base.create_calls, 1)

        with self.assertRaisesRegex(ValueError, "unsupported card field"):
            self.run_tool("entity_create", {
                "entity_type": "MAT1", "fields": {"NOT_A_FIELD": 1},
            })
        self.assertEqual(self.base.create_calls, 1)
        self.assertEqual(self.base.label_calls, 0)

    def test_node_coordinates_use_resolved_grid_card_fields(self):
        self.base.add("GRID", 9, X1=1.0, X2=2.0, X3=3.0)

        result = self.run_tool("node_coordinates", {"node_ids": [9]})

        self.assertEqual(result["nodes"][0]["coordinates"], [1.0, 2.0, 3.0])
        self.assertEqual(self.base.label_calls, 0)

    def test_create_readback_failure_reports_persistent_side_effect_without_retry(self):
        self.base.ignore_create_fields = True
        self.base.labels["MAT1"] = ["MID"]

        with self.assertRaisesRegex(RuntimeError, "MAT1 101.*may remain in the model"):
            self.run_tool("entity_create", {
                "entity_type": "MAT1", "fields": {"MID": 1},
            })

        self.assertEqual(self.base.create_calls, 1)
        self.assertEqual(self.base.delete_calls, 0)
        self.assertIsNotNone(self.base.GetEntity(1, "MAT1", 101))

    def test_unknown_empty_entity_type_reports_current_deck_types(self):
        with self.assertRaisesRegex(ValueError, "GRID.*MAT1"):
            self.run_tool("entity_count", {"entity_type": "NOT_A_TYPE"})

    def test_create_rejects_metadata_before_native_call(self):
        with self.assertRaisesRegex(ValueError, "must not write ANSA metadata"):
            self.run_tool("entity_create", {
                "entity_type": "MAT1", "fields": {"__id__": 10},
            })
        self.assertEqual(self.base.create_calls, 0)

    def test_unavailable_native_create_is_rejected_without_probe(self):
        self.base.CreateEntity = None

        with self.assertRaisesRegex(NotImplementedError, "no entity was created"):
            self.run_tool("entity_create", {
                "entity_type": "MAT1", "fields": {},
            })

        self.assertEqual(self.base.create_calls, 0)

    def test_unsupported_card_values_fail_instead_of_becoming_text(self):
        self.base.add("GRID", 1, X1=0.0, X2=1.0, X3=2.0,
                      Vector=object())

        with self.assertRaisesRegex(TypeError, "unsupported ANSA value type"):
            self.run_tool("entity_get", {
                "entity_type": "GRID", "entity_id": 1, "fields": ["Vector"],
            })

    def test_requested_deck_must_be_the_current_deck(self):
        with self.assertRaisesRegex(ValueError, "not the current ANSA deck"):
            self.run_tool("entity_count", {"deck": 2, "entity_type": "GRID"})


if __name__ == "__main__":
    unittest.main()
