"""Unit checks for guarded Part and direct SET operations."""

import sys
import types
import unittest
from unittest.mock import patch

from ansa_mcp_bridge.operations import structures


class FakeEntity:
    def __init__(self, entity_id, entity_type, fields=None):
        self._id = entity_id
        self._type = entity_type
        self.fields = dict(fields or {})
        self.members = []

    def ansa_type(self):
        return self._type

    def card_fields(self, _deck):
        return list(self.fields)


class FakeBase:
    def __init__(self):
        self.deck = 1
        self.entities = {}
        self.next_id = 100
        self.new_part_calls = []
        self.add_to_set_calls = []
        self.collection_calls = []

    def add(self, entity_type, entity_id, **fields):
        entity = FakeEntity(entity_id, entity_type, fields)
        self.entities[(entity_type, entity_id)] = entity
        return entity

    def CurrentDeck(self):
        return self.deck

    def GetEntity(self, _deck, entity_type, entity_id):
        return self.entities.get((entity_type, entity_id))

    def GetEntityCardValues(self, _deck, entity, fields):
        values = {"__id__": entity._id, "__type__": entity._type}
        values.update(entity.fields)
        return {field: values[field] for field in fields if field in values}

    def NewPart(self, name, module_id=""):
        self.new_part_calls.append((name, module_id))
        self.next_id += 1
        return self.add("ANSAPART", self.next_id, **{"Name": name, "Module Id": module_id})

    def CollectEntities(self, deck, containers, search_types, recursive=False, no_expand_types=()):
        self.collection_calls.append((deck, containers, search_types, recursive, no_expand_types))
        if search_types != "__ALL_ENTITIES__" or recursive is not False:
            raise AssertionError("the operation must request direct members")
        return [member for original in containers.members
                for member in (original.members if original._type == "SET" and "SET" not in no_expand_types
                               else [original])]

    def AddToSet(self, set_entity, entities, as_excluded=False, as_intersected=False):
        self.add_to_set_calls.append((set_entity, list(entities), as_excluded, as_intersected))
        if as_excluded or as_intersected:
            return 0
        set_entity.members.extend(entities)
        return 1


class FakeState:
    def __init__(self):
        self.assertions = 0

    def assert_main(self):
        self.assertions += 1


class StructuresChecks(unittest.TestCase):
    def setUp(self):
        self.base = FakeBase()
        self.state = FakeState()
        self.ansa = types.ModuleType("ansa")
        self.ansa.base = self.base
        self.module_patch = patch.dict(sys.modules, {"ansa": self.ansa})
        self.module_patch.start()
        self.addCleanup(self.module_patch.stop)

    def run_tool(self, tool, params):
        return structures.execute(self.state, tool, params)

    def make_set(self, entity_id=1, name="target"):
        return self.base.add("SET", entity_id, Name=name)

    def test_part_create_reads_back_name_and_identity(self):
        result = self.run_tool("part_create", {
            "name": "Boom", "module_id": "M-1", "deck": 1,
        })

        self.assertEqual(self.base.new_part_calls, [("Boom", "M-1")])
        self.assertEqual(result, {
            "entity": {"deck": 1, "type": "ANSAPART", "id": 101},
            "name": "Boom", "module_id": "M-1",
        })
        with patch.object(self.base, "NewPart", return_value=self.base.add(
                "ANSAPART", 200, **{"Name": "Wrong module", "Module Id": "ignored"})):
            with self.assertRaisesRegex(RuntimeError, "module ID"):
                self.run_tool("part_create", {"name": "Wrong module", "module_id": "M-2"})

    def test_set_members_reads_direct_members_and_enforces_bound(self):
        target = self.make_set()
        grid = self.base.add("GRID", 2, X1=1.0)
        nested = self.make_set(3, "nested")
        nested.members.append(self.base.add("GRID", 4, X1=2.0))
        target.members.extend((grid, nested))

        result = self.run_tool("set_members", {"set_id": 1})

        self.assertEqual(result["members"], [
            {"deck": 1, "type": "GRID", "id": 2},
            {"deck": 1, "type": "SET", "id": 3},
        ])
        self.assertTrue(result["direct_membership"])
        self.assertEqual(self.base.collection_calls[0][1:], (target, "__ALL_ENTITIES__", False, ("SET",)))

        target.members = [self.base.add("GRID", entity_id, X1=0.0)
                          for entity_id in range(10, 10 + structures._MAX_SET_MEMBERS + 1)]
        with self.assertRaisesRegex(ValueError, "direct membership exceeds"):
            self.run_tool("set_members", {"set_id": 1})

    def test_set_add_validates_snapshot_and_confirms_exact_union(self):
        target = self.make_set()
        old = self.base.add("GRID", 2, X1=1.0)
        new = self.base.add("GRID", 3, X1=2.0)
        target.members.append(old)

        result = self.run_tool("set_add", {
            "set_id": 1,
            "expected_fields": {"Name": "target"},
            "expected_members": [{"deck": 1, "type": "GRID", "id": 2}],
            "entities": [{
                "type": "GRID", "id": 3, "expected_fields": {"X1": 2.0},
            }],
        })

        self.assertEqual(result["members"], [
            {"deck": 1, "type": "GRID", "id": 2},
            {"deck": 1, "type": "GRID", "id": 3},
        ])
        self.assertTrue(result["membership_verified"])
        self.assertEqual(len(self.base.add_to_set_calls), 1)
        self.assertEqual(self.base.add_to_set_calls[0][1:], ([new], False, False))

    def test_set_add_refuses_unreadable_or_stale_membership_before_write(self):
        target = self.make_set()
        new = self.base.add("GRID", 2, X1=2.0)
        self.base.CollectEntities = None
        with self.assertRaisesRegex(NotImplementedError, "direct SET membership cannot be verified"):
            self.run_tool("set_add", {
                "set_id": 1, "expected_fields": {"Name": "target"},
                "expected_members": [],
                "entities": [{"type": "GRID", "id": 2, "expected_fields": {"X1": 2.0}}],
            })
        self.assertEqual(self.base.add_to_set_calls, [])

        del self.base.CollectEntities
        target.members.append(new)
        with self.assertRaisesRegex(RuntimeError, "membership changed"):
            self.run_tool("set_add", {
                "set_id": 1, "expected_fields": {"Name": "target"},
                "expected_members": [],
                "entities": [{"type": "GRID", "id": 2, "expected_fields": {"X1": 2.0}}],
            })
        self.assertEqual(self.base.add_to_set_calls, [])


if __name__ == "__main__":
    unittest.main()
