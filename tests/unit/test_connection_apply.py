"""Risk-focused checks for the guarded single-point connection realization."""

import tempfile
import types
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from pydantic import TypeAdapter, ValidationError

from ansa_mcp_bridge.operations import connection_apply
from ansa_mcp_plugin.mcp import connection_apply as connection_apply_mcp


_FIXED_SETTINGS = dict(connection_apply._FIXED_SETTINGS)
_CORNERS = {
    1: (0.0, 0.0, 0.0),
    2: (10.0, 0.0, 0.0),
    3: (10.0, 10.0, 0.0),
    4: (0.0, 10.0, 0.0),
    5: (0.0, 0.0, 2.0),
    6: (10.0, 0.0, 2.0),
    7: (10.0, 10.0, 2.0),
    8: (0.0, 10.0, 2.0),
    9: (20.0, 0.0, 4.0),
    10: (30.0, 0.0, 4.0),
    11: (30.0, 10.0, 4.0),
    12: (20.0, 10.0, 4.0),
}


class FakeEntity:
    def __init__(self, entity_type, entity_id, fields, settings=None):
        self._type = entity_type
        self._id = entity_id
        self.fields = dict(fields)
        self.settings = settings

    def ansa_type(self):
        return self._type

    def card_fields(self, deck):
        return list(self.fields)


class FakeBase:
    Entity = FakeEntity

    def __init__(self, current_path, *, mode="ok", malformed_second=False):
        self.database_path = Path(current_path)
        self.database_path.write_bytes(b"current ANSA model")
        self.entities = {}
        self.part_members = {10: [101], 20: [102], 30: [103]}
        self.mode = mode
        self.save_calls = []
        self.realize_calls = []
        self.grid_mutation = None
        self.part_mutation = None
        self.api_return_override = None
        self.add("ANSAPART", 10, **self._part_fields("Visible Part", "module-a"))
        self.add("ANSAPART", 20, **self._part_fields("Visible Part", "module-b"))
        self.add("ANSAPART", 30, **self._part_fields("Outside"))
        for node_id, xyz in _CORNERS.items():
            if malformed_second and node_id == 7:
                xyz = (12.0, 10.0, 2.0)
            self.add(
                "GRID", node_id, NID=node_id, CP=0,
                X1=xyz[0], X2=xyz[1], X3=xyz[2],
                CD=0, PS=None, SEID=None,
            )
        self.add("SHELL", 101, **self._shell_fields(101, 101, (1, 2, 3, 4)))
        self.add("SHELL", 102, **self._shell_fields(102, 102, (5, 6, 7, 8)))
        self.add("SHELL", 103, **self._shell_fields(103, 103, (9, 10, 11, 12)))
        for property_id, material_id in ((101, 101), (102, 101), (103, 101)):
            self.add(
                "PSHELL", property_id, PID=property_id, MID1=material_id, T=1.0, NSM=None,
                **{"Num.Elem": 1, "Name": f"PSHELL {property_id}",
                   "FROZEN_ID": "NO", "FROZEN_DELETE": "NO", "DEFINED": "YES"},
            )
            self.add(
                "MAT1", material_id, MID=material_id, DEFINED="YES",
                E=210000.0, NU=0.3, RHO=7.85e-9,
                FROZEN_ID="NO", FROZEN_DELETE="NO",
            )
        self.selected = self.add(
            "SpotweldPoint_Type", 501, **self._point_fields("module-a", "module-b"),
            settings=None,
        )
        self.bait = self.add(
            "SpotweldPoint_Type", 502, **self._point_fields("module-a", "module-b", 8.0, 8.0),
            settings=None,
        )

    @staticmethod
    def _part_fields(name, module_id=None):
        return {
            "Name": name, "Module Id": module_id or name, "FROZEN_ID": "NO", "FROZEN_DELETE": "NO",
            "FROZEN": "NO", "Comment": "", "Num. Elem": 1,
            "Area":100.0,"Mass":7.85e-7,"COG x":35.0 if name=="Outside" else 5.0,
            "COG y":5.0,"COG z":2.0 if module_id=="module-b" else 0.0,
            "Ixx":1.9625e-5,"Iyy":1.9625e-5,"Izz":3.925e-5,
            "Ixy":0.0,"Ixz":0.0,"Iyz":0.0,"Features Status":"Not Recognized",
            "Last Edit":"16-10月-2026 20:00:00",
        }

    @staticmethod
    def _shell_fields(entity_id, property_id, nodes):
        return {
            "type": "CQUAD4", "EID": entity_id, "PID": property_id,
            **{f"G{index + 1}": node_id for index, node_id in enumerate(nodes)},
            "mat.orient": "MCID", "MCID": None, "ZOFFS": None, "TFLAG": " ",
            "T1": 0.0, "T2": 0.0, "T3": 0.0, "T4": 0.0,
            "Name": "", "FROZEN_ID": "NO", "FROZEN_DELETE": "NO",
            "FROZEN": "NO", "Comment": "",
        }

    @staticmethod
    def _point_fields(p1, p2, x=5.0, y=5.0):
        return {
            "ID": 501 if x == 5.0 else 502,
            "TID": 0, "TmplCompliance": "", "X": x, "Y": y, "Z": 1.0,
            "D": 1.0, "Num of Parts": 2, "P1": p1, "P2": p2,
            **{f"P{index}": "" for index in range(3, 11)},
            "Status": " ", "Error Class": "No FE Rep.", "User": "",
            "FE Rep Type": "<none>", "Name": "", "FROZEN_ID": "NO",
            "FROZEN_DELETE": "NO", "Comment": "",
        }

    def add(self, entity_type, entity_id, settings=None, **fields):
        entity = FakeEntity(entity_type, entity_id, fields, settings)
        self.entities[(entity_type, entity_id)] = entity
        return entity

    def add_fe_perimeter(self, perimeter_id, shell_id):
        perimeter = self.add(
            "FE PERIMETER", perimeter_id,
            Id=perimeter_id, Sides=1, Angle=None,
            **{"Prop. Bound": "NO", "Parts Bound": "NO", "Frozen": "NO",
               "Name": "", "Comment": ""},
        )
        perimeter.edge_shells = [self.entities[("SHELL", shell_id)]]
        return perimeter

    def CurrentDeck(self):
        return 1

    def DataBaseName(self):
        return str(self.database_path)

    def SaveAs(self, path, silent=True):
        self.save_calls.append((path, silent))
        if self.mode == "backup_failure":
            return 1
        Path(path).write_bytes(b"verified backup copy")
        return 0

    def GetEntityCardValues(self, deck, entity, fields):
        values = {"__id__": entity._id, "__type__": entity._type, **entity.fields}
        return {name: values[name] for name in fields if name in values}

    def CollectEntities(
        self, deck, containers, search_type, recursive=False,
        filter_visible=False, hidden_entities=True,
    ):
        if containers is not None and search_type == "__ELEMENTS__":
            part_id = containers._id
            return [
                self.entities[("SHELL", entity_id)]
                for entity_id in self.part_members[part_id]
            ]
        if search_type == "__ALL_ENTITIES__":
            return list(self.entities.values())
        if search_type == "__ELEMENTS__":
            return [
                entity for (kind, _), entity in self.entities.items()
                if kind in {"SHELL", "RBAR"}
            ]
        if search_type == "__PROPERTIES__":
            return [entity for (kind, _), entity in self.entities.items() if kind == "PSHELL"]
        if search_type == "__MATERIALS__":
            return [entity for (kind, _), entity in self.entities.items() if kind == "MAT1"]
        return [
            entity for (kind, _), entity in self.entities.items()
            if kind == search_type
        ]

    def GetAllConnectionSettings(self, entity):
        return entity.settings

    def ConnectivityStringToEnts(self, connectivity):
        return [entity for (kind, _), entity in self.entities.items()
                if kind == "ANSAPART" and entity.fields["Module Id"] == connectivity]

    def RealizeConnections(self, entities, parameters):
        self.realize_calls.append((list(entities), dict(parameters)))
        if self.mode == "raise_before":
            raise RuntimeError("injected pre-realization exception")
        self._realize()
        if self.mode == "raise_after":
            raise RuntimeError("injected post-realization exception")
        if self.mode == "bool_one":
            return True
        if self.mode == "return_zero":
            return 0
        if self.api_return_override is not None:
            return self.api_return_override
        return 1

    def _realize(self):
        center_ids = {10: 1001, 20: 1002}
        shell_ids = {10: 101, 20: 102}
        for part_id in (10, 20):
            shell_id = shell_ids[part_id]
            shell = self.entities[("SHELL", shell_id)]
            corners = tuple(shell.fields[f"G{index}"] for index in range(1, 5))
            center_id = center_ids[part_id]
            point = _CORNERS[corners[0]]
            opposite = _CORNERS[corners[2]]
            z = point[2]
            center_xyz = ((point[0] + opposite[0]) / 2, (point[1] + opposite[1]) / 2, z)
            self.add(
                "GRID", center_id, NID=center_id, CP=0,
                X1=center_xyz[0], X2=center_xyz[1], X3=center_xyz[2],
                CD=0, PS=None, SEID=None,
            )
            if self.grid_mutation is not None and part_id == 10:
                field_name, value = self.grid_mutation
                self.entities[("GRID", center_id)].fields[field_name] = value
            triangles = [
                (corners[0], corners[1], center_id),
                (corners[1], corners[2], center_id),
                (corners[2], corners[3], center_id),
                (corners[3], corners[0], center_id),
            ]
            if self.mode == "reverse_triangle" and part_id == 10:
                triangles[0] = (corners[0], center_id, corners[1])
            for index, nodes in enumerate(triangles):
                element_id = shell_id if index == 0 else 10000 + (part_id * 10) + index
                entity = (
                    shell if index == 0
                    else self.add("SHELL", element_id, **dict(shell.fields))
                )
                entity.fields.update({
                    "type": "CTRIA3", "EID": element_id,
                    "G1": nodes[0], "G2": nodes[1], "G3": nodes[2], "G4": None,
                })
            self.part_members[part_id] = [
                shell_id,
                *[
                    10000 + (part_id * 10) + index
                    for index in range(1, 4)
                ],
            ]
            self.entities[("PSHELL", shell_id)].fields["Num.Elem"] = 4
            part_fields = self.entities[("ANSAPART",part_id)].fields
            for name in ("Ixx","Iyy","Izz"):
                part_fields[name] *= 2.0/3.0
            part_fields["Features Status"] = "Recognized (FE Perimeters)"
            part_fields["Last Edit"] = "16-10月-2026 20:01:00"
            if self.part_mutation is not None and part_id == 10:
                field_name, value = self.part_mutation
                part_fields[field_name] = value
            if self.mode == "mutate_source_thickness" and part_id == 10:
                shell.fields["T1"] = 9.0

        if self.mode != "missing_fan_element":
            self.add(
                "RBAR", 777, EID=777,
                GA=center_ids[10], GB=center_ids[20],
                CNA=123456, CNB=0, CMA=0, CMB=0, Alpha=0.0,
            )
        perimeter_specs = [
            (part_id, shell_id)
            for part_id in (10, 20)
            for shell_id in self.part_members[part_id]
        ]
        for index, (part_id, shell_id) in enumerate(perimeter_specs):
            if self.mode == "missing_perimeter" and index == len(perimeter_specs) - 1:
                continue
            perimeter = self.add_fe_perimeter(9001 + index, shell_id)
            if index == 0:
                if self.mode == "duplicate_perimeter_shell":
                    perimeter.edge_shells.append(perimeter.edge_shells[0])
                elif self.mode == "perimeter_outside_fan":
                    perimeter.edge_shells = [self.entities[("SHELL", 103)]]
                elif self.mode == "perimeter_outside_model":
                    perimeter.edge_shells = [
                        FakeEntity("SHELL", 999, self._shell_fields(999, 103, (9, 10, 11, 12)))
                    ]
                elif self.mode == "perimeter_non_default":
                    perimeter.fields["Frozen"] = "YES"
        if self.mode == "mutate_old_perimeter":
            self.entities[("FE PERIMETER", 9000)].fields["Name"] = "changed"
        self.selected.fields.update({
            "Status": "Ok", "Error Class": "RBAR", "FE Rep Type": "RBAR",
            "Search Dist": None,
            "CNA": 123456, "CNB": 0, "CMA": 0, "CMB": 0,
            "Use Nearest Node": "no",
            "Four Quads around Projection point": "no",
            "Cut off Adhesives": "no",
        })
        self.selected.settings = dict(_FIXED_SETTINGS)


class FakeMesh:
    def __init__(self, base):
        self.base = base
        self.calls = []

    def GetFEPerimeterShells(self, fe_perimeters, expand_to_macro=False):
        self.calls.append((list(fe_perimeters), expand_to_macro))
        if self.base.mode == "perimeter_read_failure":
            raise RuntimeError("injected FE perimeter relation read failure")
        if self.base.mode == "perimeter_tuple":
            return tuple(
                shell
                for perimeter in fe_perimeters
                for shell in perimeter.edge_shells
            )
        return [
            shell
            for perimeter in fe_perimeters
            for shell in perimeter.edge_shells
        ]


class FakeState:
    def __init__(self):
        self.connected_backend_id = "backend-1"
        self.epoch = 1
        self.grant_active = True
        self.main_thread = True

    def assert_main(self):
        if not self.main_thread:
            raise RuntimeError("not on ANSA main thread")

    def check_conflict(self):
        return None

    def probe(self):
        return {
            "pid": 1234, "process_start": "start-1",
            "instance_id": "instance-1", "session_id": "session-1",
            "deck": 1, "connected": True, "stopped": False,
            "persistence_blocked": False,
            "model": {"epoch": self.epoch, "write_enabled": True},
            "authorization": {
                "active": self.grant_active, "backend_id": "backend-1",
                "generation": 1,
            },
        }


class ConnectionApplyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(
            dir=Path(__file__).resolve().parents[2]
        )
        self.addCleanup(self.temp.cleanup)
        current = Path(self.temp.name) / "current.ansa"
        self.base = FakeBase(current)
        self.native_connections = types.SimpleNamespace(
            RealizeConnections=self.base.RealizeConnections,
            GetAllConnectionSettings=self.base.GetAllConnectionSettings,
            ConnectivityStringToEnts=self.base.ConnectivityStringToEnts,
        )
        self.native_mesh = FakeMesh(self.base)
        self.ansa = types.ModuleType("ansa")
        self.ansa.base = self.base
        self.ansa.connections = self.native_connections
        self.ansa.mesh = self.native_mesh
        self.ansa.constants = types.SimpleNamespace(NASTRAN=1)
        self.state = FakeState()
        self.module_patch = patch.dict("sys.modules", {"ansa": self.ansa})
        self.module_patch.start()
        self.addCleanup(self.module_patch.stop)
        self.params = self._params()

    def _reset_model(self):
        backup_path = Path(self.params["backup_path"])
        if backup_path.exists():
            backup_path.unlink()
        self.base = FakeBase(Path(self.temp.name) / "current.ansa")
        self.native_connections = types.SimpleNamespace(
            RealizeConnections=self.base.RealizeConnections,
            GetAllConnectionSettings=self.base.GetAllConnectionSettings,
            ConnectivityStringToEnts=self.base.ConnectivityStringToEnts,
        )
        self.native_mesh = FakeMesh(self.base)
        self.ansa.base = self.base
        self.ansa.connections = self.native_connections
        self.ansa.mesh = self.native_mesh
        self.params = self._params()

    def _params(self):
        parts = []
        for part_id, shell_id, property_id, material_id in (
            (10, 101, 101, 101), (20, 102, 102, 101),
        ):
            parts.append({
                "part_id": part_id,
                "part_expected_fields": {
                    key: value
                    for key, value in self.base.entities[("ANSAPART", part_id)].fields.items()
                    if key != "Num. Elem"
                },
                "shell_id": shell_id,
                "shell_expected_fields": dict(self.base.entities[("SHELL", shell_id)].fields),
                "property_id": property_id,
                "property_expected_fields": {
                    key: value
                    for key, value in self.base.entities[("PSHELL", property_id)].fields.items()
                    if key != "Num.Elem"
                },
                "material_id": material_id,
                "material_expected_fields": dict(self.base.entities[("MAT1", material_id)].fields),
            })
        return {
            "deck": 1,
            "point_id": 501,
            "expected_fields": dict(self.base.selected.fields),
            "target_parts": parts,
            "backup_path": str(Path(self.temp.name) / "connection-backup.ansa"),
        }

    def run_operation(self, params=None):
        return connection_apply.execute(
            self.state, "connection_apply", params or self.params
        )

    def test_realizes_one_point_and_verifies_exact_model_scope(self):
        before_outside = dict(self.base.entities[("SHELL", 103)].fields)
        before_bait = dict(self.base.bait.fields)
        result = self.run_operation()

        self.assertTrue(result["ok"], result["errors"])
        self.assertTrue(result["scope_verified"])
        self.assertTrue(result["model_scope_matches"])
        self.assertFalse(result["possible_effects"])
        self.assertTrue(result["backup"]["verified"])
        self.assertEqual(len(self.base.realize_calls), 1)
        selected, settings = self.base.realize_calls[0]
        self.assertEqual([entity._id for entity in selected], [501])
        self.assertEqual(settings, _FIXED_SETTINGS)
        self.assertEqual(result["api_return_type"], "int")
        self.assertEqual(result["api_return_value"], 1)
        self.assertEqual(
            [self.base.entities[("ANSAPART", part_id)].fields["Name"] for part_id in (10, 20)],
            ["Visible Part", "Visible Part"],
        )
        self.assertEqual(
            [self.base.entities[("ANSAPART", part_id)].fields["Module Id"] for part_id in (10, 20)],
            ["module-a", "module-b"],
        )
        self.assertNotIn("type", self.base.entities[("RBAR", 777)].fields)
        self.assertEqual(result["after"]["complete"], True)
        self.assertEqual(len(result["after"]["fe_perimeters"]), 8)
        self.assertTrue(all(
            len(row["edge_shells"]) == 1
            for row in result["after"]["fe_perimeters"]
        ))
        self.assertTrue(all(
            type(perimeters) is list and len(perimeters) == 1 and expanded is False
            for perimeters, expanded in self.native_mesh.calls
        ))
        self.assertEqual(self.base.entities[("SHELL", 103)].fields, before_outside)
        self.assertEqual(self.base.bait.fields, before_bait)
        self.assertEqual(self.base.entities[("PSHELL", 101)].fields["Num.Elem"], 4)
        self.assertEqual(self.base.entities[("PSHELL", 102)].fields["Num.Elem"], 4)

    def test_non_default_quad_thickness_is_rejected_before_backup(self):
        for field_name, value in (("T2", 0.25), ("T3", True), ("TFLAG", "YES")):
            with self.subTest(field=field_name, value=value):
                self._reset_model()
                self.base.entities[("SHELL", 101)].fields[field_name] = value
                self.params = self._params()
                result = self.run_operation()

                self.assertFalse(result["ok"])
                self.assertFalse(result["api_invoked"])
                self.assertFalse(result["possible_effects"])
                self.assertEqual(self.base.save_calls, [])
                self.assertEqual(self.base.realize_calls, [])

    def test_realized_triangle_thickness_mutation_fails_with_after_snapshot(self):
        self.base.mode = "mutate_source_thickness"
        result = self.run_operation()

        self.assertFalse(result["ok"])
        self.assertFalse(result["scope_verified"])
        self.assertTrue(result["possible_effects"])
        self.assertEqual(len(self.base.realize_calls), 1)
        self.assertTrue(result["after"]["complete"])
        source_after = next(
            row for row in result["after"]["fe_cards"]
            if row["entity"] == {"deck": 1, "type": "SHELL", "id": 101}
        )
        self.assertEqual(source_after["fields"]["T1"], 9.0)

    def test_projected_grid_controls_must_keep_verified_defaults(self):
        for field_name, value in (("CP", False), ("CD", 99), ("PS", 123456), ("SEID", 99)):
            with self.subTest(field=field_name, value=value):
                self._reset_model()
                self.base.grid_mutation = (field_name, value)
                result = self.run_operation()

                self.assertFalse(result["ok"])
                self.assertFalse(result["scope_verified"])
                self.assertTrue(result["possible_effects"])
                self.assertEqual(len(self.base.realize_calls), 1)
                self.assertTrue(result["after"]["complete"])
                grid_after = next(
                    row for row in result["after"]["grids"]
                    if row["entity"]["id"] == 1001
                )
                self.assertEqual(grid_after["fields"][field_name], value)

    def test_unexpected_api_returns_keep_bounded_json_safe_after_evidence(self):
        cases = (
            ("NaN", float("nan"), None),
            ("large string", "x" * 1024 * 1024, 1024),
        )
        for label, value, result_budget in cases:
            with self.subTest(return_value=label):
                self._reset_model()
                self.base.api_return_override = value
                budget_patch = (
                    patch.object(connection_apply, "_RESULT_BUDGET", result_budget)
                    if result_budget is not None else nullcontext()
                )
                with budget_patch:
                    result = self.run_operation()
                    response_size = len(connection_apply._encoded(result))
                    request_size = len(connection_apply._encoded(self.params))
                    self.assertLessEqual(response_size, connection_apply._RESULT_BUDGET)
                    self.assertLess(
                        request_size + response_size,
                        connection_apply.MAX_FRAME_BYTES,
                    )

                self.assertFalse(result["ok"])
                self.assertTrue(result["api_invoked"])
                self.assertTrue(result["possible_effects"])
                self.assertIsNone(result["api_return_value"])
                self.assertEqual(len(self.base.realize_calls), 1)
                self.assertIsInstance(result["after"], dict)
                self.assertTrue(
                    result["after"].get("complete") is True
                    or type(result["after"].get("sha256")) is str
                )
                summary = result["api_return_summary"]
                if label == "NaN":
                    self.assertEqual(summary["type"], "float")
                    self.assertFalse(summary["finite"])
                    self.assertEqual(summary["preview"], "nan")
                else:
                    self.assertEqual(summary["type"], "str")
                    self.assertEqual(summary["length"], 1024 * 1024)
                    self.assertLessEqual(len(summary["preview"]), 128)
                    self.assertEqual(len(summary["sha256"]), 64)

    def test_both_parts_are_preflighted_before_backup_or_native_call(self):
        self.base.entities[("GRID", 7)].fields["X1"] = 12.0
        self.params = self._params()
        result = self.run_operation()

        self.assertFalse(result["ok"])
        self.assertFalse(result["api_invoked"])
        self.assertEqual(self.base.save_calls, [])
        self.assertEqual(self.base.realize_calls, [])
        self.assertFalse(result["possible_effects"])

    def test_shared_material_conditions_must_match_the_same_stable_card(self):
        self.params["target_parts"][1]["material_expected_fields"]["E"] = 200000.0
        result = self.run_operation()

        self.assertFalse(result["ok"])
        self.assertFalse(result["api_invoked"])
        self.assertEqual(self.base.save_calls, [])
        self.assertEqual(self.base.realize_calls, [])
        self.assertTrue(result["errors"])

    def test_invalid_backup_or_current_file_is_rejected_before_realization(self):
        self.params["backup_path"] = str(self.base.database_path)
        with self.assertRaises((ValueError, FileExistsError)):
            self.run_operation()
        self.assertEqual(self.base.realize_calls, [])

    def test_missing_write_authorization_stops_before_backup(self):
        self.state.grant_active = False
        with self.assertRaises(RuntimeError):
            self.run_operation()
        self.assertEqual(self.base.save_calls, [])
        self.assertEqual(self.base.realize_calls, [])

    def test_boolean_true_is_not_accepted_as_strict_integer_one(self):
        self.base.mode = "bool_one"
        result = self.run_operation()

        self.assertFalse(result["ok"])
        self.assertEqual(result["api_return_type"], "bool")
        self.assertTrue(result["api_invoked"])
        self.assertTrue(result["possible_effects"])
        self.assertIsInstance(result["after"], dict)
        self.assertTrue(result["after"]["complete"])
        self.assertEqual(len(self.base.realize_calls), 1)

    def test_native_exception_keeps_post_snapshot_and_effect_warning(self):
        self.base.mode = "raise_after"
        result = self.run_operation()

        self.assertFalse(result["ok"])
        self.assertTrue(result["api_invoked"])
        self.assertTrue(result["possible_effects"])
        self.assertIsInstance(result["after"], dict)
        self.assertTrue(result["after"]["complete"])
        self.assertTrue(any("raised" in item for item in result["errors"]))

    def test_incomplete_fan_is_a_failed_result_with_after_evidence(self):
        self.base.mode = "missing_fan_element"
        result = self.run_operation()

        self.assertFalse(result["ok"])
        self.assertFalse(result["scope_verified"])
        self.assertTrue(result["possible_effects"])
        self.assertIsInstance(result["after"], dict)
        self.assertTrue(result["after"]["complete"])

    def test_reversed_triangle_orientation_is_rejected(self):
        self.base.mode = "reverse_triangle"
        result = self.run_operation()

        self.assertFalse(result["ok"])
        self.assertFalse(result["scope_verified"])
        self.assertTrue(result["possible_effects"])
        self.assertTrue(any("orientation" in item for item in result["errors"]))
        self.assertIsInstance(result["after"], dict)

    def test_fe_perimeter_additions_and_relations_are_verified(self):
        for mode in (
            "missing_perimeter", "duplicate_perimeter_shell",
            "perimeter_outside_fan", "perimeter_outside_model",
            "perimeter_non_default", "mutate_old_perimeter",
        ):
            with self.subTest(mode=mode):
                self._reset_model()
                if mode == "mutate_old_perimeter":
                    self.base.add_fe_perimeter(9000, 103)
                self.base.mode = mode
                result = self.run_operation()

                self.assertFalse(result["ok"], result["errors"])
                self.assertTrue(result["api_invoked"])
                self.assertTrue(result["possible_effects"])
                self.assertEqual(len(self.base.realize_calls), 1)
                self.assertIsInstance(result["after"], dict)

        for mode in ("perimeter_read_failure", "perimeter_tuple"):
            with self.subTest(mode=mode):
                self._reset_model()
                self.base.add_fe_perimeter(9000, 103)
                self.base.mode = mode
                result = self.run_operation()

                self.assertFalse(result["ok"])
                self.assertFalse(result["api_invoked"])
                self.assertFalse(result["possible_effects"])
                self.assertFalse(result["before"]["complete"])
                self.assertEqual(self.base.save_calls, [])
                self.assertEqual(self.base.realize_calls, [])

    def test_unrelated_existing_perimeter_is_preserved_and_source_perimeter_is_rejected(self):
        self.base.add_fe_perimeter(9000, 103)
        result = self.run_operation()

        self.assertTrue(result["ok"], result["errors"])
        old_perimeter = next(
            row for row in result["after"]["fe_perimeters"]
            if row["entity"]["id"] == 9000
        )
        self.assertEqual(old_perimeter["edge_shells"], [
            {"deck": 1, "type": "SHELL", "id": 103},
        ])

        self._reset_model()
        self.base.add_fe_perimeter(9000, 101)
        result = self.run_operation()

        self.assertFalse(result["ok"])
        self.assertFalse(result["api_invoked"])
        self.assertFalse(result["possible_effects"])
        self.assertEqual(self.base.save_calls, [])
        self.assertEqual(self.base.realize_calls, [])

    def test_derived_target_part_measures_reject_inconsistent_sources_before_write(self):
        for name, value in (("Ixx", 9.0), ("Mass", 2.0), ("COG z", 1.0)):
            with self.subTest(field=name):
                self._reset_model()
                self.base.entities[("ANSAPART",10)].fields[name] = value
                self.params = self._params()
                result = self.run_operation()
                self.assertFalse(result["ok"])
                self.assertFalse(result["api_invoked"])
                self.assertEqual(self.base.save_calls, [])
                self.assertEqual(self.base.realize_calls, [])

    def test_changed_part_measures_are_checked_instead_of_ignored_after_realization(self):
        for name, value in (("Ixx", 9.0), ("COG x", 7.0), ("Mass", 2.0),
                            ("Features Status", "Unverified"), ("Last Edit", "")):
            with self.subTest(field=name):
                self._reset_model()
                self.base.part_mutation = (name,value)
                result = self.run_operation()
                self.assertFalse(result["ok"])
                self.assertTrue(result["api_invoked"])
                self.assertTrue(result["possible_effects"])
                self.assertTrue(result["after"]["complete"])

    def test_oversized_full_snapshot_stops_before_backup_and_native_call(self):
        with patch.object(connection_apply, "_SNAPSHOT_BUDGET", 1):
            result = self.run_operation()

        self.assertFalse(result["ok"])
        self.assertFalse(result["api_invoked"])
        self.assertEqual(self.base.save_calls, [])
        self.assertEqual(self.base.realize_calls, [])
        self.assertIsInstance(result["before"], dict)
        self.assertTrue(result["before"]["details_omitted"])

    def test_oversized_request_stops_before_model_access(self):
        params = self._params()
        params["expected_fields"]["large"] = "x" * 140000
        result = self.run_operation(params)

        self.assertFalse(result["ok"])
        self.assertFalse(result["api_invoked"])
        self.assertEqual(self.base.save_calls, [])
        self.assertEqual(self.base.realize_calls, [])

    def test_mcp_schema_requires_full_point_contract_and_forbids_nested_extras(self):
        with self.assertRaises(ValidationError):
            TypeAdapter(connection_apply_mcp.PointExpectedFields).validate_python({"ID": 501})
        with self.assertRaises(ValidationError):
            TypeAdapter(connection_apply_mcp.PointExpectedFields).validate_python({
                "ID": 501, "P1": "PartA", "P2": "PartB", "X": 5.0,
                "Y": 5.0, "Z": 1.0, "D": 1.0, "Status": " ",
                "FE Rep Type": "<none>", "FROZEN_ID": "NO",
                "FROZEN_DELETE": "NO",
            })
        validated_targets = TypeAdapter(connection_apply_mcp.TargetParts).validate_python([
            {
                "part_id": part_id,
                "part_expected_fields": {"Name": name},
                "shell_id": shell_id,
                "shell_expected_fields": {"type": "CQUAD4"},
                "property_id": property_id,
                "property_expected_fields": {"PID": property_id},
                "material_id": 101,
                "material_expected_fields": {"MID": 101},
            }
            for part_id, name, shell_id, property_id in (
                (10, "PartA", 101, 101), (20, "PartB", 102, 102),
            )
        ])
        self.assertEqual(len(validated_targets), 2)
        self.assertEqual([target.material_id for target in validated_targets], [101, 101])
        with self.assertRaises(ValidationError):
            connection_apply_mcp.TargetPart.model_validate({
                "part_id": 10,
                "part_expected_fields": {"Name": "PartA"},
                "shell_id": 101,
                "shell_expected_fields": {"type": "CQUAD4"},
                "property_id": 101,
                "property_expected_fields": {"PID": 101},
                "material_id": 201,
                "material_expected_fields": {"MID": 201},
                "unexpected": True,
            })


if __name__ == "__main__":
    unittest.main()
