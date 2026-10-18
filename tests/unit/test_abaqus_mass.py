import math
import types
import unittest
from unittest.mock import patch

from ansa_mcp_bridge.operations import abaqus_mass


class Entity:
    def __init__(self, kind, eid, **fields):
        self.kind, self._id, self.fields = kind, eid, fields

    def ansa_type(self, deck):
        return self.kind

    def card_fields(self, deck):
        return list(self.fields)


class Base:
    Entity = Entity

    def __init__(self):
        self.entities = {}
        self.members = {}
        self.calls = []
        self.result = [10.0, 1, 2, 3] + [0.0] * 18
        self.mat1 = self.add("MATERIAL", 1, **{"*DENSITY": "YES", "DENS": 0.01})
        self.mat2 = self.add("MATERIAL", 2, **{"*DENSITY": "YES", "DENS": 0.002})
        self.shell_section = self.add("SHELL_SECTION", 1, MID=1)
        self.solid_section = self.add("SOLID_SECTION", 2, MID=2)
        self.shell = self.add("SHELL", 11, PID=1)
        self.solid = self.add("SOLID", 22, PID=2)
        self.part = self.add("ANSAPART", 3)
        for root in (self.mat1, self.shell_section, self.part):
            self.members[(root.kind, root._id, "SHELL")] = [self.shell]
        for root in (self.mat2, self.solid_section, self.part):
            self.members[(root.kind, root._id, "SOLID")] = [self.solid]

    def add(self, kind, eid, **fields):
        item = Entity(kind, eid, **fields)
        self.entities[kind, eid] = item
        return item

    def GetEntity(self, deck, kind, eid):
        return self.entities.get((kind, eid))

    def GetEntityCardValues(self, deck, entity, fields):
        values = {"__id__": entity._id, "__type__": entity.kind, **entity.fields}
        return {k: values[k] for k in fields if k in values}

    def CollectEntities(self, deck, root, kind, **kwargs):
        if kind == "__PROPERTIES__":
            return [p for e, p in ((self.shell, self.shell_section), (self.solid, self.solid_section)) if e in root]
        return self.members.get((root.kind, root._id, kind), [])

    def CollectEntitiesI(self, deck, root, kind, **kwargs):
        yield from self.CollectEntities(deck, root, kind, **kwargs)

    def CalcElementMass(self, elements, **kwargs):
        self.calls.append((elements, kwargs))
        return self.result


class AbaqusMassChecks(unittest.TestCase):
    def setUp(self):
        self.base = Base()
        self.constants = types.SimpleNamespace(ABAQUS=4)

    def run_mass(self, *refs, **options):
        return abaqus_mass.mass_properties(self.base, self.constants, 4, {
            "deck": 4, "entities": [{"deck": 4, "type": k, "id": i} for k, i in refs], **options,
        })

    def test_material_part_overlap_is_deduplicated_and_scope_is_explicit(self):
        result = self.run_mass(("MATERIAL", 1), ("ANSAPART", 3), no_nsm=True)
        self.assertEqual(self.base.calls, [([self.base.shell, self.base.solid], {"no_nsm": True, "deck": 4})])
        self.assertEqual(result["scope"]["counts"], {"SHELL": 1, "SOLID": 1})
        self.assertEqual(result["mass"], 10.0)
        self.assertEqual(len(result["scope"]["materials"]), 2)
        self.assertTrue(result["scope"]["material_assignments_verified"])
        shell_only = self.run_mass(("ANSAPART", 3), element_types=["SHELL"])
        self.assertEqual(shell_only["scope"]["counts"], {"SHELL": 1, "SOLID": 0})
        self.assertEqual(self.base.calls[-1][0], [self.base.shell])

    def test_empty_and_undefined_material_scopes_are_not_zero_mass(self):
        with self.assertRaisesRegex(ValueError, "empty is not zero"):
            self.run_mass(("MATERIAL", 1), element_types=["SOLID"])
        self.base.mat1.fields["*DENSITY"] = "NO"
        with self.assertRaisesRegex(RuntimeError, "undefined"):
            self.run_mass(("MATERIAL", 1))
        self.assertEqual(self.base.calls, [])

    def test_invalid_native_result_fails_instead_of_reporting_success(self):
        for native in (0, None, [1.0], [math.nan] * 22, [0.0] * 22, [True] + [0.0] * 21):
            with self.subTest(native=native):
                self.base.result = native
                with self.assertRaises(RuntimeError):
                    self.run_mass(("MATERIAL", 1))

    def test_invalid_request_does_not_call_native_calculation(self):
        for options in ({"no_nsm": 1}, {"element_types": []}, {"element_types": ["SHELL", "SHELL"]}, {"auto_fix": True}):
            with self.subTest(options=options):
                with self.assertRaises((ValueError, TypeError)):
                    self.run_mass(("MATERIAL", 1), **options)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.run_mass(("MATERIAL", 1), ("MATERIAL", 1))
        self.assertEqual(self.base.calls, [])

    def test_one_element_never_expands_its_shared_section(self):
        self.base.members[("SHELL_SECTION", 1, "SHELL")] = [self.base.shell] * 20000
        with patch.object(self.base, "CollectEntities", side_effect=AssertionError("outside selected scope")):
            result = self.run_mass(("SHELL", 11))
        self.assertEqual(result["scope"]["element_count"], 1)

    def test_streaming_scan_budget_includes_duplicate_references(self):
        self.base.members[("ANSAPART", 3, "SHELL")] = [self.base.shell] * 20
        with patch.object(abaqus_mass, "_MAX_SCANNED", 2):
            with self.assertRaisesRegex(ValueError, "scanned element references"):
                self.run_mass(("ANSAPART", 3))
        self.assertEqual(self.base.calls, [])


if __name__ == "__main__":
    unittest.main()
