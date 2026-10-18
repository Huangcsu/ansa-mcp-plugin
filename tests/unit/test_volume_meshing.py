"""Risk-focused checks for guarded, single-volume meshing."""

import asyncio
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from ansa_mcp_bridge.operations import volume_meshing


_BOX_NODES = {
    1101: (0.0, 0.0, 0.0),
    1102: (1.0, 0.0, 0.0),
    1103: (1.0, 1.0, 0.0),
    1104: (0.0, 1.0, 0.0),
    1105: (0.0, 0.0, 1.0),
    1106: (1.0, 0.0, 1.0),
    1107: (1.0, 1.0, 1.0),
    1108: (0.0, 1.0, 1.0),
}
_FACES = (
    (2101, (1101, 1104, 1103, 1102)),
    (2102, (1105, 1106, 1107, 1108)),
    (2103, (1101, 1102, 1106, 1105)),
    (2104, (1104, 1108, 1107, 1103)),
    (2105, (1101, 1105, 1108, 1104)),
    (2106, (1102, 1103, 1107, 1106)),
)
_PYRAMIDS = (
    (1102, 1103, 1104, 1101),
    (1108, 1107, 1106, 1105),
    (1105, 1106, 1102, 1101),
    (1106, 1107, 1103, 1102),
    (1103, 1107, 1108, 1104),
    (1104, 1108, 1105, 1101),
)


class FakeEntity:
    def __init__(self, entity_type, entity_id, fields):
        self._type = entity_type
        self._id = entity_id
        self.fields = dict(fields)

    def ansa_type(self):
        return self._type

    def card_fields(self, deck):
        return list(self.fields)


class FakeBase:
    Entity = FakeEntity

    def __init__(self, current_path):
        self.database_path = Path(current_path)
        self.database_path.write_bytes(b"current model")
        self.entities = {}
        self.volume_shell_ids = [entity_id for entity_id, _ in _FACES]
        self.mesh_calls = []
        self.save_calls = []
        self.mode = "pyramids"
        self.on_save = None
        self.after_mesh = None
        self.volume = self.add("VOLUME", 1, **self._volume_fields())

        for node_id, point in _BOX_NODES.items():
            self.add("GRID", node_id, NID=node_id, CP=0,
                     X1=point[0], X2=point[1], X3=point[2], CD=0, PS=None, SEID=None)
        for index, node_id in enumerate((1201, 1202, 1203, 1204)):
            # An unrelated grid in a non-basic coordinate system remains in the snapshot.
            self.add("GRID", node_id, NID=node_id, CP=17,
                     X1=float(index + 2), X2=2.0, X3=2.0, CD=0, PS=None, SEID=None)
        for entity_id, nodes in _FACES:
            self.add("SHELL", entity_id, **self._shell_fields(entity_id, nodes))
        self.add("SHELL", 2199, **self._shell_fields(2199, (1201, 1202, 1203, 1204)))
        self.add("RBE2", 2200, EID=2200, GN=1101, CM=123)
        self.add("PSHELL", 3001, PID=3001, MID1=4100, T=1.0, **{"Num. Elem": 0})
        self.add("PSOLID", 3102, PID=3102, MID=4101, **{"Num. Elem": 0})
        self.add("PSOLID", 3100, PID=3100, MID=4100, **{"Num. Elem": 0})
        self.add("MAT1", 4101, MID=4101, DEFINED="YES", E=210000.0, NU=0.3)
        self.add("MAT1", 4100, MID=4100, DEFINED="NO", E=None, NU=None)

    @staticmethod
    def _volume_fields():
        return {
            "PID": 3102, "Type": "Undefined", "Order": "", "Status": "Unmeshed",
            "Frozen": "NO", "Volume": 1.0, "Mass": 0.0, "Grids": 0,
            "Internal grids": 0, "Tetras": 0, "Pentas": 0, "Pyramids": 0,
            "Hexas": 0, "Polyhedrals": 0, "Outline draw": "YES", "Name": "box",
            "FROZEN_ID": "NO", "FROZEN_DELETE": "NO", "Comment": "",
            "MBContainer": "", "MBContainers": "",
        }

    @staticmethod
    def _shell_fields(entity_id, nodes):
        return {
            "type": "CQUAD4", "EID": entity_id, "PID": 3001,
            **{f"G{index + 1}": node_id for index, node_id in enumerate(nodes)},
            "mat.orient": None, "MCID": 0, "ZOFFS": 0.0, "TFLAG": 0,
            "T1": None, "T2": None, "T3": None, "T4": None, "Name": "",
            "FROZEN_ID": "NO", "FROZEN_DELETE": "NO", "FROZEN": "NO",
            "FREEZE ON DEPENETRATION": "NO", "Comment": "",
            "MBContainer": "", "MBContainers": "",
        }

    def add(self, entity_type, entity_id, **fields):
        entity = FakeEntity(entity_type, entity_id, fields)
        self.entities[(entity_type, entity_id)] = entity
        return entity

    def CurrentDeck(self):
        return 1

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

    def GetEntityCardValues(self, deck, entity, fields):
        available = {"__id__": entity._id, "__type__": entity._type, **entity.fields}
        return {name: available[name] for name in fields if name in available}

    def CollectEntities(self, deck, containers, search_type, recursive=False,
                        filter_visible=False, hidden_entities=True):
        if search_type == "__ALL_ENTITIES__":
            return list(self.entities.values())
        if search_type == "__ELEMENTS__":
            return [entity for (kind, _), entity in self.entities.items()
                    if kind in {"SHELL", "SOLID", "RBE2"}]
        if search_type == "__PROPERTIES__":
            return [entity for (kind, _), entity in self.entities.items()
                    if kind in {"PSHELL", "PSOLID"}]
        if search_type == "__MATERIALS__":
            return [entity for (kind, _), entity in self.entities.items() if kind == "MAT1"]
        if search_type == "SHELL" and isinstance(containers, FakeEntity):
            return [self.entities[("SHELL", entity_id)] for entity_id in self.volume_shell_ids]
        return [entity for (kind, _), entity in self.entities.items() if kind == search_type]

    def VolumesMeshV(self, volume, algorithm):
        self.mesh_calls.append((volume._id, algorithm))
        if self.mode == "raise_before":
            raise RuntimeError("injected pre-mesh exception")
        self._apply_mesh(mixed=self.mode == "mixed")
        if self.after_mesh:
            self.after_mesh(self)
        if self.mode == "mutate_unrelated_grid":
            self.entities[("GRID", 1201)].fields["X1"] += 0.25
        if self.mode == "raise_after":
            raise RuntimeError("injected post-mesh exception")
        if self.mode == "return_bool":
            return True
        if self.mode == "return_zero":
            return 0
        return 1

    def _apply_mesh(self, *, mixed):
        self.add("GRID", 2, NID=2, CP=0, X1=0.5, X2=0.5, X3=0.5,
                 CD=0, PS=None, SEID=None)
        solid_id = 5100
        tetra_count = 0
        pyramid_count = 0
        for index, nodes in enumerate(_PYRAMIDS):
            if mixed and index == 0:
                self.add("SOLID", solid_id, **self._solid_fields(
                    solid_id, "CTETRA", (*nodes[:3], 2, 0)))
                solid_id += 1
                self.add("SOLID", solid_id, **self._solid_fields(
                    solid_id, "CTETRA", (nodes[0], nodes[2], nodes[3], 2, 0)))
                solid_id += 1
                tetra_count += 2
                continue
            self.add("SOLID", solid_id, **self._solid_fields(
                solid_id, "PYRAMID", (*nodes, 2)))
            solid_id += 1
            pyramid_count += 1

        volume = self.entities[("VOLUME", 1)]
        volume.fields.update({
            "Type": "Tetra Rapid", "Order": "1st", "Status": "Meshed",
            "Grids": 9, "Internal grids": 1, "Tetras": tetra_count,
            "Pyramids": pyramid_count, "Pentas": 0, "Hexas": 0,
            "Polyhedrals": 0,
            "Growth rate": 1.2, "Element length": "0.0",
            "Criterion": "NASTRAN Max. Aspect Ratio", "Criterion value": 4.0,
            "Create pyramids": "YES", "Light volume representation": "NO ",
            "Allow reconstruction of volume skin": "NO ", "Frozen entities": 0,
            "Force rows of tetras in thin walls": 1,
            "Do not violate maximum element length": "NO ",
            "Autodefine coordinate system": "NO ", "Coordinate system": 0,
            "Buffer zones": 0, "Transition point offset": "NO ",
            "Polyhedral connection": "NO ", "Connect to symmetry": "NO ",
            "Smooth transition": "NO ",
        })
        self.entities[("PSOLID", 3102)].fields["Num. Elem"] = tetra_count + pyramid_count

    @staticmethod
    def _solid_fields(entity_id, kind, nodes):
        return {
            "type": kind, "EID": entity_id, "PID": 3102,
            **{f"G{index + 1}": node_id for index, node_id in enumerate(nodes)},
            "mat.orient": None, "mcid": 0, "Name": "", "FROZEN_ID": "NO",
            "FROZEN_DELETE": "NO", "Comment": "", "MBContainer": "",
            "MBContainers": "",
        }


class FakeState:
    def __init__(self, base):
        self.base = base
        self.epoch = 1
        self.connected_backend_id = "backend"

    def assert_main(self):
        return None

    def check_conflict(self):
        return None

    def probe(self):
        return {
            "pid": 10, "process_start": 20, "instance_id": "instance",
            "session_id": "session", "deck": 1,
            "model": {"epoch": self.epoch, "write_enabled": True},
            "authorization": {"active": True, "backend_id": "backend", "generation": 1},
            "connected": True, "stopped": False, "persistence_blocked": False,
        }


class VolumeMeshingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ansa = types.ModuleType("ansa")
        self.ansa.constants = types.SimpleNamespace(NASTRAN=1)
        self.reset_model("current")
        self.module_patch = patch.dict(sys.modules, {"ansa": self.ansa})
        self.module_patch.start()
        self.addCleanup(self.module_patch.stop)

    def reset_model(self, name):
        self.base = FakeBase(self.root / (name + ".ansa"))
        self.state = FakeState(self.base)
        self.ansa.base = self.base
        self.ansa.mesh = types.SimpleNamespace(VolumesMeshV=self.base.VolumesMeshV)

    def params(self):
        volume = self.base.entities[("VOLUME", 1)]
        names = ("PID", "Type", "Order", "Status", "Frozen")
        return {
            "deck": 1,
            "volume_id": 1,
            "expected_fields": {name: volume.fields[name] for name in names},
            "expected_shell_ids": [entity_id for entity_id, _ in _FACES],
            "backup_path": str(self.base.database_path.with_name(
                self.base.database_path.stem + "-backup.ansa")),
        }

    def run_operation(self, params=None):
        return volume_meshing.execute(
            self.state, "mesh_volume", params if params is not None else self.params()
        )

    def test_probe_pyramid_mesh_is_accepted_and_unrelated_nonbasic_grids_are_ignored(self):
        result = self.run_operation()

        self.assertTrue(result["ok"], result["error"])
        self.assertEqual(result["mesh_readback"]["solid_type_counts"], {"TETRA": 0, "PYRAMID": 6})
        self.assertEqual(result["mesh_readback"]["new_grid_ids"], [2])
        self.assertEqual(result["mesh_readback"]["volume_counts"]["Pyramids"], 6)
        self.assertEqual(self.base.mesh_calls, [(1, "TETRA RAPID")])
        self.assertTrue(result["backup"]["verified"])
        json.dumps(result, allow_nan=False)

    def test_first_order_ctetra_and_pyramid_results_are_counted_and_volume_checked(self):
        self.base.mode = "mixed"

        result = self.run_operation()

        self.assertTrue(result["ok"], result["error"])
        self.assertEqual(result["mesh_readback"]["solid_type_counts"], {"TETRA": 2, "PYRAMID": 5})
        self.assertAlmostEqual(result["mesh_readback"]["tetra_volume_sum"], 1.0)

    def test_stale_fields_boundary_and_undefined_material_reject_before_backup(self):
        params = self.params()
        params["expected_fields"]["PID"] = 3100
        with self.assertRaisesRegex(RuntimeError, "changed since it was read"):
            self.run_operation(params)
        self.assertEqual(self.base.save_calls, [])
        self.assertEqual(self.base.mesh_calls, [])

        params = self.params()
        params["expected_shell_ids"] = [*params["expected_shell_ids"][:-1], 2199]
        with self.assertRaisesRegex(ValueError, "boundary does not exactly match"):
            self.run_operation(params)
        self.assertEqual(self.base.save_calls, [])

        self.base.entities[("VOLUME", 1)].fields["PID"] = 3100
        params = self.params()
        with self.assertRaisesRegex(ValueError, "DEFINED='YES'"):
            self.run_operation(params)
        self.assertEqual(self.base.save_calls, [])
        self.assertEqual(self.base.mesh_calls, [])

    def test_reversed_pyramid_and_tetra_reject_with_full_after_evidence(self):
        for mode, left, right in (("pyramids", "G2", "G4"), ("mixed", "G2", "G3")):
            with self.subTest(mode=mode):
                self.reset_model("reversed-" + mode)
                self.base.mode = mode

                def reverse_first_cell(base):
                    fields = base.entities[("SOLID", 5100)].fields
                    fields[left], fields[right] = fields[right], fields[left]

                self.base.after_mesh = reverse_first_cell
                result = self.run_operation()
                self.assertFalse(result["ok"])
                self.assertFalse(result["scope_verified"])
                self.assertTrue(result["possible_effects"])
                self.assertTrue(result["after"]["complete"])
                self.assertIn("non-positive or degenerate signed", result["error"])
                self.assertEqual(len(self.base.mesh_calls), 1)

    def test_snapshot_budget_rejects_before_backup_and_returns_bounded_digest(self):
        self.base.entities[("SHELL", 2101)].fields["Name"] = "x" * (270 * 1024)
        params = self.params()

        result = self.run_operation(params)

        self.assertFalse(result["ok"])
        self.assertIn("256 KiB", result["error"])
        self.assertTrue(result["before"]["details_omitted"])
        self.assertEqual(self.base.save_calls, [])
        self.assertEqual(self.base.mesh_calls, [])
        self.assertLess(len(json.dumps(result).encode("utf-8")), 768 * 1024)

    def test_verified_backup_is_retained_when_context_changes_after_save(self):
        self.base.on_save = lambda: setattr(self.state, "epoch", 2)

        result = self.run_operation()

        self.assertFalse(result["ok"])
        self.assertTrue(result["backup"]["verified"])
        self.assertIn("context or write authorization changed", result["error"])
        self.assertEqual(self.base.mesh_calls, [])
        self.assertTrue(Path(self.params()["backup_path"]).is_file())

    def test_backup_save_failure_is_read_back_and_does_not_mesh(self):
        self.base.mode = "backup_failure"

        result = self.run_operation()

        self.assertFalse(result["ok"])
        self.assertFalse(result["backup"]["saved"])
        self.assertTrue(result["before"]["complete"])
        self.assertTrue(result["after"]["complete"])
        self.assertEqual(self.base.mesh_calls, [])

    def test_only_exact_int_one_is_success_even_when_readback_is_complete(self):
        self.base.mode = "return_bool"

        result = self.run_operation()

        self.assertFalse(result["ok"])
        self.assertEqual(result["api_return_type"], "bool")
        self.assertTrue(result["after"]["complete"])
        self.assertIn("exact success value int 1", result["error"])
        self.assertEqual(len(self.base.mesh_calls), 1)

    def test_exception_after_mesh_mutation_keeps_independent_after_readback(self):
        self.base.mode = "raise_after"

        result = self.run_operation()

        self.assertFalse(result["ok"])
        self.assertTrue(result["possible_effects"])
        self.assertTrue(result["after"]["complete"])
        self.assertEqual(len(self.base.mesh_calls), 1)
        self.assertIn("post-mesh exception", result["error"])

    def test_original_fe_property_and_grid_stable_card_changes_are_rejected(self):
        cases = (
            ("GRID", 1201, "X1", 99.0, "original GRID 1201 stable card changed"),
            ("RBE2", 2200, "CM", 6, "original RBE2 2200 stable FE card changed"),
            ("PSHELL", 3001, "T", 99.0, "property definitions changed"),
            ("GRID", 1201, "PS", 123, "original GRID 1201 stable card changed"),
        )
        for index, (kind, entity_id, field, value, error) in enumerate(cases):
            with self.subTest(kind=kind, field=field):
                self.reset_model("changed-" + str(index))
                self.base.after_mesh = lambda base: base.entities[(kind, entity_id)].fields.update(
                    {field: value}
                )
                result = self.run_operation()
                self.assertFalse(result["ok"])
                self.assertFalse(result["scope_verified"])
                self.assertIn(error, result["error"])
                self.assertEqual(len(self.base.mesh_calls), 1)

    def test_unreadable_stable_cards_reject_before_backup(self):
        for index, (kind, entity_id, field) in enumerate((
            ("RBE2", 2200, "CM"), ("PSHELL", 3001, "T"), ("GRID", 1201, "PS"),
        )):
            with self.subTest(kind=kind, field=field):
                self.reset_model("unreadable-" + str(index))
                target = self.base.entities[(kind, entity_id)]
                read_card = self.base.GetEntityCardValues

                def omit_field(deck, entity, fields):
                    values = read_card(deck, entity, fields)
                    if entity is target:
                        values.pop(field, None)
                    return values

                with patch.object(self.base, "GetEntityCardValues", side_effect=omit_field):
                    with self.assertRaisesRegex(ValueError, "requested card field"):
                        self.run_operation()
                self.assertEqual(self.base.save_calls, [])
                self.assertEqual(self.base.mesh_calls, [])

        with self.subTest(missing_grid_label="SEID"):
            self.reset_model("missing-grid-label")
            del self.base.entities[("GRID", 1201)].fields["SEID"]
            with self.assertRaisesRegex(NotImplementedError, "required card field.*SEID"):
                self.run_operation()
            self.assertEqual(self.base.save_calls, [])
            self.assertEqual(self.base.mesh_calls, [])

    def test_duplicate_and_cavity_mesh_topology_is_rejected(self):
        def duplicate_bottom(base):
            source = base.entities[("SOLID", 5100)].fields
            base.entities[("SOLID", 5101)].fields.update({
                "G1": source["G2"], "G2": source["G3"], "G3": source["G4"],
                "G4": source["G1"], "G5": source["G5"],
            })

        def cavity_with_balanced_volume(base):
            base.add("GRID", 3, NID=3, CP=0, X1=0.5, X2=0.5, X3=0.4,
                     CD=0, PS=None, SEID=None)
            for entity_id in (5100, 5101):
                base.entities[("SOLID", entity_id)].fields["G5"] = 3
            base.entities[("VOLUME", 1)].fields.update({"Grids": 10, "Internal grids": 2})

        for name, mutate, error in (
            ("duplicate", duplicate_bottom, "duplicate SOLID connectivity"),
            ("cavity", cavity_with_balanced_volume, "internal cavity"),
        ):
            with self.subTest(name=name):
                self.reset_model(name)
                self.base.after_mesh = mutate
                result = self.run_operation()
                self.assertFalse(result["ok"])
                self.assertIn(error, result["error"])
                self.assertTrue(result["after"]["complete"])
                self.assertEqual(len(result["after"]["solid_cards"]), 6)
                self.assertEqual(len(self.base.mesh_calls), 1)

    def test_boundary_membership_change_keeps_full_after_readback(self):
        self.base.after_mesh = lambda base: base.volume_shell_ids.pop()

        result = self.run_operation()

        self.assertFalse(result["ok"])
        self.assertFalse(result["scope_verified"])
        self.assertTrue(result["possible_effects"])
        self.assertTrue(result["backup"]["verified"])
        self.assertIn("boundary SHELL membership changed", result["error"])
        self.assertEqual(len(result["before"]["boundary_shell_references"]), 6)
        self.assertTrue(result["after"]["complete"])
        self.assertEqual(len(result["after"]["boundary_shell_references"]), 5)
        self.assertEqual(len(result["after"]["solid_cards"]), 6)
        self.assertTrue(any(row["entity"]["id"] == 2 for row in result["after"]["grids"]))
        self.assertEqual(len(self.base.mesh_calls), 1)

    def test_mcp_and_bridge_request_limits_and_result_limit_are_enforced(self):
        from mcp.server.fastmcp import FastMCP
        from mcp.server.fastmcp.exceptions import ToolError
        from ansa_mcp_plugin.mcp.schemas import ToolResult
        from ansa_mcp_plugin.mcp.volume_meshing import register

        params = self.params()
        params["expected_fields"]["Comment"] = "x" * (129 * 1024)
        with self.assertRaisesRegex(ValueError, "128 KiB"):
            volume_meshing._validate_inputs(params)

        submitted = []
        target = {"backend_id": "backend", "instance_id": "instance",
                  "session_id": "session", "generation": 1}

        def submit(tool, values, operation_id, expected_target, expected_model, ttl):
            submitted.append(tool)
            return ToolResult(ok=True, status="queued", operation_id=operation_id,
                              target=expected_target, model=expected_model.model_dump()).as_mcp()

        server = FastMCP("volume-meshing-schema")
        register(server, submit)
        arguments = {
            "operation_id": "volume-mesh", "expected_target": target,
            "expected_model": {"epoch": 1}, "deck": 1, "volume_id": 1,
            "expected_fields": {**self.params()["expected_fields"], "Comment": "x" * (129 * 1024)},
            "expected_shell_ids": [entity_id for entity_id, _ in _FACES],
            "backup_path": str(self.root / "mcp-backup.ansa"),
        }
        with self.assertRaises(ToolError):
            awaitable = server.call_tool("ansa_mesh_volume", arguments)
            asyncio.run(awaitable)
        self.assertEqual(submitted, [])

        bounded = volume_meshing._bounded_result(
            self.params(),
            {"before": {"blob": "x" * (400 * 1024)},
             "after": {"blob": "y" * (400 * 1024)}, "errors": []},
        )
        encoded = json.dumps(bounded).encode("utf-8")
        self.assertTrue(bounded["evidence_truncated"])
        self.assertLess(len(encoded), 768 * 1024)
        self.assertLess(len(encoded) + len(json.dumps(self.params()).encode("utf-8")), 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
