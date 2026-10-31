"""Risk-focused offline checks for structured native snapshots and writes."""

import copy
import hashlib
from pathlib import Path
import types
import unittest
from unittest.mock import patch

from ansa_mcp_bridge.operations import structured_native
from ansa_mcp_bridge.operations import mesh_planning
from ansa_mcp_bridge.operations import volume_meshing


class FakeEntity:
    def __init__(self, entity_id, entity_type, fields):
        self._id = entity_id
        self._type = entity_type
        self.fields = dict(fields)

    def ansa_type(self):
        return self._type

    def card_fields(self, _deck):
        return [name for name in self.fields if not name.startswith("__")]


class FakeBase:
    Entity = FakeEntity

    def __init__(self):
        self.deck = 1
        self.entities = {}
        self.face_bounds = {}
        self.face_cons = {}

    def add(self, entity_type, entity_id, **fields):
        entity = FakeEntity(entity_id, entity_type, fields)
        self.entities[(entity_type, entity_id)] = entity
        return entity

    def CurrentDeck(self):
        return self.deck

    def GetEntity(self, deck, entity_type, entity_id):
        return self.entities.get((entity_type, entity_id)) if deck == self.deck else None

    def GetEntityCardValues(self, deck, entity, fields):
        if deck != self.deck:
            return {}
        values = {}
        for name in fields:
            if name == "__id__":
                values[name] = entity._id
            elif name == "__type__":
                values[name] = entity._type
            elif name in entity.fields:
                values[name] = entity.fields[name]
        return values

    def CollectEntities(self, deck, container, entity_type, *_args, **_kwargs):
        if deck != self.deck:
            return []
        if entity_type == "__ALL_ENTITIES__":
            return list(self.entities.values())
        if entity_type == "CONS" and isinstance(container, FakeEntity):
            ids = self.face_cons.get(container._id, [])
            return [self.GetEntity(deck, "CONS", entity_id) for entity_id in ids]
        return [entity for (kind, _), entity in self.entities.items() if kind == entity_type]

    def BoundBox(self, entity_or_entities):
        entity = entity_or_entities[0] if isinstance(entity_or_entities, (list, tuple)) else entity_or_entities
        if entity._type == "FACE":
            return list(self.face_bounds[entity._id])
        return list(entity.fields.get("bounds", [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]))

    def GetFaceArea(self, face):
        return face.fields["area"]

    def Cog(self, face):
        return face.fields["center"]

    def GetFaceOrientation(self, _face):
        return [1.0, 0.0, 0.0]

    def GetFaceMacro(self, face):
        return face

    def GetFacesOfMacros(self, macros):
        return list(macros)


class FakeMesh:
    def __init__(self, base):
        self.base = base

    def GetMacroShells(self, _macros):
        return 0


class FakePreviewState:
    connected_backend_id = "backend"

    def assert_main(self):
        pass

    def check_conflict(self):
        pass

    def probe(self):
        return {
            "deck": 1,
            "pid": 17,
            "process_start": 23,
            "instance_id": "instance",
            "session_id": "session",
            "connected": True,
            "stopped": False,
            "model": {"epoch": 4},
            "authorization": {"active": True, "backend_id": "backend", "generation": 3},
        }


class StructuredNativeChecks(unittest.TestCase):
    def setUp(self):
        self.constants = types.SimpleNamespace(NASTRAN=1)

    def test_freeze_sweep_guards_and_readback(self):
        for scenario in ("success", "partial", "exception", "false_return", "scope_change", "source_change",
                         "already_frozen", "missing_field", "post_unfrozen", "default"):
            with self.subTest(scenario=scenario):
                base = FakeBase()
                shells = [base.add("SHELL", i, FROZEN="NO", PID=8) for i in (10, 11)]
                if scenario == "already_frozen":
                    shells[0].fields["FROZEN"] = "YES"
                if scenario == "missing_field":
                    del shells[0].fields["FROZEN"]
                state = {"backup": False, "freeze_calls": 0, "extrude_calls": 0, "outside": 0,
                         "source_changed": False}
                base.SaveAs = lambda *_args, **_kwargs: 0
                base.GetCurrentEntity = lambda *_args: None
                base.SetCurrentEntity = lambda *_args, **_kwargs: 0

                def freeze(deck, kind, selection):
                    self.assertTrue(state["backup"])
                    self.assertEqual((deck, kind, selection), (1, "0", shells))
                    state["freeze_calls"] += 1
                    shells[0].fields["FROZEN"] = "YES"
                    if scenario != "partial":
                        shells[1].fields["FROZEN"] = "YES"
                    if scenario == "scope_change": state["outside"] = 1
                    if scenario == "source_change": state["source_changed"] = True
                    if scenario == "exception": raise RuntimeError("freeze interrupted")
                    if scenario == "false_return": return False
                    return True
                base.FreezeElements = freeze

                class Extrude:
                    def translate(self, **_kwargs):
                        state["extrude_calls"] += 1
                        if scenario == "post_unfrozen": shells[0].fields["FROZEN"] = "NO"
                        return 1
                def scope(*_args, **_kwargs):
                    return {"complete": True, "deck": 1, "entities": [
                        {"entity": {"type": "SHELL", "id": shell._id},
                         "fields": dict(shell.fields), "card_fields": list(shell.fields)} for shell in shells
                    ] + [{"entity": {"type": "GRID", "id": 1}, "fields": {"X1": state["outside"]},
                          "card_fields": ["X1"]}]}
                digest = "a" * 64
                def source(*_args):
                    return {"snapshot_sha256": "b" * 64 if state["source_changed"] else digest,
                            "elements": [], "nodes": []}
                def backup(*_args):
                    state["backup"] = True
                    return {"verified": True}
                params = {"deck": 1, "shell_ids": [10, 11], "direction": [0, 0, 1],
                          "distance": 1., "steps": 1, "property_id": 12,
                          "expected_source_sha256": digest, "backup_path": "unused.ansa"}
                if scenario != "default": params["freeze_source"] = True
                diff = {"errors": [], "scope_verified": True, "new_grid_ids": [], "new_solid_ids": [90],
                        "new_volume_ids": [], "native_auxiliary_properties": [], "native_auxiliary_materials": []}
                with (patch.object(volume_meshing, "_execution_context", return_value={"deck": 1}),
                      patch.object(volume_meshing, "_same_context"),
                      patch.object(structured_native, "_capture_scope", side_effect=scope),
                      patch.object(structured_native, "_capture_freeze_scope", side_effect=scope),
                      patch.object(structured_native, "capture_source", side_effect=source),
                      patch.object(structured_native, "_source_preflight", return_value={}),
                      patch.object(structured_native, "_psolid_preflight", return_value=(object(), {})),
                      patch.object(structured_native, "_backup_preflight", return_value=(Path("unused.ansa"), "model.ansa", {})),
                      patch.object(structured_native, "_verify_backup", side_effect=backup),
                      patch.object(structured_native, "_sweep_scope_diff", return_value=diff) as audit,
                      patch.object(structured_native, "capture_mesh", return_value={"snapshot_sha256": digest, "integrity": {}}),
                      patch.object(structured_native, "_verify_sweep_mesh", return_value={})):
                    result = structured_native._run_sweep(object(), base, types.SimpleNamespace(VolumesExtrude=Extrude),
                                                          self.constants, params)
                if scenario in ("success", "default", "post_unfrozen"):
                    self.assertEqual(state["extrude_calls"], 1)
                    self.assertEqual(result["ok"], scenario != "post_unfrozen")
                    if scenario != "default":
                        self.assertEqual(audit.call_args.args[0]["entities"][0]["fields"]["FROZEN"], "YES")
                        self.assertEqual(result["freeze"]["before"], {"10": "NO", "11": "NO"})
                        self.assertEqual(result["freeze"]["after_freeze"], {"10": "YES", "11": "YES"})
                        self.assertEqual(result["freeze"]["after_sweep"]["10"], "NO" if scenario == "post_unfrozen" else "YES")
                else:
                    self.assertEqual(state["extrude_calls"], 0)
                    self.assertFalse(result["ok"])
                    self.assertEqual(result["possible_effects"], scenario not in ("already_frozen", "missing_field"))
                    self.assertEqual(result["recovery_required"], scenario not in ("already_frozen", "missing_field"))
                self.assertEqual(state["freeze_calls"], 0 if scenario in ("already_frozen", "missing_field", "default") else 1)

    def test_sweep_freeze_source_bridge_validation_is_strict(self):
        params = {"deck": 1, "shell_ids": [10], "direction": [0, 0, 1], "distance": 1.,
                  "steps": 1, "property_id": 12, "expected_source_sha256": "a" * 64,
                  "backup_path": "unused.ansa"}
        self.assertIs(structured_native._normalize_sweep(params)["freeze_source"], False)
        for value in (1, "true", None):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "boolean"):
                structured_native._normalize_sweep({**params, "freeze_source": value})

    def test_freeze_scope_reads_omitted_raw_fields_and_rejects_other_changes(self):
        base = FakeBase()
        shell = base.add("SHELL", 10, FROZEN="NO", PID=8, **{"Num. Elem": 1})
        reference = {"deck": 1, "type": "SHELL", "id": 10}
        reduced = {"deck": 1, "complete": True, "entities": [{"entity": reference,
                   "card_fields": ["FROZEN", "PID"], "fields": {"FROZEN": "NO", "PID": 8}}]}
        with patch.object(structured_native, "_capture_scope", side_effect=lambda *_, **_kw: copy.deepcopy(reduced)):
            before = structured_native._capture_freeze_scope(base, 1)
            shell.fields["FROZEN"] = "YES"
            after = structured_native._capture_freeze_scope(base, 1)
            self.assertEqual(before["entities"][0]["fields"]["Num. Elem"], 1)
            self.assertEqual(structured_native._freeze_scope_errors(before, after, {10}), [])
            shell.fields["Num. Elem"] = 2
            changed = structured_native._capture_freeze_scope(base, 1)
            self.assertTrue(structured_native._freeze_scope_errors(before, changed, {10}))

    def _mesh_base(self):
        base = FakeBase()
        points = {
            1: (0, 0, 0), 2: (1, 0, 0), 3: (1, 1, 0), 4: (0, 1, 0),
            5: (0, 0, 1), 6: (1, 0, 1), 7: (1, 1, 1), 8: (0, 1, 1),
        }
        for node_id, xyz in points.items():
            base.add("GRID", node_id, NID=node_id, CP=0, X1=float(xyz[0]),
                     X2=float(xyz[1]), X3=float(xyz[2]), CD=0)
        base.add("SOLID", 90, EID=90, PID=12, type="CHEXA",
                 **{f"G{i}": i for i in range(1, 9)})
        return base

    def test_capture_source_sorts_ids_hashes_snapshot_and_uses_public_audit_shape(self):
        base = FakeBase()
        points = {
            1: (0, 0, 0), 2: (1, 0, 0), 3: (1, 1, 0), 4: (0, 1, 0),
            5: (2, 0, 0), 6: (3, 0, 0), 7: (2, 1, 0),
        }
        for node_id, xyz in points.items():
            base.add("GRID", node_id, NID=node_id, CP=0, X1=float(xyz[0]),
                     X2=float(xyz[1]), X3=float(xyz[2]), CD=0)
        base.add("SHELL", 20, EID=20, PID=8, type="CTRIA3", G1=5, G2=6, G3=7)
        base.add("SHELL", 10, EID=10, PID=8, type="CQUAD4", G1=1, G2=2, G3=3, G4=4)

        first = structured_native.capture_source(base, self.constants, 1, [20, 10])
        reversed_ids = structured_native.capture_source(base, self.constants, 1, [10, 20])

        self.assertEqual(first["snapshot_sha256"], reversed_ids["snapshot_sha256"])
        self.assertEqual([row["id"] for row in first["nodes"]], list(range(1, 8)))
        self.assertEqual(first["elements"], [
            {"id": 10, "type": "CQUAD4", "nodes": [1, 2, 3, 4], "property_id": 8, "block_id": None},
            {"id": 20, "type": "CTRIA3", "nodes": [5, 6, 7], "property_id": 8, "block_id": None},
        ])
        body = {key: value for key, value in first.items() if key != "snapshot_sha256"}
        expected = hashlib.sha256(volume_meshing._encoded(body)).hexdigest()
        self.assertEqual(first["snapshot_sha256"], expected)
        self.assertTrue(first["integrity"]["complete"])

    def test_capture_source_snapshot_is_accepted_by_mesh_planning_audit(self):
        base = FakeBase()
        points = {
            1: (0, 0, 0), 2: (1, 0, 0), 3: (1, 1, 0), 4: (0, 1, 0),
            5: (0, 0, 1), 6: (1, 0, 1), 7: (1, 1, 1), 8: (0, 1, 1),
        }
        for node_id, xyz in points.items():
            base.add("GRID", node_id, NID=node_id, CP=0, X1=float(xyz[0]),
                     X2=float(xyz[1]), X3=float(xyz[2]), CD=0)
        base.add("SHELL", 10, EID=10, PID=8, type="CQUAD4", G1=1, G2=2, G3=3, G4=4)
        base.add("SHELL", 20, EID=20, PID=8, type="CQUAD4", G1=5, G2=6, G3=7, G4=8)
        snapshot = structured_native.capture_source(base, self.constants, 1, [20, 10])
        normalized = {
            **snapshot,
            "nodes": {row["id"]: {"position": row["xyz"]} for row in snapshot["nodes"]},
            "elements": [
                {"entity": {"type": "SHELL", "id": row["id"]},
                 "element_type": row["type"], "node_ids": row["nodes"]}
                for row in snapshot["elements"]
            ],
        }

        audit = mesh_planning.audit_source(
            normalized, source_ids=[10], target_ids=[20], direction=[0, 0, 1],
            layers=1, target_length=1.0,
        )

        self.assertTrue(audit["supported"])
        self.assertTrue(audit["passed"])
        self.assertEqual(snapshot["snapshot_sha256"],
                         structured_native.capture_source(base, self.constants, 1, [10, 20])["snapshot_sha256"])

    def test_capture_mesh_rejects_nonbasic_grid_coordinates_and_unknown_solids(self):
        base = self._mesh_base()
        snapshot = structured_native.capture_mesh(base, self.constants, 1, [90])
        self.assertEqual(snapshot["elements"][0]["type"], "CHEXA")
        self.assertEqual(snapshot["elements"][0]["property_id"], 12)
        self.assertIsNone(snapshot["elements"][0]["block_id"])
        self.assertEqual(snapshot["integrity"]["node_count"], 8)

        base.entities[("GRID", 1)].fields["CP"] = 2
        with self.assertRaisesRegex(ValueError, "CP=0"):
            structured_native.capture_mesh(base, self.constants, 1, [90])
        base.entities[("GRID", 1)].fields["CP"] = 0
        base.entities[("SOLID", 90)].fields["type"] = "CTETRA"
        with self.assertRaisesRegex(ValueError, "outside the supported"):
            structured_native.capture_mesh(base, self.constants, 1, [90])

    def test_partition_plan_is_readonly_explicit_and_hashes_derived_assignments(self):
        base = FakeBase()
        part = base.add("ANSAPART", 5, Name="target")
        prop = base.add("PSHELL", 8, PID=8, MID1=1, T=1.0)
        face = base.add("FACE", 1, ID=1, PID=8, Thickness=1.0, Frozen="NO",
                        area=4.0, center=[0.0, 0.0, 0.0], __part__=part, __prop__=prop)
        base.face_bounds[1] = [-1.0, -1.0, -1.0, 1.0, 1.0, 1.0]
        base.face_cons[1] = [11]
        base.add("CONS", 11, ID=11, bounds=[-1.0, -1.0, 0.0, 1.0, 1.0, 0.0])
        state = FakePreviewState()
        mesh = FakeMesh(base)
        params = {
            "deck": 1,
            "face_ids": [1],
            "plane": {"origin": [0, 0, 0], "normal": [2, 0, 0]},
            "protected_face_ids": None,
        }

        internal_plan = structured_native._partition_plan(state, base, mesh, self.constants, params)
        plan = structured_native._public_plan(internal_plan)
        repeated = structured_native._partition_plan(state, base, mesh, self.constants, params)

        self.assertTrue(plan["ok"])
        self.assertTrue(plan["read_only"])
        self.assertEqual(plan["plane"]["normal"], [1.0, 0.0, 0.0])
        self.assertFalse(plan["produce_plane_faces"])
        self.assertEqual(plan["part_id"], 5)
        self.assertEqual(plan["property_id"], 8)
        self.assertFalse(plan["source_parent_mapping_supported"])
        self.assertEqual(plan["candidates"][0]["geometry_intersection_verified"], False)
        self.assertEqual(plan["plan_sha256"], repeated["plan_sha256"])
        prop.fields["T"] = 1.1
        changed = structured_native._partition_plan(state, base, mesh, self.constants, params)
        self.assertNotEqual(plan["plan_sha256"], changed["plan_sha256"])

    def test_partition_readback_rejects_outside_face_geometry_changes(self):
        def face(entity_id, bounds, area=1.0):
            return {"entity": {"type": "FACE", "id": entity_id},
                    "fields": {"PID": 8},
                    "geometry": {"bounds": bounds, "area": area}}

        def cons(entity_id, bounds):
            return {"entity": {"type": "CONS", "id": entity_id},
                    "fields": {"ID": entity_id},
                    "geometry": {"bounds": bounds}}

        before = {"entities": [
            face(1, [0, 0, 0, 2, 2, 0]), face(2, [3, 0, 0, 4, 1, 0]), cons(10, [0, 0, 0, 2, 0, 0]),
        ]}
        after = copy.deepcopy(before)
        after["entities"][0]["geometry"]["area"] = 0.5
        after["entities"].extend([
            face(3, [0.5, 0, 0, 1.5, 2, 0]), cons(11, [0.5, 0, 0, 0.5, 2, 0]),
        ])
        source = [{"id": 1, "boundary_cons_ids": [10],
                   "geometry": {"bounds": [0, 0, 0, 2, 2, 0]}}]

        local = structured_native._partition_diff(before, after, {1}, source)
        self.assertTrue(local["outside_scope_verified"])
        self.assertEqual(local["added_face_ids"], [3])
        self.assertEqual(local["added_cons_ids"], [11])

        after["entities"][1]["geometry"]["area"] = 1.1
        outside = structured_native._partition_diff(before, after, {1}, source)
        self.assertFalse(outside["outside_scope_verified"])
        self.assertTrue(any("FACE 2" in error for error in outside["errors"]))
        curve = {"entity": {"type": "CURVE", "id": 99}, "fields": {},
                 "geometry": {"bounds": [0.5, 0., 0., 0.5, 1., 0.]}}
        with_curve = {"entities": before["entities"] + [curve]}
        removed_curve = structured_native._partition_diff(with_curve, before, {1}, source)
        self.assertFalse(removed_curve["outside_scope_verified"])
        self.assertFalse(removed_curve["observable_changes"])

    def test_sweep_requires_positive_chexa_volume_and_exact_source_layer_coverage(self):
        source = {
            "nodes": [
                {"id": 1, "xyz": [0, 0, 0]}, {"id": 2, "xyz": [1, 0, 0]},
                {"id": 3, "xyz": [1, 1, 0]}, {"id": 4, "xyz": [0, 1, 0]},
            ],
            "elements": [{"id": 10, "type": "CQUAD4", "nodes": [1, 2, 3, 4]}],
        }
        output = {
            "nodes": [
                {"id": node_id, "xyz": xyz} for node_id, xyz in enumerate(
                    ([0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0],
                     [0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1]), 1
                )
            ],
            "elements": [{"id": 90, "type": "CHEXA", "nodes": list(range(1, 9)),
                          "property_id": 12, "block_id": None}],
        }
        params = {"shell_ids": [10], "steps": 1, "distance": 1.0,
                  "direction": [0.0, 0.0, 1.0], "property_id": 12}

        result = structured_native._verify_sweep_mesh(source, output, [5, 6, 7, 8], params)
        self.assertEqual(result["positive_volume_count"], 1)
        self.assertAlmostEqual(result["total_volume"], 1.0)
        self.assertEqual(result["source_layer_map"], [{"source_shell_id": 10, "layer": 1, "solid_id": 90}])
        self.assertEqual(len(result["node_map"]), 4)
        disconnected = copy.deepcopy(output)
        for node in disconnected["nodes"][:4]: node["id"] += 1000
        disconnected["elements"][0]["nodes"][:4] = [1001,1002,1003,1004]
        with self.assertRaisesRegex(ValueError, "source GRID identity"):
            structured_native._verify_sweep_mesh(source, disconnected, [5,6,7,8], params)
        source_params = {"shell_ids": [10], "steps": 1, "distance": 1., "direction": [0., 0., 1.]}
        self.assertEqual(structured_native._source_preflight(source, source_params)["status"], "incomplete")
        with self.assertRaisesRegex(ValueError, "perpendicular"):
            structured_native._source_preflight(source, {**source_params, "direction": [1., 0., 0.]})

        inverted = copy.deepcopy(output)
        inverted["elements"][0]["nodes"] = [2, 1, 3, 4, 6, 5, 7, 8]
        with self.assertRaisesRegex(ValueError, "non-positive"):
            structured_native._verify_sweep_mesh(source, inverted, [5, 6, 7, 8], params)

    def test_native_exception_is_recorded_once_and_never_retried(self):
        class ExplodingExtrude:
            calls = 0

            def translate(self, **_kwargs):
                type(self).calls += 1
                raise RuntimeError("native failure after submit")

        class FakeState:
            pass

        base = types.SimpleNamespace(CurrentDeck=lambda: 1, SaveAs=lambda *_args, **_kwargs: 0,
            GetCurrentEntity=lambda *_args: None, SetCurrentEntity=lambda *_args, **_kwargs: 0)
        mesh = types.SimpleNamespace(VolumesExtrude=ExplodingExtrude)
        constants = types.SimpleNamespace(NASTRAN=1)
        shell = FakeEntity(10, "SHELL", {})
        reference = {"deck": 1, "type": "SHELL", "id": 10}
        before = {"complete": True, "deck": 1, "entities": [
            {"entity": reference, "fields": {}, "card_fields": []},
        ]}
        digest = "a" * 64
        source = {"complete": True, "snapshot_sha256": digest,
                  "elements": [{"id": 10, "type": "CQUAD4", "nodes": [1, 2, 3, 4]}],
                  "nodes": []}
        params = {
            "deck": 1, "shell_ids": [10], "direction": [0, 0, 1], "distance": 1.0,
            "steps": 1, "property_id": 12, "expected_source_sha256": digest,
            "backup_path": "unused.ansa",
        }
        with (
            patch.object(volume_meshing, "_execution_context", return_value={"deck": 1}),
            patch.object(volume_meshing, "_same_context"),
            patch.object(structured_native, "_capture_scope", return_value=before),
            patch.object(structured_native, "capture_source", return_value=source),
            patch.object(structured_native, "_source_preflight", return_value={"status": "incomplete"}),
            patch.object(structured_native, "_psolid_preflight", return_value=(object(), {})),
            patch.object(structured_native, "_backup_preflight", return_value=(Path("unused.ansa"), "model.ansa", {})),
            patch.object(structured_native, "_verify_backup", return_value={"verified": True}),
            patch.object(structured_native.catalog, "_get_entity", return_value=(shell, reference)),
        ):
            result = structured_native._run_sweep(FakeState(), base, mesh, constants, params)

        self.assertEqual(ExplodingExtrude.calls, 1)
        self.assertFalse(result["ok"])
        self.assertTrue(result["possible_effects"])
        self.assertIn("native failure after submit", result["api_error"])
        self.assertEqual(result["api_return_code"], None)

    def test_native_volume_coverage_and_unused_auxiliary_scope_are_checked(self):
        def row(kind, eid, fields, **extra):
            return {"entity":{"type":kind,"id":eid},"fields":fields,**extra}
        params={"property_id":12,"steps":2,"distance":2.}
        before={"entities":[row("PSOLID",12,{"PID":12,"MID":1})]}
        after={"entities":before["entities"]+[
            row("SOLID",100,{"PID":12}),
            row("VOLUME",2,{"Type":"Extrude","Algorithm":"Translate","PID":12,
                "Extrude steps":2,"Distance":2.,"Order":"1st","Status":"Meshed"},solid_ids=[100]),
            row("PSOLID",13,{"PID":13,"MID":2}),row("MAT1",2,{"MID":2})]}
        self.assertTrue(structured_native._sweep_scope_diff(before,after,params)["scope_verified"])
        after["entities"][1]["fields"]["PID"]=13
        self.assertFalse(structured_native._sweep_scope_diff(before,after,params)["scope_verified"])
        after["entities"][1]["fields"]["PID"]=12
        after["entities"][2]["solid_ids"]=[999]
        self.assertFalse(structured_native._sweep_scope_diff(before,after,params)["scope_verified"])

    def test_sweep_diff_allows_only_evidenced_face_volume_count_increment(self):
        def row(kind, eid, fields, **extra):
            return {"entity":{"type":kind,"id":eid},"fields":fields,**extra}

        params = {"property_id": 12, "steps": 1, "distance": 1.0}

        def scopes(face_counts=(" 0",)):
            faces = [row("FACE", index + 1,
                {"Num. Volumes": count, "Thickness": " 2.0"},
                card_fields=["Num. Volumes", "Thickness"],
                geometry={"area": 1.0, "center": [0.0, 0.0, 0.0]},
                assignments={"__part__": 4, "__prop__": 12})
                for index, count in enumerate(face_counts)]
            before = {"entities": faces + [row("PSOLID", 12, {"PID": 12})]}
            after = copy.deepcopy(before)
            after["entities"].extend([
                row("SOLID", 100, {"PID": 12}),
                row("VOLUME", 7, {"Type": "Extrude", "Algorithm": "Translate", "PID": 12,
                    "Extrude steps": 1, "Distance": 1.0, "Order": "1st", "Status": "Meshed"},
                    solid_ids=[100]),
            ])
            return before, after

        before, after = scopes()
        after["entities"][0]["fields"]["Num. Volumes"] = " 1"
        verified = structured_native._sweep_scope_diff(
            before, after, params, volume_boundary_faces={7: [1]}
        )
        self.assertTrue(verified["scope_verified"], verified["errors"])

        missing_evidence = structured_native._sweep_scope_diff(before, after, params)
        self.assertFalse(missing_evidence["scope_verified"])
        self.assertTrue(any("without native VOLUME boundary evidence" in error
                            for error in missing_evidence["errors"]))
        self.assertFalse(structured_native._sweep_scope_diff(
            before, after, None, volume_boundary_faces={7: [1]}
        )["scope_verified"])
        self.assertFalse(structured_native._sweep_scope_diff(
            before, after, params, volume_boundary_faces={8: [1]}
        )["scope_verified"])
        self.assertFalse(structured_native._sweep_scope_diff(
            before, after, params, volume_boundary_faces={7: [99]}
        )["scope_verified"])

        unrelated_before, unrelated_after = scopes((" 0", " 0"))
        unrelated_after["entities"][0]["fields"]["Num. Volumes"] = " 1"
        unrelated_after["entities"][1]["fields"]["Num. Volumes"] = " 1"
        unrelated = structured_native._sweep_scope_diff(
            unrelated_before, unrelated_after, params, volume_boundary_faces={7: [1]}
        )
        self.assertFalse(unrelated["scope_verified"])
        self.assertIn(["FACE", 2], unrelated["changed_ids"])

        wrong_increment = copy.deepcopy(after)
        wrong_increment["entities"][0]["fields"]["Num. Volumes"] = " 2"
        self.assertFalse(structured_native._sweep_scope_diff(
            before, wrong_increment, params, volume_boundary_faces={7: [1]}
        )["scope_verified"])

        for changed_part in ("Thickness", "geometry", "assignments"):
            with self.subTest(changed_part=changed_part):
                cad_change = copy.deepcopy(after)
                if changed_part == "Thickness":
                    cad_change["entities"][0]["fields"]["Thickness"] = " 3.0"
                else:
                    cad_change["entities"][0][changed_part]["changed"] = True
                result = structured_native._sweep_scope_diff(
                    before, cad_change, params, volume_boundary_faces={7: [1]}
                )
                self.assertFalse(result["scope_verified"])
                self.assertIn(["FACE", 1], result["changed_ids"])

    def test_sweep_volume_face_evidence_requires_complete_single_face_macro(self):
        base = FakeBase()
        face = base.add("FACE", 1, **{"Num. Volumes": " 0"})
        shell = base.add("SHELL", 10)
        base.add("VOLUME", 20)
        before = {"entities": [
            {"entity": {"type": "FACE", "id": 1}},
            {"entity": {"type": "SHELL", "id": 10}},
        ]}
        after = {"entities": before["entities"] + [
            {"entity": {"type": "VOLUME", "id": 20}},
        ]}
        mesh = types.SimpleNamespace(
            GetVolumeBoundary=lambda _volume: [face],
            GetMacroShells=lambda _macros: [shell],
        )

        evidence = structured_native._collect_sweep_volume_boundary_faces(
            base, mesh, 1, [20], [10], before, after
        )
        self.assertEqual(evidence, {20: [1]})

        mesh.GetMacroShells = lambda _macros: 0
        with self.assertRaisesRegex(RuntimeError, "GetMacroShells returned 0"):
            structured_native._collect_sweep_volume_boundary_faces(
                base, mesh, 1, [20], [10], before, after
            )

        mesh.GetMacroShells = lambda _macros: [base.add("SHELL", 11)]
        with self.assertRaisesRegex(RuntimeError, "outside the requested sweep scope"):
            structured_native._collect_sweep_volume_boundary_faces(
                base, mesh, 1, [20], [10], before, after
            )

        second_face = base.add("FACE", 2)
        base.GetFacesOfMacros = lambda _macros: [face, second_face]
        mesh.GetMacroShells = lambda _macros: [shell]
        with self.assertRaisesRegex(RuntimeError, "single-FACE macro"):
            structured_native._collect_sweep_volume_boundary_faces(
                base, mesh, 1, [20], [10], before, after
            )


if __name__ == "__main__":
    unittest.main()
