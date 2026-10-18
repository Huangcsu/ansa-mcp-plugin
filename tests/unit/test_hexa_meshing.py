"""Risk-focused checks for guarded ABAQUS Map-volume meshing."""

import asyncio
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from ansa_mcp_bridge.operations import hexa_meshing


def _middle_node_id(x, y):
    return {
        (0, 0): 19,
        (0, 1): 20,
        (0, 2): 21,
        (1, 0): 22,
        (1, 2): 23,
        (2, 0): 24,
        (2, 1): 25,
        (2, 2): 26,
    }[(x, y)]


def _node_id(layer, x, y):
    if layer == 0:
        return 1 + x * 3 + y
    if layer == 2:
        return 10 + x * 3 + y
    if (x, y) == (1, 1):
        return 27
    return _middle_node_id(x, y)


def _coords(layer, x, y):
    return float(x * 10.0), float(y * 10.0), float(layer * 10.0)


def _quad(layer, x, y):
    return (
        _node_id(layer, x, y),
        _node_id(layer, x + 1, y),
        _node_id(layer, x + 1, y + 1),
        _node_id(layer, x, y + 1),
    )


def _cells():
    result = []
    for layer in (0, 1):
        for x in (0, 1):
            for y in (0, 1):
                result.append((*_quad(layer, x, y), *_quad(layer + 1, x, y)))
    return result


_CELLS = _cells()


class FakeEntity:
    def __init__(self, entity_type, entity_id, fields):
        self._type = entity_type
        self._id = entity_id
        self.fields = dict(fields)

    def ansa_type(self, deck):
        return self._type

    def card_fields(self, deck):
        return list(self.fields)


class FakeBase:
    Entity = FakeEntity

    def __init__(self, current_path):
        self.deck = 4
        self.database_path = Path(current_path)
        self.database_path.write_bytes(b"current model")
        self.entities = {}
        self.save_calls = []
        self.map_calls = []
        self.volume_shells = []
        self.volume_solids = []
        self.part_volumes = []
        self.volume_delta = 0.0
        self.mode = "ok"
        self.on_save = None
        self.after_map = None
        self.add("ANSAPART", 1, Name="ProbePart", **{"Module Id": "probe"})
        self.add("SOLID_SECTION", 2, MID=1, Name="SolidSection", **{"Num. Elem": 0})
        self.add(
            "MATERIAL",
            1,
            Name="Steel",
            E=210000.0,
            NU=0.3,
            **{"*DENSITY": "YES", "DENS": 0.002},
        )
        self.add("KIN_MARKER", 7, Name="marker")
        for layer, x, y in (
            (layer, x, y)
            for layer in (0, 1, 2)
            for x in range(3)
            for y in range(3)
        ):
            if layer == 1 and (x, y) == (1, 1):
                continue
            node_id = _node_id(layer, x, y)
            px, py, pz = _coords(layer, x, y)
            self.add("NODE", node_id, X=px, Y=py, Z=pz)
        self.master_ids = []
        self.slave_ids = []
        self.round_ids = []
        shell_id = 1001
        for layer in (0, 2):
            for x in (0, 1):
                for y in (0, 1):
                    self.add(
                        "SHELL",
                        shell_id,
                        type="QUAD",
                        ID=shell_id,
                        PID=1,
                        **{
                            f"G{index}": node
                            for index, node in enumerate(_quad(layer, x, y), 1)
                        },
                    )
                    (self.master_ids if layer == 0 else self.slave_ids).append(shell_id)
                    shell_id += 1
        for fixed_axis, fixed_value in (("x", 0), ("x", 2), ("y", 0), ("y", 2)):
            for layer in (0, 1):
                for across in (0, 1):
                    if fixed_axis == "x":
                        nodes = (
                            _node_id(layer, fixed_value, across),
                            _node_id(layer, fixed_value, across + 1),
                            _node_id(layer + 1, fixed_value, across + 1),
                            _node_id(layer + 1, fixed_value, across),
                        )
                    else:
                        nodes = (
                            _node_id(layer, across, fixed_value),
                            _node_id(layer, across + 1, fixed_value),
                            _node_id(layer + 1, across + 1, fixed_value),
                            _node_id(layer + 1, across, fixed_value),
                        )
                    self.add(
                        "SHELL",
                        shell_id,
                        type="QUAD",
                        ID=shell_id,
                        PID=1,
                        **{
                            f"G{index}": node
                            for index, node in enumerate(nodes, 1)
                        },
                    )
                    self.round_ids.append(shell_id)
                    shell_id += 1

    def add(self, entity_type, entity_id, **fields):
        entity = FakeEntity(entity_type, entity_id, fields)
        self.entities[(entity_type, entity_id)] = entity
        return entity

    def CurrentDeck(self):
        return self.deck

    def DataBaseName(self):
        return str(self.database_path)

    def SaveAs(self, path, silent=True):
        self.save_calls.append((path, silent))
        if self.on_save:
            self.on_save()
        if self.mode == "backup_failure":
            return 1
        Path(path).write_bytes(b"verified backup")
        return 0

    def GetEntity(self, deck, entity_type, entity_id):
        return self.entities.get((entity_type, entity_id))

    def GetEntityCardValues(self, deck, entity, fields):
        values = {
            "__id__": entity._id,
            "__type__": entity._type,
            **entity.fields,
        }
        return {name: values[name] for name in fields if name in values}

    def CollectEntities(self, deck, containers, entity_type, recursive=False,
                        filter_visible=False, hidden_entities=True):
        if containers is None:
            if entity_type == "__ALL_ENTITIES__":
                return list(self.entities.values())
            return [
                entity for (kind, _), entity in self.entities.items()
                if kind == entity_type
            ]
        if containers is self.entities.get(("VOLUME", 1)):
            if entity_type == "SHELL":
                return list(self.volume_shells)
            if entity_type == "SOLID":
                return list(self.volume_solids)
        if containers is self.entities.get(("ANSAPART", 1)):
            if entity_type == "VOLUME":
                if self.mode == "part_membership_raise":
                    raise RuntimeError("injected ANSAPART membership read failure")
                if self.mode == "part_membership_missing":
                    return []
                return list(self.part_volumes)
        return []

    def BoundBox(self, entities):
        return [0.0, 0.0, 0.0, 20.0, 20.0, 20.0]

    def Cog(self, entity):
        return (10.0, 10.0, 10.0)

    def GetFaceArea(self, entity):
        return 400.0

    def apply_map(self, steps, property_id):
        self.add("NODE", 27, X=10.0, Y=10.0, Z=10.0)
        if self.mode == "orphan_node":
            self.add("NODE", 28, X=5.0, Y=5.0, Z=5.0)
        solids = []
        for index, nodes in enumerate(_CELLS, 1):
            cell_nodes = nodes
            if self.mode == "flipped_hexa" and index == 1:
                cell_nodes = (nodes[1], nodes[0], *nodes[2:])
            fields = {
                "type": "HEXA",
                "EID": index,
                "PID": 99 if self.mode == "wrong_pid" and index == 1 else property_id,
                **{
                    f"G{position}": node
                    for position, node in enumerate(cell_nodes, 1)
                },
            }
            solids.append(self.add("SOLID", index, **fields))
        volume = self.add(
            "VOLUME",
            1,
            ID=1,
            PID=property_id,
            Type="Map",
            Method="Normal Parts",
            **{"Map steps": steps},
            **{"Change side": "YES", "Respect target": "YES"},
            **{"Light volume representation": "NO "},
            Order="1st",
            Status="Meshed",
            Frozen="NO",
            Volume=8000.0 + self.volume_delta,
            Mass=6.28e-05,
            Grids=27,
            **{"Internal grids": 1},
            Tetras=0,
            Pentas=0,
            Pyramids=0,
            Hexas=8,
            Polyhedrals=0,
            Name="",
            FROZEN_ID="NO",
            FROZEN_DELETE="NO",
            Comment="",
            MBContainer=None,
            MBContainers=[],
        )
        self.volume_shells = [
            self.entities[("SHELL", shell_id)]
            for shell_id in (*self.master_ids, *self.slave_ids, *self.round_ids)
        ]
        self.volume_solids = solids
        self.part_volumes = [volume]
        self.entities[("SOLID_SECTION", 2)].fields["Num. Elem"] = 8
        if self.after_map:
            self.after_map(self)


class FakeMesh:
    def __init__(self, base):
        self.base = base

    def VolumesMap(self, master, slave, round_areas, **kwargs):
        if "part" in kwargs and kwargs["part"] is None:
            raise TypeError(
                "argument6 must be ansa.base.Entity, not None"
            )
        required = {
            "method", "steps", "prop", "ret_ents",
            "light_volume_representation",
        }
        if set(kwargs) - required - {"part"}:
            raise TypeError("unsupported VolumesMap keyword argument")
        part_value = kwargs.get("part", "<omitted>")
        call = {
            "master": [entity._id for entity in master],
            "slave": [entity._id for entity in slave],
            "round": [entity._id for entity in round_areas],
            "method": kwargs.get("method"),
            "steps": kwargs.get("steps"),
            "part": part_value._id if hasattr(part_value, "_id") else part_value,
            "part_present": "part" in kwargs,
            "prop": kwargs.get("prop")._id if kwargs.get("prop") is not None else None,
            "ret_ents": kwargs.get("ret_ents"),
            "light": kwargs.get("light_volume_representation"),
            "kwargs": sorted(kwargs),
        }
        self.base.map_calls.append(call)
        mode = self.base.mode
        if mode == "raise_before":
            raise RuntimeError("injected pre-mesh exception")
        self.base.apply_map(kwargs["steps"], kwargs["prop"]._id)
        if mode == "raise_after":
            raise RuntimeError("injected post-mesh exception")
        if mode == "return_empty":
            return []
        if mode == "return_int":
            return 1
        return [self.base.entities[("VOLUME", 1)]]


class FakeState:
    def __init__(self, base):
        self.base = base
        self.connected_backend_id = "backend"
        self.epoch = 1

    def assert_main(self):
        return None

    def check_conflict(self):
        return None

    def probe(self):
        return {
            "pid": 10,
            "process_start": 20,
            "instance_id": "instance",
            "session_id": "session",
            "deck": 4,
            "model": {"epoch": self.epoch, "write_enabled": True},
            "authorization": {
                "active": True,
                "backend_id": "backend",
                "generation": 1,
            },
            "connected": True,
            "stopped": False,
            "persistence_blocked": False,
        }


class HexaMeshingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ansa = types.ModuleType("ansa")
        self.ansa.constants = types.SimpleNamespace(ABAQUS=4)
        self.reset_model("current")
        self.module_patch = patch.dict(sys.modules, {"ansa": self.ansa})
        self.module_patch.start()
        self.addCleanup(self.module_patch.stop)

    def reset_model(self, name):
        self.base = FakeBase(self.root / (name + ".ansa"))
        self.state = FakeState(self.base)
        self.mesh = FakeMesh(self.base)
        self.ansa.base = self.base
        self.ansa.mesh = self.mesh

    def shell_condition(self, shell_id):
        fields = self.base.entities[("SHELL", shell_id)].fields
        return {
            "type": "SHELL",
            "id": shell_id,
            "expected_fields": {
                name: fields[name]
                for name in ("PID", "G1", "G2", "G3", "G4")
            },
        }

    def params(self, **overrides):
        values = {
            "deck": 4,
            "master": [self.shell_condition(i) for i in self.base.master_ids],
            "slave": [self.shell_condition(i) for i in self.base.slave_ids],
            "round": [self.shell_condition(i) for i in self.base.round_ids],
            "method": "Normal parts",
            "steps": 2,
            "part": {
                "deck": 4,
                "type": "ANSAPART",
                "id": 1,
                "expected_fields": {"Name": "ProbePart"},
            },
            "property": {
                "deck": 4,
                "type": "SOLID_SECTION",
                "id": 2,
                "expected_fields": {"MID": 1},
            },
            "backup_path": str(self.root / "backup.ansa"),
        }
        values.update(overrides)
        return values

    def run_operation(self, params=None):
        return hexa_meshing.execute(
            self.state,
            "mesh_hex_map",
            self.params() if params is None else params,
        )

    def test_explicit_steps_creates_two_layers_eight_hexa8_and_verifies_scope(self):
        result = self.run_operation()

        self.assertTrue(result["ok"], result["error"])
        self.assertEqual(result["mesh_readback"]["hexa8_count"], 8)
        self.assertEqual(result["mesh_readback"]["new_solid_ids"], list(range(1, 9)))
        self.assertEqual(result["mesh_readback"]["new_node_ids"], [27])
        self.assertEqual(result["mesh_readback"]["used_node_ids"], list(range(1, 28)))
        self.assertEqual(result["map_readback"]["fields"]["Type"], "Map")
        self.assertEqual(result["map_readback"]["fields"]["Status"], "Meshed")
        self.assertEqual(result["map_readback"]["fields"]["Hexas"], 8)
        self.assertEqual(result["map_readback"]["fields"]["Grids"], 27)
        self.assertEqual(result["map_readback"]["fields"]["Internal grids"], 1)
        self.assertEqual(result["map_readback"]["fields"]["Volume"], 8000.0)
        self.assertEqual(result["requested"]["steps"], 2)
        self.assertEqual(self.base.map_calls[0]["master"], self.base.master_ids)
        self.assertEqual(self.base.map_calls[0]["slave"], self.base.slave_ids)
        self.assertEqual(self.base.map_calls[0]["round"], self.base.round_ids)
        self.assertEqual(self.base.map_calls[0]["part"], 1)
        self.assertEqual(self.base.map_calls[0]["prop"], 2)
        self.assertTrue(self.base.map_calls[0]["ret_ents"])
        self.assertTrue(result["backup"]["verified"])
        self.assertEqual(
            result["mesh_readback"]["part_metadata"]["fields"]["Name"],
            "ProbePart",
        )
        self.assertEqual(
            result["scope_card_coverage"]["unclassified_entities_identity_only"],
            [{"deck": 4, "type": "KIN_MARKER", "id": 7}],
        )
        self.assertNotIn(
            "ANSAPART",
            result["scope_card_coverage"]["card_verified_types"],
        )
        self.assertEqual(
            result["scope_card_coverage"]["metadata_verified_types"],
            ["ANSAPART"],
        )
        material = result["before"]["materials"][0]["fields"]
        self.assertEqual(material["E"], 210000.0)
        self.assertEqual(material["*DENSITY"], "YES")
        self.assertEqual(material["DENS"], 0.002)
        json.dumps(result, allow_nan=False)

    def test_target_length_derives_steps_from_centroid_delta(self):
        params = self.params()
        params.pop("steps")
        params["target_length"] = 5.0

        result = self.run_operation(params)

        self.assertTrue(result["ok"], result["error"])
        self.assertEqual(result["requested"]["steps"], 4)
        self.assertEqual(result["requested"]["steps_source"], "target_length")
        self.assertEqual(self.base.map_calls[0]["steps"], 4)

    def test_target_length_uses_ceiling_for_layer_length_not_exceeding_target(self):
        params = self.params()
        params.pop("steps")
        params["target_length"] = 7.0

        result = self.run_operation(params)

        self.assertTrue(result["ok"], result["error"])
        self.assertEqual(result["requested"]["steps"], 3)
        self.assertEqual(self.base.map_calls[0]["steps"], 3)

    def test_target_length_rejects_nonplanar_master_before_backup(self):
        params = self.params()
        params.pop("steps")
        params["target_length"] = 5.0
        shell = self.base.entities[("SHELL", params["master"][0]["id"])]
        shell.fields["G1"] = 19
        params["master"][0]["expected_fields"]["G1"] = 19

        with self.assertRaisesRegex(ValueError, "planar, parallel"):
            self.run_operation(params)
        self.assertEqual(self.base.save_calls, [])
        self.assertEqual(self.base.map_calls, [])

    def test_face_region_is_rejected_before_backup(self):
        params = self.params()
        params["master"][0] = {
            "type": "FACE",
            "id": 1,
            "expected_fields": {
                "ID": 1,
                "PID": 1,
                "Thickness": 0.0,
                "Frozen": "NO",
            },
        }

        with self.assertRaisesRegex(ValueError, "FACE regions are not supported"):
            self.run_operation(params)
        self.assertEqual(self.base.save_calls, [])
        self.assertEqual(self.base.map_calls, [])

    def test_part_is_optional_and_is_omitted_from_native_kwargs(self):
        result = self.run_operation(self.params(part=None))

        self.assertTrue(result["ok"], result["error"])
        self.assertFalse(self.base.map_calls[0]["part_present"])
        self.assertNotIn("part", self.base.map_calls[0]["kwargs"])
        self.assertEqual(
            self.base.map_calls[0]["kwargs"],
            [
                "light_volume_representation",
                "method",
                "prop",
                "ret_ents",
                "steps",
            ],
        )

    def test_stale_shell_condition_rejects_before_backup(self):
        params = self.params()
        params["round"][0]["expected_fields"]["G1"] = 999

        with self.assertRaisesRegex(RuntimeError, "changed since they were read"):
            self.run_operation(params)
        self.assertEqual(self.base.save_calls, [])
        self.assertEqual(self.base.map_calls, [])

    def test_wrong_solid_pid_is_partial_failure_with_after_evidence(self):
        self.base.mode = "wrong_pid"

        result = self.run_operation()

        self.assertFalse(result["ok"])
        self.assertTrue(result["possible_effects"])
        self.assertTrue(result["after"]["complete"])
        self.assertIn("PID", result["error"])
        self.assertEqual(len(self.base.map_calls), 1)

    def test_flipped_hexa_is_rejected_by_signed_jacobian(self):
        self.base.mode = "flipped_hexa"

        result = self.run_operation()

        self.assertFalse(result["ok"])
        self.assertTrue(result["possible_effects"])
        self.assertTrue(result["after"]["complete"])
        self.assertIn("Jacobian", result["error"])
        self.assertEqual(len(self.base.map_calls), 1)

    def test_degenerate_hexa_is_rejected(self):
        points = [
            (0.0, 0.0, 0.0),
            (10.0, 0.0, 0.0),
            (10.0, 10.0, 0.0),
            (0.0, 10.0, 0.0),
            (0.0, 0.0, 0.0),
            (10.0, 0.0, 0.0),
            (10.0, 10.0, 0.0),
            (0.0, 10.0, 0.0),
        ]

        with self.assertRaisesRegex(ValueError, "Jacobian|integrated volume"):
            hexa_meshing._hexa_integration(points)

    def test_orphan_new_node_is_rejected_even_when_volume_counts_match(self):
        self.base.mode = "orphan_node"

        result = self.run_operation()

        self.assertFalse(result["ok"])
        self.assertTrue(result["possible_effects"])
        self.assertIn("unused by new SOLID", result["error"])
        self.assertEqual(result["mesh_readback"]["new_node_ids"], [27, 28])

    def test_exception_after_native_mutation_keeps_full_after_readback(self):
        self.base.mode = "raise_after"

        result = self.run_operation()

        self.assertFalse(result["ok"])
        self.assertTrue(result["possible_effects"])
        self.assertTrue(result["after"]["complete"])
        self.assertEqual(len(result["mesh_readback"]["new_solid_ids"]), 8)
        self.assertIn("post-mesh exception", result["error"])

    def test_empty_return_is_not_success_even_when_readback_has_a_volume(self):
        self.base.mode = "return_empty"

        result = self.run_operation()

        self.assertFalse(result["ok"])
        self.assertTrue(result["possible_effects"])
        self.assertTrue(result["after"]["complete"])
        self.assertIn("exactly one VOLUME", result["error"])

    def test_backup_failure_does_not_call_volumes_map(self):
        self.base.mode = "backup_failure"

        result = self.run_operation()

        self.assertFalse(result["ok"])
        self.assertFalse(result["backup"]["saved"])
        self.assertTrue(result["before"]["complete"])
        self.assertEqual(self.base.map_calls, [])

    def test_part_membership_missing_or_unreadable_is_failure(self):
        for mode, message in (
            ("part_membership_missing", "absent from ANSAPART"),
            ("part_membership_raise", "membership readback failed"),
        ):
            with self.subTest(mode=mode):
                self.reset_model(mode)
                self.base.mode = mode

                params = self.params()
                params["backup_path"] = str(self.root / (mode + "-backup.ansa"))
                result = self.run_operation(params)

                self.assertFalse(result["ok"])
                self.assertTrue(result["possible_effects"])
                self.assertIn(message, result["error"])
                self.assertEqual(len(self.base.map_calls), 1)

    def test_material_active_card_change_is_rejected(self):
        self.base.after_map = lambda base: base.entities[
            ("MATERIAL", 1)
        ].fields.update({"E": 1.0})

        result = self.run_operation()

        self.assertFalse(result["ok"])
        self.assertTrue(result["possible_effects"])
        self.assertIn("material 1 card changed", result["error"])
        self.assertIn(
            "MATERIAL",
            result["scope_card_coverage"]["card_verified_types"],
        )

    def test_volume_tolerance_uses_local_hexa_scale_not_far_coordinates(self):
        for entity in self.base.entities.values():
            if entity._type == "NODE":
                entity.fields["X"] += 1.0e6
                entity.fields["Y"] += 1.0e6
                entity.fields["Z"] += 1.0e6
        self.base.add("NODE", 999, X=1.0e9, Y=1.0e9, Z=1.0e9)
        self.base.volume_delta = 0.05

        result = self.run_operation()

        self.assertFalse(result["ok"])
        self.assertTrue(result["possible_effects"])
        self.assertIn("does not match VOLUME Volume", result["error"])

    def test_mcp_schema_rejects_xor_duplicate_and_relative_backup(self):
        from mcp.server.fastmcp import FastMCP
        from mcp.server.fastmcp.exceptions import ToolError
        from ansa_mcp_plugin.mcp.hexa_meshing import register
        from ansa_mcp_plugin.mcp.schemas import ToolResult

        submitted = []
        target = {
            "backend_id": "backend",
            "instance_id": "instance",
            "session_id": "session",
            "generation": 1,
        }

        def submit(tool, values, operation_id, expected_target, expected_model, ttl):
            submitted.append(tool)
            return ToolResult(
                ok=True,
                status="queued",
                operation_id=operation_id,
                target=expected_target,
                model=expected_model.model_dump(),
            ).as_mcp()

        server = FastMCP("hexa-meshing-schema")
        register(server, submit)
        arguments = {
            "operation_id": "hexa-map",
            "expected_target": target,
            "expected_model": {"epoch": 1},
            **self.params(),
            "target_length": 5.0,
        }
        with self.assertRaises(ToolError):
            asyncio.run(server.call_tool("ansa_mesh_hex_map", arguments))
        self.assertEqual(submitted, [])

        duplicate = {
            "operation_id": "hexa-map",
            "expected_target": target,
            "expected_model": {"epoch": 1},
            **self.params(),
        }
        duplicate["round"] = [duplicate["master"][0], *duplicate["round"]]
        with self.assertRaises(ToolError):
            asyncio.run(server.call_tool("ansa_mesh_hex_map", duplicate))

        face = {
            "operation_id": "hexa-map",
            "expected_target": target,
            "expected_model": {"epoch": 1},
            **self.params(),
        }
        face["master"][0] = {
            "type": "FACE",
            "id": 1,
            "expected_fields": {
                "ID": 1,
                "PID": 1,
                "Thickness": 0.0,
                "Frozen": "NO",
            },
        }
        with self.assertRaises(ToolError):
            asyncio.run(server.call_tool("ansa_mesh_hex_map", face))

        relative = {
            "operation_id": "hexa-map",
            "expected_target": target,
            "expected_model": {"epoch": 1},
            **self.params(backup_path="relative.ansa"),
        }
        with self.assertRaises(ToolError):
            asyncio.run(server.call_tool("ansa_mesh_hex_map", relative))
        self.assertEqual(submitted, [])

        valid = {
            "operation_id": "hexa-map",
            "expected_target": target,
            "expected_model": {"epoch": 1},
            **self.params(),
        }
        asyncio.run(server.call_tool("ansa_mesh_hex_map", valid))
        self.assertEqual(submitted, ["mesh_hex_map"])

        target_int = {
            "operation_id": "hexa-map-int",
            "expected_target": target,
            "expected_model": {"epoch": 1},
            **self.params(),
        }
        target_int.pop("steps")
        target_int["target_length"] = 10
        asyncio.run(server.call_tool("ansa_mesh_hex_map", target_int))
        self.assertEqual(submitted[-1], "mesh_hex_map")

        for value in (True, "10"):
            with self.subTest(target_length=value):
                invalid = {
                    "operation_id": "hexa-map-invalid",
                    "expected_target": target,
                    "expected_model": {"epoch": 1},
                    **self.params(),
                }
                invalid.pop("steps")
                invalid["target_length"] = value
                with self.assertRaises(ToolError):
                    asyncio.run(server.call_tool("ansa_mesh_hex_map", invalid))


if __name__ == "__main__":
    unittest.main()
