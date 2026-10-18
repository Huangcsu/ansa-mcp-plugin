"""Unit checks for read-only ABAQUS surface member inspection."""

import json
import types
import unittest
from unittest.mock import patch

from ansa_mcp_bridge.operations.abaqus_surfaces import surface_members


class FakeEntity:
    def __init__(self, entity_id, entity_type, fields=None, card_labels=None, oriented=""):
        self._id = entity_id
        self._type = entity_type
        self.fields = dict(fields or {})
        self.labels = list(card_labels if card_labels is not None else self.fields)
        self.oriented = oriented
        self.members = []
        self.events = []

    def ansa_type(self, deck):
        self.events.append(("ansa_type", deck))
        return self._type

    def card_fields(self, deck):
        self.events.append(("card_fields", deck))
        return list(self.labels)


class FakeBase:
    def __init__(self):
        self.entities = {}
        self.collection_calls = []
        self.card_calls = []
        self.attribute_calls = []
        self.yielded_members = 0

    def add(self, entity):
        self.entities[(entity._type, entity._id)] = entity
        return entity

    def GetEntity(self, deck, entity_type, entity_id):
        del deck
        return self.entities.get((entity_type, entity_id))

    def GetEntityCardValues(self, deck, entity, fields):
        self.card_calls.append((entity, tuple(fields)))
        entity.events.append(("card_values", tuple(fields)))
        values = {"__id__": entity._id, "__type__": entity._type}
        if entity._type == "SOLIDFACET":
            values["__type__"] = "SOLID"  # Officially documented SOLIDFACET exception.
        values.update(entity.fields)
        return {field: values[field] for field in fields if field in values}

    def CollectEntities(self, deck, containers, search_types, recursive=False,
                         filter_visible=False, prop_from_entities=False,
                         mat_from_entities=False, model_browser_filter=None,
                         no_expand_types=(), hidden_entities=True):
        if type(model_browser_filter) is not dict:
            raise TypeError("CollectEntities: model_browser_filter must be a dict.")
        del filter_visible, prop_from_entities, mat_from_entities
        del model_browser_filter, hidden_entities
        self.collection_calls.append((
            deck, containers, search_types, recursive, no_expand_types,
        ))
        return list(containers.members)

    def CollectEntitiesI(self, *args, **kwargs):
        raise AssertionError("iterator loses native SOLIDFACET identity")

    def GetEntityAttributeFromSet(self, set_entity, entity, attribute_name):
        self.attribute_calls.append((set_entity, entity, attribute_name))
        if entity._type == "SOLIDFACET":
            return "not found"
        if attribute_name != "Oriented":
            return "invalid attribute"
        return entity.oriented


class AbaqusSurfaceChecks(unittest.TestCase):
    def setUp(self):
        self.base = FakeBase()
        self.constants = types.SimpleNamespace(ABAQUS=1)
        self.set_entity = self.base.add(FakeEntity(5, "SET", {"Name": "surface"}))
        self.params = {"deck": 1, "set_id": 5}

    def add_hex8(self, element_type="HEXA"):
        nodes = (11, 12, 13, 14, 15, 16, 17, 18)
        fields = {"type": element_type}
        fields.update({f"G{index}": node for index, node in enumerate(nodes, 1)})
        labels = ("type",) + tuple(f"G{index}" for index in range(1, 9))
        return self.base.add(FakeEntity(10, "SOLID", fields, labels))

    def add_facet(self, nodes, card_labels=None, oriented=""):
        fields = {"EID": "10", "PID": "2"}
        fields.update({f"G{index}": str(node) for index, node in enumerate(nodes, 1)})
        default_labels = (
            "type", "EID", "PID", "G1", "G2", "G3", "G4",
            "G5", "G6", "G7", "G8",
        )
        return FakeEntity(
            10, "SOLIDFACET", fields,
            card_labels if card_labels is not None else default_labels,
            oriented,
        )

    def add_shell(self, entity_id, oriented, shape="QUAD"):
        fields = {
            "type": shape, "ID": str(entity_id), "G1": "1", "G2": "2",
            "G3": "3", "G4": "4",
        }
        return FakeEntity(
            entity_id, "SHELL", fields,
            ("type", "ID", "G1", "G2", "G3", "G4"), oriented,
        )

    def call(self, params=None, deck=1):
        return surface_members(
            self.base, self.constants, deck,
            dict(self.params if params is None else params),
        )

    def test_reads_mixed_direct_members_and_keeps_distinct_faces_with_repeated_id(self):
        self.add_hex8()
        shell_spos = self.add_shell(1, "SPOS")
        shell_sneg = self.add_shell(1, "SNEG")
        facet_s3 = self.add_facet((11, 12, 16, 15))
        duplicate_s3 = self.add_facet((11, 12, 16, 15))
        facet_s4 = self.add_facet((12, 13, 17, 16))
        self.set_entity.members = [shell_spos, facet_s3, duplicate_s3, facet_s4, shell_sneg]

        first = self.call({"deck": 1, "set_id": 5, "offset": 0, "limit": 3})
        second = self.call({"deck": 1, "set_id": 5, "offset": 3, "limit": 3})

        self.assertEqual(self.base.collection_calls[0][2:], (
            "__ALL_ENTITIES__", False, ("SET",),
        ))
        self.assertEqual(first["status"], "ok")
        self.assertFalse(first["complete"])
        self.assertTrue(first["all_members_readable"])
        self.assertEqual(first["total"], 4)
        self.assertTrue(first["has_more"])
        self.assertFalse(second["complete"])
        self.assertTrue(second["all_members_readable"])
        members = first["members"] + second["members"]
        facets = [row for row in members if row["type"] == "SOLIDFACET"]
        shells = [row for row in members if row["type"] == "SHELL"]
        self.assertEqual({row["face_number"] for row in facets}, {"S3", "S4"})
        self.assertEqual({row["parent_element_id"] for row in facets}, {10})
        self.assertEqual({tuple(row["node_ids"]) for row in facets}, {
            (11, 12, 16, 15), (12, 13, 17, 16),
        })
        self.assertEqual({row["side"] for row in facets}, {"S3", "S4"})
        self.assertEqual({row["direction"] for row in facets}, {"S3", "S4"})
        self.assertTrue(all(row["oriented"] is None for row in facets))
        self.assertEqual({row["direction"] for row in shells}, {"SPOS", "SNEG"})
        self.assertEqual(len({json.dumps(row["key"], sort_keys=True) for row in facets}), 2)
        self.assertEqual(first["duplicate_members_removed"], 1)
        self.assertTrue(all("__type__" not in fields for _entity, fields in self.base.card_calls))
        self.assertTrue(all(call[2] == "Oriented" for call in self.base.attribute_calls))
        self.assertTrue(all(entity._type == "SHELL" for _set_entity, entity, _attr
                            in self.base.attribute_calls))
        self.assertEqual(first["not_provided"], [
            "node_coordinates", "distance_check", "coverage_check",
        ])

        full_page = self.call({"deck": 1, "set_id": 5, "limit": 10})
        self.assertEqual(full_page["status"], "ok")
        self.assertTrue(full_page["complete"])
        self.assertTrue(full_page["all_members_readable"])

    def test_higher_order_hexa_is_not_misreported_as_hex8(self):
        parent = self.add_hex8()
        parent.fields["G9"] = 19
        parent.labels.append("G9")
        self.set_entity.members = [self.add_facet((11, 12, 16, 15))]
        result = self.call()
        self.assertEqual(result["status"], "partial")
        self.assertIn("higher-order HEXA", result["members"][0]["errors"][0])

    def test_confirmed_empty_set_is_distinct_from_failure(self):
        result = self.call()

        self.assertEqual(result["status"], "empty")
        self.assertTrue(result["complete"])
        self.assertTrue(result["direct_membership"])
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["members"], [])

    def test_unsupported_deck_is_explicit(self):
        result = self.call({"deck": 2, "set_id": 5}, deck=2)

        self.assertEqual(result["status"], "unsupported")
        self.assertFalse(result["complete"])
        self.assertIsNone(result["total"])
        self.assertEqual(self.base.collection_calls, [])

    def test_unrecognized_solid_type_returns_partial_with_actual_connectivity(self):
        self.add_hex8("TETRA")
        self.set_entity.members = [self.add_facet((11, 12, 16, 15))]

        result = self.call()

        self.assertEqual(result["status"], "partial")
        self.assertFalse(result["complete"])
        self.assertEqual(result["total"], 1)
        member = result["members"][0]
        self.assertEqual(member["parent_element_type"], "TETRA")
        self.assertEqual(member["node_ids"], [11, 12, 16, 15])
        self.assertIsNone(member["face_number"])
        self.assertIn("outside the supported HEXA topology scope", member["errors"][0])

    def test_card_fields_are_checked_before_ordinary_facet_fields_are_read(self):
        self.add_hex8()
        incomplete_labels = (
            "type", "EID", "PID", "G1", "G2", "G3", "G5", "G6", "G7", "G8",
        )
        facet = self.add_facet((11, 12, 16, 15), incomplete_labels)
        self.set_entity.members = [facet]

        result = self.call()

        ordinary_calls = [fields for entity, fields in self.base.card_calls if entity is facet]
        self.assertEqual(ordinary_calls, [("__id__",)])
        self.assertEqual(result["status"], "partial")
        self.assertFalse(result["complete"])
        self.assertIsNone(result["total"])
        self.assertIn("card_fields is missing required field(s): G4", result["members"][0]["errors"])

    def test_hex8_mapping_uses_parent_node_identity_for_s3_and_s4(self):
        self.add_hex8()
        self.set_entity.members = [
            self.add_facet((11, 12, 16, 15)),
            self.add_facet((12, 13, 17, 16)),
        ]

        result = self.call()

        self.assertEqual(result["total"], 2)
        self.assertEqual({row["face_number"] for row in result["members"]}, {"S3", "S4"})
        self.assertTrue(result["complete"])

    def test_probed_hexa_facet_connectivity_maps_to_s1_and_s2(self):
        self.add_hex8()
        self.set_entity.members = [
            self.add_facet((14, 13, 12, 11)),
            self.add_facet((18, 15, 16, 17)),
        ]

        result = self.call()

        facets = result["members"]
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["complete"])
        self.assertEqual({row["parent_element_type"] for row in facets}, {"HEXA"})
        self.assertEqual({row["face_number"] for row in facets}, {"S1", "S2"})
        self.assertEqual({row["side"] for row in facets}, {"S1", "S2"})
        self.assertEqual({row["direction"] for row in facets}, {"S1", "S2"})
        self.assertTrue(all(row["oriented"] is None for row in facets))

    def test_unsupported_shell_shape_keeps_member_identity_and_marks_partial(self):
        self.set_entity.members = [self.add_shell(3, "SPOS", "TRIA")]

        result = self.call()

        self.assertEqual(result["status"], "partial")
        self.assertFalse(result["complete"])
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["members"][0]["key"], {
            "entity_type": "SHELL", "entity_id": 3, "oriented": "SPOS",
        })
        self.assertIn("outside the supported four-node QUAD scope",
                      result["members"][0]["errors"][0])

    def test_nested_set_is_returned_without_expansion_and_marks_partial(self):
        nested = FakeEntity(6, "SET", {"Name": "nested"})
        nested.members = [self.add_shell(3, "SPOS")]
        self.set_entity.members = [nested]

        result = self.call()

        self.assertEqual(result["status"], "partial")
        self.assertFalse(result["complete"])
        self.assertFalse(result["all_members_readable"])
        self.assertEqual(result["total"], 1)
        self.assertEqual([row["id"] for row in result["members"]], [6])
        self.assertIn("nested SET member is not expanded",
                      result["members"][0]["errors"][0])
        self.assertEqual(self.base.collection_calls[0][1], self.set_entity)

    def test_streaming_member_scan_stops_at_budget_and_reports_partial(self):
        self.set_entity.members = [
            self.add_shell(1, "SPOS"),
            self.add_shell(2, "SNEG"),
            self.add_shell(3, "SPOS"),
        ]

        with patch(
            "ansa_mcp_bridge.operations.abaqus_surfaces._DIRECT_MEMBER_SCAN_LIMIT", 2
        ):
            result = self.call({"deck": 1, "set_id": 5, "limit": 1})

        self.assertEqual(self.base.collection_calls[0][2:], (
            "__ALL_ENTITIES__", False, ("SET",),
        ))
        self.assertEqual(result["scanned_members"], 2)
        self.assertEqual(result["scan_limit"], 2)
        self.assertTrue(result["scan_limit_reached"])
        self.assertEqual(result["status"], "partial")
        self.assertFalse(result["complete"])
        self.assertFalse(result["all_members_readable"])
        self.assertIsNone(result["total"])
        self.assertIsNone(result["has_more"])
        self.assertIn("budget before iterator exhaustion", result["errors"][-1]["error"])

    def test_nested_expansion_inherits_explicit_surface_orientation(self):
        self.set_entity.fields["ORIENTATION"] = "SNEG"
        self.set_entity.labels.append("ORIENTATION")
        nested = self.base.add(FakeEntity(6, "SET", {"Name": "nested"}))
        nested.members = [self.add_shell(3, " ")]
        self.set_entity.members = [nested]
        result = self.call({"deck": 1, "set_id": 5, "expand_nested": True})
        self.assertTrue(result["complete"])
        self.assertTrue(result["expanded_nested_sets"])
        self.assertFalse(result["direct_membership"])
        self.assertEqual(result["members"][0]["side"], "SNEG")

    def test_nested_cycle_is_partial_not_empty(self):
        self.set_entity.members = [self.set_entity]
        result = self.call({"deck": 1, "set_id": 5, "expand_nested": True})
        self.assertEqual(result["status"], "partial")
        self.assertIsNone(result["total"])
        self.assertIn("cyclic nested SET", str(result["errors"]))

    def test_shell_section_expansion_inherits_surface_side_and_checks_pid(self):
        self.set_entity.fields["ORIENTATION"] = "SNEG"
        self.set_entity.labels.append("ORIENTATION")
        section = self.base.add(FakeEntity(7, "SHELL_SECTION", oriented="not found"))
        shell = self.add_shell(3, "not found")
        shell.fields["PID"] = 7
        shell.labels.append("PID")
        section.members = [shell]
        self.set_entity.members = [section]

        result = self.call({"deck": 1, "set_id": 5, "expand_nested": True})

        self.assertTrue(result["complete"], result)
        self.assertEqual(result["members"][0]["side"], "SNEG")
        self.assertEqual(result["members"][0]["id"], 3)
        self.assertIn("PID-verified", result["scope"]["shell_properties"])
        self.assertIn((1, section, "SHELL", False, ()), self.base.collection_calls)

        section.oriented = "SPOS"
        explicit = self.call({"deck": 1, "set_id": 5, "expand_nested": True})
        self.assertTrue(explicit["complete"], explicit)
        self.assertEqual(explicit["members"][0]["side"], "SPOS")

        shell.fields["PID"] = 8
        escaped = self.call({"deck": 1, "set_id": 5, "expand_nested": True})
        self.assertFalse(escaped["complete"])
        self.assertEqual(escaped["status"], "partial")
        self.assertIn("escaped section 7", str(escaped["errors"]))

    def test_invalid_or_unbounded_page_size_is_rejected(self):
        for value in (0, 201, True, "100"):
            with self.subTest(limit=value):
                with self.assertRaises(ValueError):
                    self.call({"deck": 1, "set_id": 5, "limit": value})


if __name__ == "__main__":
    unittest.main()
