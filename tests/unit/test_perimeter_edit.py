"""Risks specific to editing existing native perimeter points."""

import copy
from contextlib import ExitStack
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ansa_mcp_bridge.operations import perimeter_edit as edit
from tests.unit.test_cons_seed import Entity, _audit_case, _row


def boundary(ident=10, y=0., middle=.5):
    return {"cons_id": ident, "face_ids": [5], "status": "supported", "segments": 2,
            "point_count": 3, "endpoint_grid_ids": [1, 2],
            "points": [{"id": 1, "xyz": [0., y, 0.]}, {"id": 9, "xyz": [middle, y, 0.]},
                       {"id": 2, "xyz": [1., y, 0.]}],
            "hot_points": [{"id": 101, "xyz": [0., y, 0.]}, {"id": 102, "xyz": [1., y, 0.]}],
            "native_fields": {"Collapsed": "NO", "Frozen": "NO", "Biasing": "LINEAR"}}


def params():
    return {"deck": 1, "face_ids": [5], "expected_seed_snapshot_sha256": "a"*64,
            "backup_path": "new.ansa", "edit": {"action": "number", "expected_scope_sha256": "b"*64,
            "count_type": "edges", "assignments": [{"cons_id": 10, "count": 2}]}}


def scope():
    return {"context": {"epoch": 1}, "selection": {"entity_ids": [10]}, "scope_sha256": "b"*64,
            "faces": [{"entity": {"id": 5}, "macro_id": 5, "cons_ids": [10, 20], "volume_ids": []}],
            "macros": [{"macro_id": 5, "face_ids": [5], "shell_ids": [], "primary": True}],
            "cons": [{"entity": {"id": 10}, "face_ids": [5], "unexpanded_face_ids": [],
                      "effective_macro_protection": {"value": False}}], "volumes": []}


def prepared():
    result, before, _ = _audit_case()
    before["entities"].append(_row("GRID", 9, fields={"X1": .5, "X2": 0., "X3": 0.}))
    default = {"TYPE": "GRID", "NID": 9, "CP": None, "CD": None, "PS": None, "SEID": None,
               "X1": .5, "X2": 0., "X3": 0., "FROZEN_ID": "NO", "FROZEN_DELETE": "NO",
               "DEFINED": "YES", "AUXILIARY": "NO", "Name": "", "Comment": "", "MBContainers": []}
    before["grids"].append({"id": 9, "card": {"card_fields": list(default), "fields": default}})
    before["perimeters"][10] = [1, 9, 2]
    result.update(context={"epoch": 1}, scope=scope(), backup=("new.ansa",),
                  requested={10: 2}, protected_grid_ids=[1, 2, 7, 8], boundaries=[boundary()], set_nodes={})
    result["witness"].update(seed_snapshot_sha256="a"*64, boundaries=[boundary()])
    return result


class PerimeterEditTests(unittest.TestCase):
    def setUp(self):
        self.state = SimpleNamespace(assert_main=Mock())
        self.base = SimpleNamespace(NodesToElements=lambda nodes, **kw: {n: [] for n in nodes},
            GetEntity=lambda deck, kind, ident: Entity(kind, ident),
            GetEntityCardValues=lambda deck, entity, fields: {"__id__": entity._id, "__type__": entity.kind})
        self.constants = SimpleNamespace(NASTRAN=1)
        self.mesh = SimpleNamespace(NumberPerimeters=Mock(return_value=1),
            ApplyNewLengthToMacros=Mock(return_value=0), PerimetersSpacingAlign=Mock(return_value=0))

    def test_normalization_rejects_ambiguous_and_unbounded_requests(self):
        for change in (True, 0, 10001):
            request = params()
            request["edit"]["assignments"][0]["count"] = change
            with self.subTest(count=change), self.assertRaises(ValueError):
                edit.normalize(request)
        request = params()
        request["edit"]["count_type"] = "nodes"
        request["edit"]["assignments"][0]["count"] = 1
        with self.assertRaises(ValueError):
            edit.normalize(request)
        request = params()
        request["edit"]["assignments"] *= 2
        with self.assertRaises(ValueError):
            edit.normalize(request)
        request = params()
        request["edit"]["remesh"] = True
        with self.assertRaises(ValueError):
            edit.normalize(request)

    def _preflight(self, observed):
        clean, prep = edit.normalize(params()), prepared()
        with ExitStack() as stack:
            stack.enter_context(patch.object(edit.guards, "_execution_context", return_value={"epoch": 1}))
            stack.enter_context(patch.object(edit.guards, "_same_context"))
            stack.enter_context(patch.object(edit.mesh_scope, "capture", return_value=observed))
            stack.enter_context(patch.object(edit.seeds, "capture", return_value=prep["witness"]))
            guard = stack.enter_context(patch.object(edit.seeds, "_guard_snapshot", return_value=prep["before"]))
            backup = stack.enter_context(patch.object(edit.native, "_backup_preflight", return_value=()))
            stack.enter_context(patch.object(edit.seeds, "_point_budget"))
            stack.enter_context(patch.object(edit.seeds, "_collect", return_value=[]))
            result = {}
            try:
                value = edit._preflight(self.state, self.base, self.mesh, self.constants, clean, result)
                return value, result, guard.call_count, backup.call_count
            except Exception as exc:
                return exc, result, guard.call_count, backup.call_count

    def test_stale_frozen_meshed_and_incomplete_macro_reject_before_backup(self):
        changes = [lambda s: s.update(scope_sha256="c"*64),
                   lambda s: s["cons"][0]["effective_macro_protection"].update(value=True),
                   lambda s: s["macros"][0].update(shell_ids=[77]),
                   lambda s: s["macros"][0].update(face_ids=[5, 6]),
                   lambda s: s["volumes"].append({"face_ids": [5], "solid_ids": [88]})]
        for mutate in changes:
            observed = scope(); mutate(observed)
            with self.subTest(scope=observed):
                failure, result, guards_read, backups = self._preflight(observed)
                self.assertIsInstance(failure, ValueError)
                self.assertEqual((guards_read, backups), (0, 0))
                if observed["macros"][0]["shell_ids"]:
                    self.assertEqual(result["blocked_macro_shell_ids"], [77])
        self.assertIsInstance(self._preflight(scope())[0], dict)

    def test_existing_unreferenced_interior_is_editable_but_endpoint_and_outside_are_fixed(self):
        prep = prepared(); result = {}
        with (patch.object(edit.seeds, "_guard_snapshot", return_value=copy.deepcopy(prep["before"])),
              patch.object(edit.seeds, "_boundary", return_value=boundary()),
              patch.object(edit.features, "_entity", side_effect=lambda b, d, k, i: Entity(k, i))):
            observed = edit._materialize(self.state, self.base, edit.normalize(params()), prep, result)
        self.assertEqual(result["editable_interior_grid_ids"], [9])
        self.assertEqual(observed["protected_grid_ids"], [1, 2, 7, 8])

    def test_permanent_constraint_and_nondefault_grid_definitions_cannot_be_discarded(self):
        for field, value in (("PS", 123), ("CD", 42), ("SEID", 2), ("FROZEN_DELETE", "YES"),
                             ("Name", "retained engineering node"), ("PS", "unknown")):
            prep = prepared()
            prep["before"]["grids"][-1]["card"]["fields"][field] = value
            result = {}
            with (patch.object(edit.seeds, "_guard_snapshot", return_value=prep["before"]),
                  patch.object(edit.seeds, "_boundary", return_value=boundary())):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    edit._materialize(self.state, self.base, edit.normalize(params()), prep, result)
            self.assertEqual(result["blocked_defined_grid_ids"], [9])
            self.mesh.NumberPerimeters.assert_not_called()
        row = prepared()["before"]["grids"][-1]
        row["card"]["fields"].pop("PS")
        with self.assertRaisesRegex(ValueError, "incomplete"):
            edit._virtual_definition(row)

    def test_solver_or_set_referenced_interior_is_rejected_before_api(self):
        for in_set in (False, True):
            prep = prepared(); result = {}
            self.base.NodesToElements = lambda nodes, **kw: {n: ([] if in_set else [Entity("SPC", 1)]) for n in nodes}
            with (patch.object(edit.seeds, "_guard_snapshot", return_value=copy.deepcopy(prep["before"])),
                  patch.object(edit.seeds, "_boundary", return_value=boundary()),
                  patch.object(edit.features, "_entity", side_effect=lambda b, d, k, i: Entity(k, i)),
                  patch.object(edit, "_set_nodes", return_value={1: [9]} if in_set else {})):
                with self.subTest(in_set=in_set), self.assertRaisesRegex(ValueError, "references"):
                    edit._materialize(self.state, self.base, edit.normalize(params()), prep, result)
            self.assertEqual(result["blocked_referenced_grid_ids"], [9])
            self.mesh.NumberPerimeters.assert_not_called()

    def test_shared_interior_and_missing_attachment_evidence_are_rejected(self):
        prep = prepared(); prep["before"]["perimeters"][20].append(9)
        with (patch.object(edit.seeds, "_guard_snapshot", return_value=prep["before"]),
              patch.object(edit.seeds, "_boundary", return_value=boundary())):
            with self.assertRaisesRegex(ValueError, "shared"):
                edit._materialize(self.state, self.base, edit.normalize(params()), prep, {})
        prep = prepared(); self.base.NodesToElements = lambda nodes, **kw: {}
        with (patch.object(edit.seeds, "_guard_snapshot", return_value=prep["before"]),
              patch.object(edit.seeds, "_boundary", return_value=boundary()),
              patch.object(edit.features, "_entity", side_effect=lambda b, d, k, i: Entity(k, i))):
            with self.assertRaisesRegex(RuntimeError, "omits"):
                edit._materialize(self.state, self.base, edit.normalize(params()), prep, {})

    def test_number_total_nodes_are_converted_to_edges_without_native_node_ambiguity(self):
        request = params(); request["edit"].update(count_type="nodes", assignments=[{"cons_id": 10, "count": 8}])
        clean = edit.normalize(request); prep = prepared(); prep["requested"] = edit._expected(clean, prep["witness"])
        with (patch.object(edit.guards, "_same_context"),
              patch.object(edit.features, "_entity", side_effect=lambda b, d, k, i: Entity(k, i))):
            result = {"native_calls": []}; edit._invoke(self.state, self.base, self.mesh, clean, prep, result)
        self.assertEqual(self.mesh.NumberPerimeters.call_args.args[1], "7")
        self.assertEqual(self.mesh.NumberPerimeters.call_args.kwargs, {"apply_multiple_number": False,
            "remesh_macros": False, "apply_number_of": "edges", "use_ansa_defaults_values": False})

    def test_align_passes_both_chains_and_disables_cuts_freeze_and_remesh(self):
        request = params(); request["edit"] = {"action": "align", "expected_scope_sha256": "b"*64,
            "source_cons_id": 20, "source_start_hot_point_id": 201,
            "targets": [{"cons_id": 10, "start_hot_point_id": 101}]}
        with (patch.object(edit.guards, "_same_context"),
              patch.object(edit.features, "_entity", side_effect=lambda b, d, k, i: Entity(k, i))):
            edit._invoke(self.state, self.base, self.mesh, edit.normalize(request), prepared(), {"native_calls": []})
        args, kwargs = self.mesh.PerimetersSpacingAlign.call_args
        self.assertEqual([e._id for e in args[0]], [10, 20])
        self.assertEqual([e._id for e in args[1]], [20])
        self.assertEqual(kwargs["fields"], edit._ALIGN_FIELDS)

    def test_native_length_zero_is_normal_and_bad_number_return_is_not_retried(self):
        request = params(); request["edit"] = {"action": "length", "expected_scope_sha256": "b"*64,
                                              "cons_ids": [10], "target_length": 2.5}
        with (patch.object(edit.guards, "_same_context"),
              patch.object(edit.features, "_entity", side_effect=lambda b, d, k, i: Entity(k, i))):
            edit._invoke(self.state, self.base, self.mesh, edit.normalize(request), prepared(), {"native_calls": []})
            self.mesh.NumberPerimeters.return_value = 0
            with self.assertRaisesRegex(RuntimeError, "status"):
                edit._invoke(self.state, self.base, self.mesh, edit.normalize(params()), prepared(), {"native_calls": []})
        self.mesh.NumberPerimeters.assert_called_once()

    def test_nonuniform_distribution_retains_declared_orientation(self):
        self.assertEqual(edit._fractions(boundary(middle=.2), 101), [0., .2, 1.])
        reversed_values = edit._fractions(boundary(middle=.2), 102)
        self.assertAlmostEqual(reversed_values[1], .8)
        bent = boundary(); bent["points"][1]["xyz"][1] = .1
        with self.assertRaisesRegex(ValueError, "straight"):
            edit._fractions(bent, 101)
        with self.assertRaisesRegex(ValueError, "endpoint"):
            edit._fractions(boundary(), 999)

    def test_align_verifies_distribution_positions_not_only_counts(self):
        request = params(); request["edit"] = {"action": "align", "expected_scope_sha256": "b"*64,
            "source_cons_id": 20, "source_start_hot_point_id": 201,
            "targets": [{"cons_id": 10, "start_hot_point_id": 101}]}
        for fraction in (.2, .8):
            prep = prepared(); source = boundary(20, 2., .2)
            source["hot_points"][0]["id"] = 201; source["hot_points"][1]["id"] = 202
            source["points"] = [{"id": i, "xyz": [x, 2., 0.]} for i, x in ((7, 0.), (30, .2), (8, 1.))]
            source["endpoint_grid_ids"] = [7, 8]
            prep["boundaries"].append(copy.deepcopy(source)); prep["requested"][20] = 2
            target = boundary(middle=fraction)
            observed = {"seed_snapshot_sha256": "a"*64, "boundaries": [target, source]}
            with (patch.object(edit.guards, "_same_context"),
                  patch.object(edit.seeds, "capture", return_value=observed),
                  patch.object(edit.seeds, "_guard_snapshot", return_value=prep["before"]),
                  patch.object(edit.seeds, "_boundary", side_effect=[target, source]),
                  patch.object(edit.features, "_entity", side_effect=lambda b, d, k, i: Entity(k, i)),
                  patch.object(edit.seeds, "_audit", return_value=([], []))):
                result = {"source_distribution": [0., .2, 1.]}
                calc = SimpleNamespace(ProjectPointToCons=lambda xyz, edge: [xyz])
                if fraction == .8:
                    with self.assertRaisesRegex(RuntimeError, "distribution differs"):
                        edit._verify(self.state, self.base, calc, edit.normalize(request), prep, result)
                else:
                    edit._verify(self.state, self.base, calc, edit.normalize(request), prep, result)

    def test_full_audit_keeps_outside_fe_endpoints_and_cad_protected(self):
        for mutation in (None, "FE", "endpoint", "CAD", "count"):
            prep = prepared(); after = copy.deepcopy(prep["before"]); row = boundary()
            if mutation == "FE": after["entities"][8]["fields"]["PID"] = 99
            if mutation == "endpoint": after["entities"][4]["fields"]["X1"] = .1
            if mutation == "CAD": after["cad_sha256"] = "c"*64
            if mutation == "count": row["segments"] = 3
            readback = {"seed_snapshot_sha256": "a"*64, "boundaries": [row]}
            with (patch.object(edit.guards, "_same_context"),
                  patch.object(edit.seeds, "capture", return_value=readback),
                  patch.object(edit.seeds, "_guard_snapshot", return_value=after),
                  patch.object(edit.seeds, "_boundary", return_value=row),
                  patch.object(edit.features, "_entity", side_effect=lambda b, d, k, i: Entity(k, i))):
                calc = SimpleNamespace(ProjectPointToCons=lambda xyz, edge: [xyz])
                if mutation:
                    with self.subTest(mutation=mutation), self.assertRaises(RuntimeError):
                        edit._verify(self.state, self.base, calc, edit.normalize(params()), prep, {})
                else:
                    edit._verify(self.state, self.base, calc, edit.normalize(params()), prep, {})

    def test_configuration_change_backup_exception_and_native_exception_keep_recovery_evidence(self):
        for failure in (None, "configuration", "backup", "native", "relations"):
            prep = prepared(); observed = scope()
            configs = [{"snapshot_sha256": "a"*64}, {"snapshot_sha256": ("c" if failure == "configuration" else "a")*64}]
            if failure == "relations": observed["macros"][0]["face_ids"] = [5, 6]
            with (patch.object(edit, "_preflight", return_value=prep),
                  patch.object(edit.meshing_context, "capture", side_effect=configs),
                  patch.object(edit.native, "_verify_backup", side_effect=RuntimeError("backup failed") if failure == "backup" else None),
                  patch.object(edit.guards, "_same_context"),
                  patch.object(edit.seeds, "_guard_snapshot", return_value=prep["before"]),
                  patch.object(edit.mesh_scope, "capture", side_effect=[scope(), observed]),
                  patch.object(edit, "_materialize", return_value=prep),
                  patch.object(edit, "_verify"),
                  patch.object(edit.features, "_entity", side_effect=lambda b, d, k, i: Entity(k, i)),
                  patch.object(edit.seeds, "_boundary", return_value=boundary())):
                self.mesh.NumberPerimeters.side_effect = RuntimeError("partial native failure") if failure == "native" else None
                self.mesh.NumberPerimeters.return_value = 1
                result = edit.apply(self.state, self.base, self.mesh, object(), self.constants, params())
            with self.subTest(failure=failure):
                self.assertEqual(result["ok"], failure is None)
                self.assertEqual(result["recovery_required"], failure is not None)
                if failure:
                    self.assertTrue(result["observed_boundaries"])
                if failure == "backup":
                    self.assertFalse(result["api_called"])


if __name__ == "__main__":
    unittest.main()
