"""Focused risk tests for controlled kinematics operations and schemas."""

import asyncio
import json
import math
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from ansa_mcp_bridge.operations import kinematics


class FakeEntity:
    def __init__(self, entity_id, entity_type, fields):
        self._id = entity_id
        self._type = entity_type
        self.fields = dict(fields)

    def ansa_type(self, deck=None):
        return self._type

    def card_fields(self, deck):
        return list(self.fields)


class FakeState:
    def __init__(self):
        self.assertions = 0
        self.epoch = 1
        self.write_enabled = True
        self.generation = 1
        self.connected_backend_id = "backend"

    def assert_main(self):
        self.assertions += 1

    def check_conflict(self):
        self.assert_main()

    def probe(self):
        self.assert_main()
        return {
            "pid": 1,
            "process_start": 1,
            "instance_id": "instance",
            "session_id": "session",
            "deck": 2,
            "model": {"epoch": self.epoch, "write_enabled": self.write_enabled},
            "authorization": {
                "active": True,
                "backend_id": "backend",
                "generation": self.generation,
            },
            "connected": True,
            "stopped": False,
            "persistence_blocked": False,
        }


class FakeBase:
    Entity = FakeEntity

    def __init__(self, database_path):
        self.deck = 2
        self.database_path = Path(database_path)
        self.database_path.write_bytes(b"current")
        self.entities = {}
        self.memberships = {}
        self.create_calls = []
        self.save_calls = []
        self.label_queries = []
        self.keyword_labels = {}
        self.next_id = 100
        self.add_to_body_return = 0
        self.fail_membership = False
        self.include_cog_marker_field = True
        self.cog_marker_value = None

    def add(self, entity_type, entity_id, **fields):
        entity = FakeEntity(entity_id, entity_type, fields)
        self.entities[(entity_type, entity_id)] = entity
        return entity

    def CurrentDeck(self):
        return self.deck

    def DataBaseName(self):
        return str(self.database_path)

    def SaveAs(self, path, silent=True):
        self.save_calls.append((path, silent))
        Path(path).write_bytes(b"backup")
        return 0

    def GetEntity(self, deck, entity_type, entity_id):
        if entity_type == "__PROPERTIES__":
            return next(
                (
                    entity for (kind, _), entity in self.entities.items()
                    if _ == entity_id and kind not in {"NODE", "SHELL", "SOLID"}
                ),
                None,
            )
        return self.entities.get((entity_type, entity_id))

    def GetEntityCardValues(self, deck, entity, fields):
        values = {"__id__": entity._id, "__type__": entity._type, **entity.fields}
        return {field: values[field] for field in fields if field in values}

    def SetEntityCardValues(self, deck, entity, fields):
        entity.fields.update(fields)
        if entity._type == "KIN_CONFIG" and "ACTUATOR_JOINT" in fields:
            joint = self.GetEntity(deck, "KIN_JOINT", fields["ACTUATOR_JOINT"])
            entity.fields["ACTUATOR_JOINT_TYPE"] = joint.fields["TYPE"]
        return 0

    def CollectEntities(
        self, deck, containers, search_types, recursive=False, filter_visible=False,
        hidden_entities=True, prop_from_entities=False, no_expand_types=None,
    ):
        if isinstance(containers, FakeEntity):
            if search_types == "__ALL_ENTITIES__":
                if self.fail_membership and containers._type == "KIN_BODY":
                    raise RuntimeError("injected membership read failure")
                return list(self.memberships.get(containers._id, []))
            if containers._type == "ANSAPART":
                wanted = set(self.part_contents.get(containers._id, []))
                if search_types == "__PROPERTIES__":
                    wanted = {item for item in wanted if item[0] not in {
                        "NODE", "SHELL", "SOLID",
                    }}
                return [
                    entity for (kind, entity_id), entity in self.entities.items()
                    if kind == search_types and (kind, entity_id) in wanted
                ]
        if search_types == "__PROPERTIES__":
            return [
                entity for (kind, _), entity in self.entities.items()
                if kind not in {"NODE", "SHELL", "SOLID", "KIN_MARKER",
                                "KIN_BODY", "KIN_JOINT", "KIN_CONFIG", "ANSAPART"}
            ]
        return [
            entity for (kind, _), entity in self.entities.items()
            if kind == search_types
        ]

    def GetKeywordFieldLabels(self, deck, entity_type):
        self.label_queries.append(entity_type)
        return list(self.keyword_labels.get(entity_type, []))

    def CreateEntity(self, deck, entity_type, fields, mbcontainer=None, debug=None):
        self.create_calls.append((entity_type, dict(fields)))
        self.next_id += 1
        entity_id = self.next_id
        if entity_type == "KIN_RBODY":
            body_fields = {
                "EID": entity_id,
                "GEN_TYPE": fields["GEN_TYPE"],
                "ACTIVE": "YES",
                "MORPHABLE": "NO",
                "INERTIA_MARKER": 0,
                "COG_x": 0.0,
                "COG_y": 0.0,
                "COG_z": 0.0,
                "ORIENTATION": "EULER_313",
                "PHI": 0.0,
                "THE": 0.0,
                "PSI": 0.0,
                "MASS_CALCULATION": "AUTO",
                "mass": 0.0,
                "Ixx": 1.0,
                "Ixy": 0.0,
                "Ixz": 0.0,
                "Iyy": 1.0,
                "Iyz": 0.0,
                "Izz": 1.0,
                "RESTRAINED_DOFS": "NO",
                "INITIAL_VELOCITIES": "NO",
                "Name": fields["Name"],
                "FROZEN_ID": "NO",
                "FROZEN_DELETE": "NO",
                "Comment": "",
                "MBContainer": None,
                "MBContainers": [],
            }
            if self.include_cog_marker_field:
                body_fields["COG_MARKER"] = (
                    entity_id
                    if self.cog_marker_value is None
                    else self.cog_marker_value
                )
            entity = self.add("KIN_BODY", entity_id, **body_fields)
            self.memberships[entity_id] = []
            marker = self.add("KIN_MARKER", entity_id, ID=entity_id, Name="COG")
            self.memberships[entity_id].append(marker)
        elif entity_type == "KIN_JOINT":
            entity = self.add(
                "KIN_JOINT",
                entity_id,
                **{
                    "GROUP NAME": "Default KINETIC JOINT Group",
                    "ID": entity_id,
                    "ACTIVE": fields["ACTIVE"],
                    "TYPE": fields["TYPE"],
                    "DEFINITION": "BY_BODIES",
                    "KIN_BODY_1": fields["KIN_BODY_1"],
                    "KIN_BODY_2": fields["KIN_BODY_2"],
                    "DIRECTION_TYPE": fields["DIRECTION_TYPE"],
                    "ANCHOR_x": fields["ANCHOR_x"],
                    "ANCHOR_y": fields["ANCHOR_y"],
                    "ANCHOR_z": fields["ANCHOR_z"],
                    "Z_x": fields["Z_x"],
                    "Z_y": fields["Z_y"],
                    "Z_z": fields["Z_z"],
                    "FRICTION": 0,
                    "CUR_ROT_Z": 0.0,
                    "REL_ROT_Z": 0.0,
                    "MIN_ANGLE_Z": None,
                    "MAX_ANGLE_Z": None,
                    "CONTACT_TYPE_RZ": "OFF",
                    "INITIAL_CONDITIONS": "NO",
                    "FORCES_DISPLAY": "NONE",
                    "Name": fields["Name"],
                    "FROZEN_ID": "NO",
                    "FROZEN_DELETE": "NO",
                    "Comment": "",
                    "MBContainer": None,
                    "MBContainers": [],
                },
            )
        else:
            entity = self.add(
                "KIN_CONFIG",
                entity_id,
                EID=entity_id,
                ACTUATOR_JOINT=0,
                ACTUATOR_JOINT_TYPE="NONE",
                STEP_TRA_X=1.0,
                TOTAL_TRA_X=0.0,
                STEP_TRA_Y=1.0,
                TOTAL_TRA_Y=0.0,
                STEP_TRA_Z=1.0,
                TOTAL_TRA_Z=0.0,
                STEP_ROT_X=1.0,
                TOTAL_ROT_X=0.0,
                STEP_ROT_Y=1.0,
                TOTAL_ROT_Y=0.0,
                STEP_ROT_Z=1.0,
                TOTAL_ROT_Z=0.0,
                Name=fields["Name"],
                FROZEN_ID="NO",
                FROZEN_DELETE="NO",
                Comment="",
                MBContainer=None,
                MBContainers=[],
            )
            entity.joints = []
        return entity, {}


class FakeKinetics:
    def __init__(self, base):
        self.base = base
        self.unit_calls = []
        self.parameter_calls = []
        self.mutate_old_body = None

    def AddToRigidBody(self, body, elements):
        if self.base.add_to_body_return == 0:
            self.base.memberships.setdefault(body._id, []).extend(elements)
            if self.mutate_old_body is not None:
                old_body_id, member = self.mutate_old_body
                self.base.memberships.setdefault(old_body_id, []).append(member)
        return self.base.add_to_body_return

    def AddJointToConfig(self, config, joint):
        config.joints.append(joint)
        return 0

    def SetUnits(self, **kwargs):
        self.unit_calls.append(dict(kwargs))
        return 0

    def ConfigSetParameters(self, **kwargs):
        self.parameter_calls.append(dict(kwargs))
        config = self.base.GetEntity(2, "KIN_CONFIG", kwargs["kin_config"])
        config.fields["ACTUATOR_JOINT"] = kwargs["motor_joint"]
        config.fields["ACTUATOR_JOINT_TYPE"] = "ROTATION"
        return 0

    def GetJointsFromConfig(self, config, recursive=False):
        return list(config.joints)

    def GetBodiesFromConfig(self, config, recursive=False):
        bodies = {}
        for joint in config.joints:
            for body_id in (joint.fields["KIN_BODY_1"], joint.fields["KIN_BODY_2"]):
                bodies[body_id] = self.base.GetEntity(2, "KIN_BODY", body_id)
        return list(bodies.values())

    def GetModelInfo(self, config):
        return {"rigid_bodies": 2, "joints": len(config.joints), "dofs": 1}


class KinematicsOperationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.base = FakeBase(self.root / "current.ansa")
        self.base.part_contents = {}
        self.constants = types.SimpleNamespace(ABAQUS=2, REPORT_ALL=1)
        self.kinetics = FakeKinetics(self.base)
        self.ansa = types.ModuleType("ansa")
        self.ansa.base = self.base
        self.ansa.constants = self.constants
        self.ansa.kinetics = self.kinetics
        self.module_patch = patch.dict(sys.modules, {"ansa": self.ansa})
        self.module_patch.start()
        self.addCleanup(self.module_patch.stop)
        self.state = FakeState()

    def add_body(self, body_id, name):
        body = self.base.add(
            "KIN_BODY",
            body_id,
            EID=body_id,
            GEN_TYPE="RIG_BODY",
            COG_MARKER=body_id,
            Name=name,
        )
        self.base.memberships[body_id] = []
        marker = self.base.add("KIN_MARKER", body_id, ID=body_id, Name="COG")
        self.base.memberships[body_id].append(marker)
        return body

    def run_operation(self, tool, params):
        return kinematics.execute(self.state, tool, params)

    def test_body_create_reads_members_separately_from_automatic_marker(self):
        self.base.add("NODE", 201, X=1.0, Y=2.0, Z=3.0)
        self.base.add("SHELL_SECTION", 30, MID=1, T=2.0)
        backup = self.root / "body.ansa"
        result = self.run_operation("kinematics_body_create", {
            "deck": 2,
            "name": "BOOM",
            "ground": False,
            "members": [
                {"deck": 2, "type": "NODE", "id": 201},
                {"deck": 2, "type": "SHELL_SECTION", "id": 30},
            ],
            "backup_path": str(backup),
        })

        self.assertTrue(result["ok"], result)
        self.assertFalse(result["possible_effects"])
        self.assertEqual(result["members"], [
            {"deck": 2, "type": "NODE", "id": 201},
            {"deck": 2, "type": "SHELL_SECTION", "id": 30},
        ])
        self.assertEqual([item["type"] for item in result["markers"]], ["KIN_MARKER"])
        self.assertEqual(result["body"]["card_fields"], list(
            result["body"]["fields"]
        ))
        self.assertEqual(result["body"]["entity"]["type"], "KIN_BODY")
        self.assertEqual(result["automatic_cog_marker"]["field"], "COG_MARKER")
        self.assertIs(result["automatic_cog_marker"]["available"], True)
        self.assertEqual(
            result["automatic_cog_marker"]["value"],
            result["body"]["fields"]["COG_MARKER"],
        )
        self.assertEqual(result["add_to_rigid_body_return_code"], 0)
        self.assertEqual(result["before_counts"]["KIN_BODY"], 0)
        self.assertEqual(result["after_counts"]["KIN_BODY"], 1)
        self.assertEqual(result["before_counts"]["KIN_MARKER"], 0)
        self.assertEqual(result["after_counts"]["KIN_MARKER"], 1)
        self.assertTrue(backup.is_file())
        self.assertEqual(self.base.save_calls, [(str(backup), True)])
        self.assertEqual(self.base.label_queries, ["KIN_BODY"])
        self.assertEqual(result["preflight_metadata"], {
            "available": False,
            "query_type": "KIN_BODY",
            "labels": [],
            "required_fields": ["Name", "GEN_TYPE"],
            "source": (
                "verified 2026.1 kinematic creation schema; "
                "GetKeywordFieldLabels empty"
            ),
        })

    def test_body_create_reports_missing_cog_marker_as_unavailable(self):
        self.base.include_cog_marker_field = False
        self.base.add("NODE", 201, X=1.0, Y=2.0, Z=3.0)

        result = self.run_operation("kinematics_body_create", {
            "deck": 2,
            "name": "NO_COG",
            "ground": False,
            "members": [{"deck": 2, "type": "NODE", "id": 201}],
            "backup_path": str(self.root / "no-cog.ansa"),
        })

        self.assertTrue(result["ok"], result)
        self.assertFalse(result["possible_effects"])
        self.assertNotIn("COG_MARKER", result["body"]["card_fields"])
        self.assertEqual(result["automatic_cog_marker"]["field"], "COG_MARKER")
        self.assertIs(result["automatic_cog_marker"]["available"], False)
        self.assertIn("COG_MARKER", result["automatic_cog_marker"]["reason"])
        self.assertEqual(result["members"], [
            {"deck": 2, "type": "NODE", "id": 201},
        ])

    def test_body_create_rejects_invalid_cog_marker_when_field_is_present(self):
        self.base.cog_marker_value = 0
        self.base.add("NODE", 201, X=1.0, Y=2.0, Z=3.0)

        result = self.run_operation("kinematics_body_create", {
            "deck": 2,
            "name": "BAD_COG",
            "ground": False,
            "members": [{"deck": 2, "type": "NODE", "id": 201}],
            "backup_path": str(self.root / "bad-cog.ansa"),
        })

        self.assertFalse(result["ok"])
        self.assertTrue(result["possible_effects"])
        self.assertIn("invalid automatic COG marker", result["error"])

    def test_keyword_field_query_has_no_unknown_type_fallback(self):
        with self.assertRaisesRegex(ValueError, "unsupported kinetics keyword"):
            kinematics._preflight_keyword_fields(
                self.base, 2, "UNKNOWN_KEYWORD", ("Name",)
            )
        self.assertEqual(self.base.label_queries, [])

    def test_nonempty_keyword_metadata_still_checks_required_fields(self):
        self.base.keyword_labels["KIN_BODY"] = ["Name"]
        with self.assertRaisesRegex(
            ValueError, "KIN_BODY does not define required field"
        ):
            kinematics._preflight_keyword_fields(
                self.base, 2, "KIN_RBODY", ("Name", "GEN_TYPE")
            )
        self.assertEqual(self.base.label_queries, ["KIN_BODY"])

    def test_warm_keyword_metadata_ignores_only_blank_layout_labels(self):
        self.base.keyword_labels["KIN_BODY"] = ["", " ", "Name", "GEN_TYPE", "ROAD_REF        "]
        value = kinematics._preflight_keyword_fields(
            self.base, 2, "KIN_RBODY", ("Name", "GEN_TYPE")
        )
        self.assertTrue(value["available"])
        self.assertEqual(value["ignored_blank_labels"], 2)
        self.assertEqual(value["labels"], ["Name", "GEN_TYPE", "ROAD_REF        "])
        self.base.keyword_labels["KIN_BODY"] = ["Name", "", " "]
        with self.assertRaisesRegex(ValueError, "GEN_TYPE"):
            kinematics._preflight_keyword_fields(
                self.base, 2, "KIN_RBODY", ("Name", "GEN_TYPE")
            )

    def test_ground_body_allows_empty_members(self):
        result = self.run_operation("kinematics_body_create", {
            "deck": 2,
            "name": "GROUND",
            "ground": True,
            "members": [],
            "backup_path": str(self.root / "ground.ansa"),
        })

        self.assertTrue(result["ok"])
        self.assertEqual(result["members"], [])
        self.assertEqual(result["body"]["fields"]["GEN_TYPE"], "GROUND")
        self.assertIsNone(result["add_to_rigid_body_return_code"])

    def test_ground_members_rejected_before_backup_or_creation(self):
        with self.assertRaisesRegex(ValueError, "ground bodies must have an empty"):
            self.run_operation("kinematics_body_create", {
                "deck": 2, "name": "GROUND", "ground": True,
                "members": [{"deck": 2, "type": "NODE", "id": 201}],
                "backup_path": str(self.root / "ground-with-members.ansa"),
            })
        self.assertEqual(self.base.save_calls, [])
        self.assertEqual(self.base.create_calls, [])

    def test_large_write_evidence_keeps_receipt_inside_transport_budget(self):
        refs = [{"deck": 2, "type": "NODE", "id": index} for index in range(1, 10001)]
        result = {"ok": True, "tool": "kinematics_body_create", "deck": 2,
                  "body": {"entity": {"deck": 2, "type": "KIN_BODY", "id": 3}},
                  "members": refs, "existing_bodies_before": {"1": {"members": refs}},
                  "existing_bodies_after": {"1": {"members": refs}},
                  "backup": {"path": "retained.ansa"}, "possible_effects": False}
        bounded = kinematics._bounded_result(result)
        self.assertLessEqual(len(kinematics._encoded(bounded)), kinematics._RESULT_BUDGET)
        self.assertTrue(bounded["ok"])
        self.assertTrue(bounded["evidence_truncated"])
        self.assertEqual(bounded["body"]["entity"]["id"], 3)
        self.assertEqual(bounded["evidence_summaries"]["result.members"]["total"], 10000)
        self.assertEqual(len(bounded["full_result_sha256"]), 64)

    def test_existing_body_property_overlap_is_rejected_before_backup(self):
        body = self.add_body(1, "EXISTING")
        section = self.base.add("SHELL_SECTION", 30, MID=1, T=2.0)
        shell = self.base.add("SHELL", 301, PID=30, G1=1, G2=2, G3=3, G4=4)
        self.base.memberships[body._id].append(shell)

        with self.assertRaisesRegex(ValueError, "overlap existing KIN_BODY"):
            self.run_operation("kinematics_body_create", {
                "deck": 2,
                "name": "NEW",
                "ground": False,
                "members": [{"deck": 2, "type": "SECTION", "id": section._id}],
                "backup_path": str(self.root / "blocked.ansa"),
            })

        self.assertEqual(self.base.save_calls, [])
        self.assertEqual(self.base.create_calls, [])

    def test_old_body_membership_change_after_write_is_not_reported_as_success(self):
        node = self.base.add("NODE", 201, X=0.0, Y=0.0, Z=0.0)
        self.add_body(1, "EXISTING")
        self.kinetics.mutate_old_body = (1, node)

        result = self.run_operation("kinematics_body_create", {
            "deck": 2,
            "name": "NEW",
            "ground": False,
            "members": [{"deck": 2, "type": "NODE", "id": 201}],
            "backup_path": str(self.root / "old-body-change.ansa"),
        })

        self.assertFalse(result["ok"])
        self.assertTrue(result["possible_effects"])
        self.assertEqual(result["existing_bodies_before"]["1"]["members"], [])
        self.assertEqual(
            result["old_bodies_after"]["1"]["members"],
            [{"deck": 2, "type": "NODE", "id": 201}],
        )
        self.assertIn("old KIN_BODY membership changed", result["error"])

    def test_joint_create_normalizes_axis_and_reads_full_card(self):
        self.add_body(1, "BASE")
        self.add_body(2, "ARM")
        result = self.run_operation("kinematics_joint_create", {
            "deck": 2,
            "name": "PIVOT",
            "joint_type": "REVOLUTE",
            "body1_id": 1,
            "body2_id": 2,
            "anchor": [1.0, 2.0, 3.0],
            "axis": [1.7e308, 1.7e308, 0.0],
            "backup_path": str(self.root / "joint.ansa"),
        })

        self.assertTrue(result["ok"])
        self.assertAlmostEqual(result["axis"][0], 2.0 ** -0.5)
        self.assertAlmostEqual(result["axis"][1], 2.0 ** -0.5)
        self.assertEqual(result["axis"][2], 0.0)
        self.assertEqual(result["joint"]["fields"]["DEFINITION"], "BY_BODIES")
        self.assertEqual(result["joint"]["fields"]["KIN_BODY_1"], 1)
        self.assertEqual(result["joint"]["fields"]["KIN_BODY_2"], 2)
        self.assertAlmostEqual(result["joint"]["fields"]["Z_x"], 2.0 ** -0.5)
        self.assertEqual(result["joint"]["fields"]["Z_z"], 0.0)

    def test_config_create_sets_fixed_units_and_zero_pose_actuator(self):
        self.add_body(1, "BASE")
        self.add_body(2, "ARM")
        joint = FakeEntity(10, "KIN_JOINT", {
            "Name": "PIVOT",
            "TYPE": "REVOLUTE",
            "KIN_BODY_1": 1,
            "KIN_BODY_2": 2,
        })
        self.base.entities[("KIN_JOINT", 10)] = joint
        result = self.run_operation("kinematics_config_create", {
            "deck": 2,
            "name": "MECHANISM",
            "joint_ids": [10],
            "actuator_joint_id": 10,
            "backup_path": str(self.root / "config.ansa"),
        })

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["joint_ids"], [10])
        self.assertEqual(result["units"], {
            "length": "mm",
            "mass": "tonne",
            "force": "newton",
            "time": "sec",
            "angle": "deg",
        })
        self.assertEqual(self.kinetics.unit_calls[0]["length"], "mm")
        self.assertEqual(self.kinetics.unit_calls[0]["mass"], "tonne")
        self.assertEqual(result["config"]["entity"]["id"], 101)
        self.assertEqual(self.kinetics.parameter_calls, [])
        self.assertEqual(result["config"]["fields"]["ACTUATOR_JOINT"], 10)
        self.assertEqual(result["config"]["fields"]["ACTUATOR_JOINT_TYPE"], "REVOLUTE")
        self.assertEqual(result["set_actuator_return_code"], 0)
        self.assertIsNone(result["config_set_parameters_return_code"])

    def test_config_closure_includes_ground_omitted_by_native_body_list(self):
        ground = self.add_body(1, "GROUND")
        ground.fields["GEN_TYPE"] = "GROUND"
        arm = self.add_body(2, "ARM")
        joint_card = {"card_fields": ["KIN_BODY_1", "KIN_BODY_2"],
                      "fields": {"KIN_BODY_1": 1, "KIN_BODY_2": 2}}
        bodies, native_ids = kinematics._config_body_closure(self.base, 2, [arm], [joint_card])
        self.assertEqual([body._id for body in bodies], [1, 2])
        self.assertEqual(native_ids, [2])

    def test_inspect_config_returns_every_card_field_and_body_membership(self):
        self.add_body(1, "BASE")
        self.add_body(2, "ARM")
        joint = FakeEntity(10, "KIN_JOINT", {
            "Name": "PIVOT",
            "TYPE": "REVOLUTE",
            "KIN_BODY_1": 1,
            "KIN_BODY_2": 2,
        })
        self.base.entities[("KIN_JOINT", 10)] = joint
        config = FakeEntity(20, "KIN_CONFIG", {"Name": "MECHANISM", "EID": 20})
        config.joints = [joint]
        self.base.entities[("KIN_CONFIG", 20)] = config

        result = self.run_operation("kinematics_inspect", {
            "deck": 2,
            "config_id": 20,
            "body_ids": [],
            "joint_ids": [],
            "config_ids": [],
        })

        self.assertTrue(result["ok"])
        self.assertFalse(result["possible_effects"])
        self.assertEqual(result["configs"][0]["fields"], {
            "Name": "MECHANISM",
            "EID": 20,
        })
        self.assertEqual(len(result["bodies"]), 2)
        self.assertEqual(
            [item["entity"]["type"] for item in result["bodies"]],
            ["KIN_BODY", "KIN_BODY"],
        )
        self.assertEqual(result["bodies"][0]["markers"][0]["type"], "KIN_MARKER")
        self.assertEqual(result["model_info"], {
            "rigid_bodies": 2,
            "joints": 1,
            "dofs": 1,
        })
        json.dumps(result, allow_nan=False)

    def test_native_membership_failure_is_not_swallowed(self):
        self.base.add("NODE", 201, X=0.0, Y=0.0, Z=0.0)
        self.base.add_to_body_return = 1
        backup = self.root / "failed-member.ansa"

        result = self.run_operation("kinematics_body_create", {
            "deck": 2,
            "name": "BROKEN",
            "ground": False,
            "members": [{"deck": 2, "type": "NODE", "id": 201}],
            "backup_path": str(backup),
        })

        self.assertFalse(result["ok"])
        self.assertTrue(result["possible_effects"])
        self.assertIsNotNone(result["body_id"])
        self.assertIn("AddToRigidBody", result["error"])
        self.assertIsNotNone(result["after_counts"])
        self.assertFalse(result["after_counts_unknown"])
        self.assertEqual(result["new_entity"]["entity"]["id"], result["body_id"])
        self.assertEqual(result["new_members"], [])
        self.assertEqual(result["new_markers"][0]["type"], "KIN_MARKER")
        self.assertFalse(result["old_bodies_after_unknown"])
        self.assertTrue(backup.is_file())

    def test_failure_readback_distinguishes_unknown_from_empty_membership(self):
        self.base.add("NODE", 201, X=0.0, Y=0.0, Z=0.0)
        self.base.add_to_body_return = 1
        self.base.fail_membership = True

        result = self.run_operation("kinematics_body_create", {
            "deck": 2,
            "name": "UNKNOWN",
            "ground": False,
            "members": [{"deck": 2, "type": "NODE", "id": 201}],
            "backup_path": str(self.root / "unknown-members.ansa"),
        })

        self.assertFalse(result["ok"])
        self.assertTrue(result["possible_effects"])
        self.assertIsNone(result["new_members"])
        self.assertTrue(result["new_members_unknown"])
        self.assertIn("membership read failure", result["new_members_error"])
        self.assertEqual(result["new_entity"]["entity"]["id"], result["body_id"])


class KinematicsSchemaTests(unittest.TestCase):
    def setUp(self):
        from mcp.server.fastmcp import FastMCP
        from ansa_mcp_plugin.mcp.kinematics import register
        from ansa_mcp_plugin.mcp.schemas import ToolResult

        self.submitted = []

        def submit(tool, params, operation_id, target, model, ttl):
            self.submitted.append((tool, params, operation_id, target, model, ttl))
            return ToolResult(
                ok=True,
                status="queued",
                operation_id=operation_id,
                target=target,
                model=model.model_dump(),
            ).as_mcp()

        self.server = FastMCP("kinematics-schema")
        register(self.server, submit)
        self.target = {
            "backend_id": "b",
            "instance_id": "i",
            "session_id": "s",
            "generation": 1,
        }
        self.base_args = {
            "operation_id": "kinematics",
            "expected_target": self.target,
            "expected_model": {"epoch": 1},
            "deck": 2,
        }

    def call(self, name, args):
        return asyncio.run(self.server.call_tool(name, {**self.base_args, **args}))

    def test_schema_rejects_invalid_ground_joint_axis_and_inspect_selection(self):
        from mcp.server.fastmcp.exceptions import ToolError

        invalid = (
            (
                "ansa_kinematics_body_create",
                {
                    "name": "BODY",
                    "ground": False,
                    "members": [],
                    "backup_path": str(Path(tempfile.gettempdir()) / "body.ansa"),
                },
            ),
            (
                "ansa_kinematics_body_create",
                {
                    "name": "BODY",
                    "ground": "false",
                    "members": [{"deck": 2, "type": "NODE", "id": 201}],
                    "backup_path": str(Path(tempfile.gettempdir()) / "body.ansa"),
                },
            ),
            (
                "ansa_kinematics_joint_create",
                {
                    "name": "JOINT",
                    "joint_type": "REVOLUTE",
                    "body1_id": 1,
                    "body2_id": 2,
                    "anchor": [0.0, 0.0, 0.0],
                    "axis": [0.0, 0.0, 0.0],
                    "backup_path": str(Path(tempfile.gettempdir()) / "joint.ansa"),
                },
            ),
            (
                "ansa_kinematics_joint_create",
                {
                    "name": "JOINT",
                    "joint_type": "REVOLUTE",
                    "body1_id": 1,
                    "body2_id": 2,
                    "anchor": [True, 0.0, 0.0],
                    "axis": [1.0, 0.0, 0.0],
                    "backup_path": str(Path(tempfile.gettempdir()) / "joint.ansa"),
                },
            ),
            (
                "ansa_kinematics_joint_create",
                {
                    "name": "JOINT",
                    "joint_type": "REVOLUTE",
                    "body1_id": 1,
                    "body2_id": 2,
                    "anchor": [0.0, 0.0, 0.0],
                    "axis": ["1", 0.0, 0.0],
                    "backup_path": str(Path(tempfile.gettempdir()) / "joint.ansa"),
                },
            ),
            (
                "ansa_kinematics_inspect",
                {
                    "config_id": None,
                    "body_ids": [],
                    "joint_ids": [],
                    "config_ids": [],
                },
            ),
        )
        for name, args in invalid:
            with self.subTest(name=name):
                with self.assertRaises(ToolError):
                    self.call(name, args)
        self.assertEqual(self.submitted, [])

    def test_body_schema_submits_normalized_member_contract(self):
        self.call("ansa_kinematics_body_create", {
            "name": "BODY",
            "ground": False,
            "members": [{"deck": 2, "type": "NODE", "id": 201}],
            "backup_path": str(Path(tempfile.gettempdir()) / "body-valid.ansa"),
        })

        self.assertEqual(len(self.submitted), 1)
        tool, params, operation_id, target, model, ttl = self.submitted[0]
        self.assertEqual(tool, "kinematics_body_create")
        self.assertEqual(params["members"], [{"deck": 2, "type": "NODE", "id": 201}])
        self.assertEqual(operation_id, "kinematics")
        self.assertEqual(target.backend_id, "b")
        self.assertEqual(model.epoch, 1)
        self.assertEqual(ttl, 300)

    def test_joint_schema_normalizes_axis_without_overflow(self):
        self.call("ansa_kinematics_joint_create", {
            "name": "JOINT",
            "joint_type": "REVOLUTE",
            "body1_id": 1,
            "body2_id": 2,
            "anchor": [1, 2, 3],
            "axis": [1.7e308, 1.7e308, 0.0],
            "backup_path": str(Path(tempfile.gettempdir()) / "joint-valid.ansa"),
        })

        params = self.submitted[0][1]
        self.assertAlmostEqual(params["axis"][0], 2.0 ** -0.5)
        self.assertAlmostEqual(params["axis"][1], 2.0 ** -0.5)
        self.assertEqual(params["axis"][2], 0.0)
        self.assertTrue(all(math.isfinite(item) for item in params["axis"]))


if __name__ == "__main__":
    unittest.main()
