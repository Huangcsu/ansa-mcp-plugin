"""Risk-based checks for ABAQUS contact cards and node-sampled geometry."""

import math
import types
import unittest

import json

from pydantic import TypeAdapter, ValidationError

from ansa_mcp_shared.framing import MAX_FRAME_BYTES
from ansa_mcp_bridge.operations.abaqus_contact import (
    inspect_relationships,
    quad4_metrics,
)
from ansa_mcp_plugin.mcp.contacts import ContactIds, Tolerance


DECK = 9

TIE_FIELDS = [
    "id", "SECONDARY", "SSID", "MAIN", "MSID", "REGION TYPE_S", "TRIM_S",
    "ORIENT_S", "REGION TYPE_M", "TRIM_M", "ORIENT_M", "PARAM", "DISTANCE",
    "ADJUST", "ROTATION", "CYCLIC SYMMETRY", "TYPE_tie_coefficients",
    "NO THICKNESS", "Name", "FROZEN_ID", "FROZEN_DELETE", "DEFINED",
    "AUXILIARY", "Comment", "MBContainer", "MBContainers",
]
CONTACT_PAIR_FIELDS = [
    "id", "STEP", "TYPE", "INTERFERENCE", "SECONDARY", "SSID", "INTERACTION",
    "MAIN", "MSID", "REGION TYPE_S", "TRIM_S", "OFFSET_S", "THICK_S",
    "MAX RATIO_S", "SCL.THICK_S", "ORIENT_S", "REGION TYPE_M", "TRIM_M",
    "OFFSET_M", "THICK_M", "MAX RATIO_M", "SCL.THICK_M", "ORIENT_M", "HCRIT",
    "EXT.ZONE", "SMOOTH", "ADJUST", "TIED", "SMALL SLIDING",
    "MECHANICAL CONSTRAINT", "MINIMUM DISTANCE", "GEOM. CORRECTION", "WEIGHT",
    "TYPE_tie_coefficients", "MIDFACE NODES", "NO THICKNESS", "CONSTR.POS.",
    "SUPPL.CONST.", "TRACKING", "Name", "FROZEN_ID", "FROZEN_DELETE", "DEFINED",
    "AUXILIARY", "Comment", "MBContainer", "MBContainers",
]
INTERACTION_FIELDS = [
    "THICKNESS", "PAD THICKNESS", "TRACKING THICKNESS", "*FRICTION", "FRICT_ID",
    "*BLOCKAGE", "*SURF_BEHAVIOR", "*COHESIVE BEHAVIOR", "*GAP CONDUCTANCE",
    "*GAP ELECTRICAL CONDUCTANCE", "*GAP HEAT GENERATION", "*GAP RADIATION",
    "*CONTACT DAMPING", "*SPH SURFACE BEHAVIOR", "INTERACTION FRACTURE CRITERION",
    "Name", "FROZEN_DELETE", "Comment", "MBContainer", "MBContainers",
]
FRICTION_FIELDS = [
    "ID", "OPTION", "TRAC.SLOPE", "EXP.DECAY", "ANISOTROPIC", "DEP", "M",
    "SLIP RATE", "P", "TAUMAX", "Name", "FROZEN_ID", "FROZEN_DELETE",
    "Comment", "MBContainer", "MBContainers",
]
SET_FIELDS = [
    "SID", "Output as:", "ORIENTATION", "SURF.PROP", "OUTPUT TYPE", "TRIM",
    "UNSORTED", "DUPLICATES_SET", "MEDINA OUTPUT AS ABAQUS", "BOOLEAN OPERATION",
    "Name", "FROZEN_ID", "FROZEN_DELETE", "DEFINED", "AUXILIARY", "INTERFACE",
    "START_ID", "END_ID", "INCREMENT", "NUMBERING_RULE_NAME", "ELEMENTS", "NODES",
    "MATERIALS", "PRESERVE", "FORCE", "COLOR_R", "COLOR_G", "COLOR_B",
    "TRANSPARENCY", "Labels", "Comment", "MBContainer", "MBContainers",
]
SHELL_FIELDS = [
    "type", "ID", "PID", "G1", "G2", "G3", "G4", "t1", "t2", "t3", "t4",
    "mat.orient", "mcid", "ZOFFS", "Shell Stiffness", "Name", "FROZEN_ID",
    "FROZEN_DELETE", "FROZEN", "FREEZE ON DEPENETRATION", "Comment", "MBContainer",
    "MBContainers",
]
SHELL_SECTION_FIELDS = [
    "PID", "SECTION TYPE", "MID", "CONTROLS", "ORIENT", "DENSITY", "POISSON",
    "INTEGRATION", "TEMPERATURE", "Offset Type", "T", "TYPE", "optional1",
    "optional2", "*TRANSVERSE SHEAR STIFFNESS", "*HOURGLASS STIFFNESS", "*REBAR LAYER",
    "Name", "FROZEN_ID", "FROZEN_DELETE", "DEFINED", "TRIM", "USE_IN_MODEL",
    "INTERFACE", "START_ID", "END_ID", "INCREMENT", "NUMBERING_RULE_NAME",
    "SUBOPTION_NODES", "SUBOPTION_ELEMENTS", "SUBOPTION_MATERIALS", "PRESERVE", "FORCE",
    "COLOR_R", "COLOR_G", "COLOR_B", "TRANSPARENCY", "RENDER_MAT_ID",
    "drawing/PerPidDrawing", "drawing/Shadow", "drawing/Wire", "drawing/Perimeters",
    "drawing/InFront", "User/cad_material", "User/cad_thickness", "Hardening K",
    "Hardening n", "Initial Thickness", "Friction Coefficient", "r_0", "r_45", "r_90",
    "Forming Limit Curve", "Labels", "Num.Elem", "Comment", "MBContainer", "MBContainers",
]


class FakeEntity:
    def __init__(self, entity_type, entity_id, labels, values, *, members=None, oriented=""):
        self.entity_type = entity_type
        self._id = entity_id
        self.labels = list(labels)
        self.values = dict(values)
        self.members = list(members or [])
        self.oriented = oriented

    def ansa_type(self, deck):
        assert deck == DECK
        return self.entity_type

    def card_fields(self, deck):
        assert deck == DECK
        return list(self.labels)


class FakeBase:
    def __init__(self):
        self.entities = {}
        self.card_calls = []

    def add(self, entity):
        self.entities[(entity.entity_type, entity._id)] = entity
        return entity

    def GetEntity(self, deck, entity_type, entity_id):
        assert deck == DECK
        return self.entities.get((entity_type, entity_id))

    def GetEntityCardValues(self, deck, entity, fields):
        assert deck == DECK
        requested = tuple(fields)
        self.card_calls.append((entity, requested))
        values = {"__id__": entity._id, "__type__": entity.entity_type}
        values.update(entity.values)
        return {field: values[field] for field in requested if field in values}

    def CollectEntities(self, deck, containers, search_types, recursive=False, **kwargs):
        assert deck == DECK
        assert search_types == "__ALL_ENTITIES__"
        assert recursive is False
        return list(containers.members)

    def GetEntityAttributeFromSet(self, set_entity, entity, attribute_name):
        assert attribute_name == "Oriented"
        return entity.oriented


def _card_values(labels, overrides):
    values = {label: None for label in labels}
    values.update(overrides)
    return values


class ContactFixture:
    def __init__(self):
        self.base = FakeBase()
        self.constants = types.SimpleNamespace(ABAQUS=DECK)

    def add_node(self, node_id, xyz):
        return self.base.add(FakeEntity(
            "NODE", node_id, ["ID", "X", "Y", "Z"],
            {"ID": node_id, "X": xyz[0], "Y": xyz[1], "Z": xyz[2]},
        ))

    def add_shell(
        self, element_id, node_ids, section_id, *, z=0.0, zoffs=0.0, xy_coords=None,
    ):
        if xy_coords is None:
            xy_coords = ((0, 0), (20, 0), (20, 10), (0, 10))
        for node_id, xy in zip(node_ids, xy_coords):
            self.add_node(node_id, (xy[0], xy[1], z))
        values = _card_values(SHELL_FIELDS, {
            "type": "QUAD", "ID": element_id, "PID": section_id,
            **{f"G{i}": node_id for i, node_id in enumerate(node_ids, 1)},
            "t1": 0.0, "t2": 0.0, "t3": 0.0, "t4": 0.0,
            "mat.orient": "mcid", "ZOFFS": zoffs,
        })
        return self.base.add(FakeEntity("SHELL", element_id, SHELL_FIELDS, values))

    def add_section(self, section_id, *, offset_type, offset=None):
        labels = list(SHELL_SECTION_FIELDS)
        if offset is not None:
            labels.insert(labels.index("T"), "OFFSET")
        values = _card_values(labels, {
            "PID": section_id, "SECTION TYPE": "SECTION", "MID": 1,
            "Offset Type": offset_type, "T": 2.0, "TYPE": "S_", "optional1": "R",
            "optional2": "", "Name": f"SECTION_{section_id}",
        })
        if offset is not None:
            values["OFFSET"] = offset
        return self.base.add(FakeEntity("SHELL_SECTION", section_id, labels, values))

    def add_set(self, set_id, name, orientation, members):
        values = _card_values(SET_FIELDS, {
            "SID": set_id, "Output as:": "Surface", "ORIENTATION": orientation,
            "SURF.PROP": "NO", "OUTPUT TYPE": "ELEMENT", "Name": name,
            "FROZEN_ID": "NO", "FROZEN_DELETE": "NO", "DEFINED": "YES",
        })
        return self.base.add(FakeEntity("SET", set_id, SET_FIELDS, values, members=members))

    def add_shell_surface(
        self, set_id, name, orientation, *, count, element_start, node_start, section_id,
    ):
        members = []
        for index in range(count):
            first_node = node_start + index * 4
            members.append(self.add_shell(
                element_start + index,
                (first_node, first_node + 1, first_node + 2, first_node + 3),
                section_id,
            ))
        return self.add_set(set_id, name, orientation, members)

    def add_tie(self, tie_id, master_set, slave_set):
        values = _card_values(TIE_FIELDS, {
            "id": tie_id, "SECONDARY": "ELEMENT", "SSID": slave_set,
            "MAIN": "ELEMENT", "MSID": master_set, "ORIENT_S": "SNEG",
            "ORIENT_M": "SPOS", "PARAM": "POS_TOLER", "DISTANCE": 3.0,
            "ADJUST": "NO", "CYCLIC SYMMETRY": "NO", "NO THICKNESS": " ",
            "Name": "FIXTURE_TIE", "FROZEN_ID": "NO", "FROZEN_DELETE": "NO",
            "DEFINED": "YES", "AUXILIARY": "NO", "Comment": "",
            "MBContainer": None, "MBContainers": [],
        })
        return self.base.add(FakeEntity("TIE", tie_id, TIE_FIELDS, values))

    def add_hex_face_set(self, set_id):
        # HEXA bottom face nodes 1-4 are a 20x10 rectangle at z=0.
        coords = (
            (0, 0, 0), (20, 0, 0), (20, 10, 0), (0, 10, 0),
            (0, 0, 1), (20, 0, 1), (20, 10, 1), (0, 10, 1),
        )
        for node_id, xyz in enumerate(coords, 1):
            self.add_node(100 + node_id, xyz)
        solid_values = {"type": "HEXA", **{f"G{i}": 100 + i for i in range(1, 9)}}
        solid_labels = ["type", *(f"G{i}" for i in range(1, 9))]
        self.base.add(FakeEntity("SOLID", 800, solid_labels, solid_values))
        facet_labels = ["EID", "PID", "G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8"]
        facet_values = {"EID": 800, "PID": 80, "G1": 101, "G2": 102,
                        "G3": 103, "G4": 104, "G5": None, "G6": None,
                        "G7": None, "G8": None}
        facet = self.base.add(FakeEntity("SOLIDFACET", 800, facet_labels, facet_values))
        return self.add_set(set_id, "HEXA_TOP", "", [facet])


class AbaqusContactChecks(unittest.TestCase):
    def test_quad4_metrics_and_degenerate_rejection(self):
        area, normal = quad4_metrics(((0, 0, 0), (20, 0, 0), (20, 10, 0), (0, 10, 0)))
        self.assertEqual(area, 200.0)
        self.assertEqual(normal, (0.0, 0.0, 1.0))
        with self.assertRaisesRegex(ValueError, "degenerate"):
            quad4_metrics(((0, 0, 0), (1, 0, 0), (2, 0, 0), (3, 0, 0)))

    def test_tie_reads_every_native_card_field_and_reports_node_sample_metrics(self):
        fixture = ContactFixture()
        shell_master = fixture.add_shell(101, (1, 2, 3, 4), 11, zoffs=0.0)
        shell_slave = fixture.add_shell(102, (5, 6, 7, 8), 12, zoffs=0.0)
        fixture.add_section(11, offset_type="SPOS")
        fixture.add_section(12, offset_type="SNEG")
        fixture.add_set(1, "MASTER", "SPOS", [shell_master])
        fixture.add_set(2, "SLAVE", "SNEG", [shell_slave])
        tie = fixture.add_tie(4, 1, 2)

        result = inspect_relationships(fixture.base, fixture.constants, DECK, {
            "deck": DECK, "relation_type": "TIE", "ids": [4], "tolerance": 0.5,
        })

        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["complete"])
        relation = result["relationships"][0]
        self.assertEqual(relation["master"], {
            "set_id": 1, "member_type": "ELEMENT", "orientation": "SPOS",
        })
        self.assertEqual(relation["slave"]["set_id"], 2)
        self.assertEqual(relation["tie_tolerance"]["solver_position_tolerance"], 3.0)
        self.assertEqual(result["tolerance"], 0.5)
        self.assertEqual(
            relation["geometry_comparison"]["reference_gap"]["slave_to_master_node_distances"]["maximum_distance"],
            0.0,
        )
        self.assertEqual(relation["geometry_comparison"]["reference_gap"]["slave_node_projection_coverage"]["ratio"], 1.0)
        self.assertEqual(
            relation["geometry_comparison"]["uniform_section_physical_gap"]["slave_to_master_node_distances"]["maximum_distance"],
            0.0,
        )
        self.assertIn("not Abaqus solver TIE coverage or area coverage", result["coverage_method"])
        master_face = next(s for s in result["surfaces"] if s["set_id"] == 1)["faces"][0]
        self.assertEqual(master_face["area"], 200.0)
        self.assertEqual(master_face["unit_normal"], [0.0, 0.0, 1.0])
        self.assertEqual(master_face["shell_thickness"]["section_value"], 2.0)
        self.assertEqual(master_face["section_offset"]["type"], "SPOS")
        self.assertTrue(master_face["section_offset"]["used_for_uniform_physical_geometry"])
        self.assertEqual(master_face["element_offset"]["status"], "explicit_zero")
        master_physical = master_face["uniform_section_physical_nodes"]["SPOS"]
        self.assertTrue(master_physical["available"])
        self.assertEqual(master_physical["offset_distance_along_positive_normal"], 0.0)
        self.assertTrue(all(node["xyz"][2] == 0.0 for node in master_physical["node_coordinates"]))
        tie_calls = [fields for entity, fields in fixture.base.card_calls if entity is tie]
        self.assertIn(tuple(TIE_FIELDS), tie_calls)
        self.assertEqual(relation["card"]["fields"], TIE_FIELDS)
        self.assertEqual(relation["card"]["values"]["NO THICKNESS"], " ")

    def test_reference_and_physical_gaps_use_symbolic_and_numeric_offsets(self):
        fixture = ContactFixture()
        low = fixture.add_shell(111, (11, 12, 13, 14), 11, z=0.0, zoffs=0.0)
        high = fixture.add_shell(112, (15, 16, 17, 18), 12, z=3.0, zoffs=0.0)
        fixture.add_section(11, offset_type="SPOS")
        fixture.add_section(12, offset_type="", offset=0.25)
        fixture.add_set(11, "LOW", "SPOS", [low])
        fixture.add_set(12, "HIGH", "SNEG", [high])
        fixture.add_tie(7, 11, 12)

        result = inspect_relationships(fixture.base, fixture.constants, DECK, {
            "deck": DECK, "relation_type": "TIE", "ids": [7], "tolerance": 0.25,
        })

        self.assertEqual(result["status"], "ok")
        comparison = result["relationships"][0]["geometry_comparison"]
        self.assertEqual(comparison["reference_gap"]["slave_to_master_node_distances"]["minimum_distance"], 3.0)
        self.assertEqual(
            comparison["uniform_section_physical_gap"]["slave_to_master_node_distances"]["minimum_distance"],
            1.5,
        )
        low_face = next(surface for surface in result["surfaces"] if surface["set_id"] == 11)["faces"][0]
        high_face = next(surface for surface in result["surfaces"] if surface["set_id"] == 12)["faces"][0]
        self.assertEqual(low_face["section_offset"]["offset_ratio"], 0.5)
        self.assertEqual(low_face["uniform_section_physical_nodes"]["SPOS"]["offset_distance_along_positive_normal"], 0.0)
        self.assertEqual(high_face["section_offset"]["offset_ratio"], 0.25)
        self.assertEqual(high_face["uniform_section_physical_nodes"]["SNEG"]["offset_distance_along_positive_normal"], -1.5)
        self.assertTrue(all(node["xyz"][2] == 1.5 for node in high_face["uniform_section_physical_nodes"]["SNEG"]["node_coordinates"]))

    def test_contact_pair_reads_interaction_friction_and_hexa_face(self):
        fixture = ContactFixture()
        master_set = fixture.add_hex_face_set(30)
        shell = fixture.add_shell(202, (21, 22, 23, 24), 22, z=3.0, zoffs=0.0)
        fixture.add_section(22, offset_type="", offset=0.25)
        fixture.add_set(20, "SECONDARY", "SNEG", [shell])
        contact_values = _card_values(CONTACT_PAIR_FIELDS, {
            "id": 5, "STEP": 0, "TYPE": "CONTACT PAIR", "INTERFERENCE": "NO",
            "SECONDARY": "ELEMENT", "SSID": 20, "INTERACTION": 7,
            "MAIN": "ELEMENT", "MSID": 30, "ORIENT_M": "SPOS", "ORIENT_S": "SNEG",
            "TIED": "NO", "SMALL SLIDING": "NO", "GEOM. CORRECTION": 0,
            "TYPE_tie_coefficients": "SURFACE TO SURFACE", "NO THICKNESS": " ",
            "Name": "FIXTURE_CONTACT", "FROZEN_ID": "NO", "FROZEN_DELETE": "NO",
            "DEFINED": "YES", "AUXILIARY": "NO", "Comment": "",
            "MBContainer": None, "MBContainers": [],
        })
        contact = fixture.base.add(FakeEntity("CONTACT_PAIR", 5, CONTACT_PAIR_FIELDS, contact_values))
        interaction_values = _card_values(INTERACTION_FIELDS, {
            "THICKNESS": None, "PAD THICKNESS": None, "TRACKING THICKNESS": None,
            "*FRICTION": "YES", "FRICT_ID": 1, "*BLOCKAGE": "NO",
            "*SURF_BEHAVIOR": "NO", "*COHESIVE BEHAVIOR": "NO",
            "*GAP CONDUCTANCE": "NO", "*GAP ELECTRICAL CONDUCTANCE": "NO",
            "*GAP HEAT GENERATION": "NO", "*GAP RADIATION": "NO",
            "*CONTACT DAMPING": "NO", "*SPH SURFACE BEHAVIOR": "NO",
            "INTERACTION FRACTURE CRITERION": "NO", "Name": "FRIC",
            "FROZEN_DELETE": "NO", "Comment": "", "MBContainer": None,
            "MBContainers": [],
        })
        interaction = fixture.base.add(FakeEntity(
            "SURFACE_INTERACTION", 7, INTERACTION_FIELDS, interaction_values,
        ))
        friction_values = _card_values(FRICTION_FIELDS, {
            "ID": 1, "OPTION": " ", "TRAC.SLOPE": None, "EXP.DECAY": "NO",
            "ANISOTROPIC": "NO", "DEP": "NO", "M": 0.2,
            "SLIP RATE": None, "P": None, "TAUMAX": None, "Name": "FRIC",
            "FROZEN_ID": "NO", "FROZEN_DELETE": "NO", "Comment": "",
            "MBContainer": None, "MBContainers": [],
        })
        friction = fixture.base.add(FakeEntity("FRICTION", 1, FRICTION_FIELDS, friction_values))

        result = inspect_relationships(fixture.base, fixture.constants, DECK, {
            "deck": DECK, "relation_type": "CONTACT_PAIR", "ids": [5], "tolerance": 2.0,
        })

        relation = result["relationships"][0]
        self.assertEqual(relation["master"]["set_id"], 30)
        self.assertEqual(relation["slave"]["set_id"], 20)
        self.assertEqual(relation["interaction"]["surface_interaction"]["card"]["values"]["*FRICTION"], "YES")
        self.assertEqual(relation["interaction"]["friction"]["values"]["M"], 0.2)
        comparison = relation["geometry_comparison"]
        self.assertEqual(comparison["reference_gap"]["slave_to_master_node_distances"]["minimum_distance"], 3.0)
        physical = comparison["uniform_section_physical_gap"]
        self.assertEqual(physical["slave_to_master_node_distances"]["minimum_distance"], 1.5)
        self.assertTrue(physical["complete"])
        self.assertFalse(physical["solver_equivalence_claim"])
        hex_face = next(s for s in result["surfaces"] if s["set_id"] == 30)["faces"][0]
        self.assertEqual(hex_face["member_type"], "SOLIDFACET")
        self.assertEqual(hex_face["face_number"], "S1")
        self.assertEqual(hex_face["area"], 200.0)
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["complete"])
        for entity, expected in ((contact, CONTACT_PAIR_FIELDS),
                                 (interaction, INTERACTION_FIELDS),
                                 (friction, FRICTION_FIELDS)):
            calls = [fields for current, fields in fixture.base.card_calls if current is entity]
            self.assertIn(tuple(expected), calls)
        self.assertTrue(master_set.values["Output as:"] == "Surface")

    def test_null_or_blank_element_offset_is_unset_and_allows_section_geometry(self):
        for zoffs in (None, " "):
            with self.subTest(zoffs=zoffs):
                fixture = ContactFixture()
                master = fixture.add_shell(301, (31, 32, 33, 34), 31, zoffs=0.0)
                slave = fixture.add_shell(302, (35, 36, 37, 38), 32, z=3.0, zoffs=zoffs)
                fixture.add_section(31, offset_type="SPOS")
                fixture.add_section(32, offset_type="", offset=0.25)
                fixture.add_set(31, "MASTER", "SPOS", [master])
                fixture.add_set(32, "SLAVE", "SNEG", [slave])
                fixture.add_tie(6, 31, 32)

                result = inspect_relationships(fixture.base, fixture.constants, DECK, {
                    "deck": DECK, "relation_type": "TIE", "ids": [6], "tolerance": 0.5,
                })

                self.assertEqual(result["status"], "ok")
                self.assertTrue(result["complete"])
                slave_face = next(s for s in result["surfaces"] if s["set_id"] == 32)["faces"][0]
                element_offset = slave_face["element_offset"]
                self.assertEqual(element_offset["status"], "unset")
                self.assertIsNone(element_offset["raw_value"])
                self.assertFalse(element_offset["override_applied"])
                self.assertTrue(element_offset["resolved"])
                self.assertTrue(element_offset["supported_for_physical_geometry"])
                comparison = result["relationships"][0]["geometry_comparison"]
                self.assertEqual(
                    comparison["reference_gap"]["slave_to_master_node_distances"]["minimum_distance"],
                    3.0,
                )
                self.assertEqual(
                    comparison["uniform_section_physical_gap"]["slave_to_master_node_distances"]["minimum_distance"],
                    1.5,
                )

    def test_missing_or_unsupported_element_offset_never_becomes_zero(self):
        cases = (
            ("missing_card_field", "unavailable"),
            ("missing_api_value", "unavailable"),
            ("nonnumeric", "unavailable"),
            ("nonzero", "unsupported_nonzero"),
            ("nonzero_tiny", "unsupported_nonzero"),
        )
        for case, expected_status in cases:
            with self.subTest(case=case):
                fixture = ContactFixture()
                master = fixture.add_shell(311, (51, 52, 53, 54), 51, zoffs=0.0)
                slave = fixture.add_shell(312, (55, 56, 57, 58), 52, z=0.2, zoffs=0.0)
                if case == "missing_card_field":
                    master.labels.remove("ZOFFS")
                elif case == "missing_api_value":
                    del master.values["ZOFFS"]
                elif case == "nonnumeric":
                    master.values["ZOFFS"] = "not-a-number"
                elif case == "nonzero":
                    master.values["ZOFFS"] = 0.1
                else:
                    master.values["ZOFFS"] = 1.0e-13
                fixture.add_section(51, offset_type="SPOS")
                fixture.add_section(52, offset_type="SNEG")
                fixture.add_set(51, "MASTER", "SPOS", [master])
                fixture.add_set(52, "SLAVE", "SNEG", [slave])
                fixture.add_tie(9, 51, 52)

                result = inspect_relationships(fixture.base, fixture.constants, DECK, {
                    "deck": DECK, "relation_type": "TIE", "ids": [9], "tolerance": 0.5,
                })

                self.assertEqual(result["status"], "partial")
                comparison = result["relationships"][0]["geometry_comparison"]
                self.assertTrue(comparison["reference_gap"]["complete"])
                self.assertFalse(comparison["uniform_section_physical_gap"]["available"])
                master_face = next(s for s in result["surfaces"] if s["set_id"] == 51)["faces"][0]
                element_offset = master_face["element_offset"]
                self.assertEqual(element_offset["status"], expected_status)
                self.assertFalse(element_offset["supported_for_physical_geometry"])
                if case.startswith("missing"):
                    self.assertFalse(element_offset["resolved"])
                    self.assertIsInstance(element_offset["raw_value"], dict)
                elif case.startswith("nonzero"):
                    self.assertTrue(element_offset["resolved"])
                    self.assertTrue(element_offset["override_applied"])
                else:
                    self.assertFalse(element_offset["resolved"])
                    self.assertIsNone(element_offset["override_applied"])

    def test_absent_numeric_section_offset_stays_unknown_but_reference_gap_remains_available(self):
        fixture = ContactFixture()
        master = fixture.add_shell(401, (41, 42, 43, 44), 41, zoffs=0.0)
        slave = fixture.add_shell(402, (45, 46, 47, 48), 42, z=0.4, zoffs=0.0)
        fixture.add_section(41, offset_type="SPOS")
        fixture.add_section(42, offset_type="")
        fixture.add_set(41, "MASTER", "SPOS", [master])
        fixture.add_set(42, "SLAVE", "SNEG", [slave])
        fixture.add_tie(8, 41, 42)

        result = inspect_relationships(fixture.base, fixture.constants, DECK, {
            "deck": DECK, "relation_type": "TIE", "ids": [8], "tolerance": 0.5,
        })

        self.assertEqual(result["status"], "partial")
        comparison = result["relationships"][0]["geometry_comparison"]
        self.assertTrue(comparison["reference_gap"]["complete"])
        self.assertEqual(comparison["reference_gap"]["slave_to_master_node_distances"]["minimum_distance"], 0.4)
        physical = comparison["uniform_section_physical_gap"]
        self.assertEqual(physical["status"], "unavailable")
        self.assertFalse(physical["available"])
        slave_face = next(surface for surface in result["surfaces"] if surface["set_id"] == 42)["faces"][0]
        self.assertEqual(slave_face["section_offset"]["status"], "unknown")
        self.assertFalse(slave_face["uniform_section_physical_nodes"]["SNEG"]["available"])

    def test_global_face_and_utf8_budgets_bound_large_contact_reply(self):
        fixture = ContactFixture()
        fixture.add_section(90, offset_type="SPOS")
        relation_ids = []
        for index in range(20):
            master_id = 100 + index * 2
            slave_id = master_id + 1
            fixture.add_shell_surface(
                master_id, f"MASTER_{index}", "SPOS", count=20,
                element_start=10000 + index * 40, node_start=100000 + index * 160,
                section_id=90,
            )
            fixture.add_shell_surface(
                slave_id, f"SLAVE_{index}", "SNEG", count=20,
                element_start=10020 + index * 40, node_start=100080 + index * 160,
                section_id=90,
            )
            relation_id = 500 + index
            fixture.add_tie(relation_id, master_id, slave_id)
            relation_ids.append(relation_id)

        result = inspect_relationships(fixture.base, fixture.constants, DECK, {
            "deck": DECK, "relation_type": "TIE", "ids": relation_ids, "tolerance": 0.5,
        })

        encoded = json.dumps(
            result, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
        summary = result["face_record_summary"]
        self.assertEqual(summary["limit"], 200)
        self.assertEqual(summary["included_count"], 200)
        self.assertEqual(summary["omitted_count"], 600)
        self.assertRegex(summary["omitted_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(sum(surface["face_count"] for surface in result["surfaces"]), 800)
        self.assertEqual(sum(len(surface["faces"]) for surface in result["surfaces"]), 200)
        self.assertLessEqual(len(encoded), MAX_FRAME_BYTES * 3 // 4)
        self.assertEqual(result["status"], "partial")
        self.assertFalse(result["complete"])

    def test_projection_budget_is_shared_and_reuses_repeated_pair_stats(self):
        fixture = ContactFixture()
        fixture.add_section(91, offset_type="SPOS")
        fixture.add_shell_surface(
            91, "MASTER", "SPOS", count=60, element_start=20000,
            node_start=200000, section_id=91,
        )
        fixture.add_shell_surface(
            92, "SLAVE", "SNEG", count=60, element_start=20100,
            node_start=201000, section_id=91,
        )
        fixture.add_tie(601, 91, 92)
        fixture.add_tie(602, 91, 92)

        result = inspect_relationships(fixture.base, fixture.constants, DECK, {
            "deck": DECK, "relation_type": "TIE", "ids": [601, 602], "tolerance": 0.5,
        })

        budget = result["projection_budget"]
        self.assertEqual(budget["limit"], 100000)
        self.assertEqual(budget["used"], budget["limit"])
        self.assertTrue(budget["exhausted"])
        self.assertEqual(budget["cache_hits"], 4)
        self.assertEqual(result["status"], "partial")
        self.assertTrue(all(not relation["complete"] for relation in result["relationships"]))

    def test_concave_quad_is_rejected_as_partial_without_using_fixed_split(self):
        fixture = ContactFixture()
        fixture.add_section(93, offset_type="SPOS")
        concave = fixture.add_shell(
            701, (701, 702, 703, 704), 93,
            xy_coords=((0, 0), (0.5, 0.5), (2, 0), (0, 2)),
        )
        slave = fixture.add_shell(702, (705, 706, 707, 708), 93)
        fixture.add_set(93, "CONCAVE", "SPOS", [concave])
        fixture.add_set(94, "SLAVE", "SNEG", [slave])
        fixture.add_tie(603, 93, 94)

        result = inspect_relationships(fixture.base, fixture.constants, DECK, {
            "deck": DECK, "relation_type": "TIE", "ids": [603], "tolerance": 0.5,
        })

        surface = next(item for item in result["surfaces"] if item["set_id"] == 93)
        self.assertEqual(result["status"], "partial")
        self.assertFalse(surface["complete"])
        self.assertEqual(surface["face_count"], 0)
        self.assertTrue(any(
            "concave" in error or "convex" in error for error in surface["errors"]
        ))

    def test_partly_collinear_quad_marks_only_its_face_partial(self):
        fixture = ContactFixture()
        fixture.add_section(95, offset_type="SPOS")
        collinear = fixture.add_shell(
            801, (801, 802, 803, 804), 95,
            xy_coords=((0, 0), (1, 0), (2, 0), (0, 1)),
        )
        valid = fixture.add_shell(802, (805, 806, 807, 808), 95)
        slave = fixture.add_shell(803, (809, 810, 811, 812), 95)
        fixture.add_set(95, "MIXED", "SPOS", [collinear, valid])
        fixture.add_set(96, "SLAVE", "SNEG", [slave])
        fixture.add_tie(604, 95, 96)

        result = inspect_relationships(fixture.base, fixture.constants, DECK, {
            "deck": DECK, "relation_type": "TIE", "ids": [604], "tolerance": 0.5,
        })

        surface = next(item for item in result["surfaces"] if item["set_id"] == 95)
        self.assertEqual(result["status"], "partial")
        self.assertFalse(surface["complete"])
        self.assertEqual(surface["expansion"]["member_count"], 2)
        self.assertEqual(surface["face_count"], 1)
        self.assertEqual(surface["face_sample_count"], 1)
        self.assertTrue(any("SHELL 801 geometry unavailable" in error for error in surface["errors"]))

    def test_nonfinite_tie_options_stay_sanitized_in_strict_json_output(self):
        sanitized = {"available": False, "reason": "native value is not strict JSON"}
        for field, output_location in (("ADJUST", "options"), ("DISTANCE", "tie_tolerance")):
            with self.subTest(field=field):
                fixture = ContactFixture()
                fixture.add_section(97, offset_type="SPOS")
                master = fixture.add_shell(901, (901, 902, 903, 904), 97)
                slave = fixture.add_shell(902, (905, 906, 907, 908), 97)
                fixture.add_set(97, "MASTER", "SPOS", [master])
                fixture.add_set(98, "SLAVE", "SNEG", [slave])
                tie = fixture.add_tie(605, 97, 98)
                tie.values[field] = math.nan

                result = inspect_relationships(fixture.base, fixture.constants, DECK, {
                    "deck": DECK, "relation_type": "TIE", "ids": [605], "tolerance": 0.5,
                })

                json.dumps(result, ensure_ascii=False, allow_nan=False)
                relation = result["relationships"][0]
                self.assertEqual(result["status"], "partial")
                self.assertFalse(relation["complete"])
                self.assertFalse(relation["card"]["complete"])
                self.assertEqual(relation["card"]["values"][field], sanitized)
                self.assertEqual(relation[output_location][field if field == "ADJUST" else "distance_raw"], sanitized)

    def test_bounded_unique_ids_and_positive_finite_tolerance_schema(self):
        ids = TypeAdapter(ContactIds)
        tolerance = TypeAdapter(Tolerance)
        self.assertEqual(ids.validate_python([1, 20]), [1, 20])
        for invalid in ([1, 1], [], [0], list(range(1, 22))):
            with self.subTest(ids=invalid), self.assertRaises(ValidationError):
                ids.validate_python(invalid)
        self.assertEqual(tolerance.validate_python(0.25), 0.25)
        for invalid in (0.0, -1.0, math.inf, math.nan):
            with self.subTest(tolerance=invalid), self.assertRaises(ValidationError):
                tolerance.validate_python(invalid)

    def test_operation_rejects_non_abaqus_deck_and_bad_scope(self):
        fixture = ContactFixture()
        for params in (
            {"deck": 8, "relation_type": "TIE", "ids": [1], "tolerance": 0.1},
            {"deck": DECK, "relation_type": "CONTACT", "ids": [1], "tolerance": 0.1},
            {"deck": DECK, "relation_type": "TIE", "ids": [1, 1], "tolerance": 0.1},
            {"deck": DECK, "relation_type": "TIE", "ids": [1], "tolerance": 0.0},
        ):
            with self.subTest(params=params), self.assertRaises((TypeError, ValueError)):
                inspect_relationships(fixture.base, fixture.constants, DECK, params)


if __name__ == "__main__":
    unittest.main()
