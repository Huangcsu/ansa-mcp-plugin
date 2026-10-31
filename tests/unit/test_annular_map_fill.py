import copy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ansa_mcp_bridge.operations import annular_map_fill as annular, map_block_fill as fill
from ansa_mcp_bridge.operations.map_block_fill_audit import audit_many_changes
from test_annular_source import options, ring
from test_map_block_fill_audit import _row, _two_block_fixture


def params():
    return {"deck": 1, "volume_ids": [4, 2, 3, 1], "annular_options": {
        "source_face_ids": [6, 19, 25, 28], "center": [0., 0., 0.], "axis": [0., 0., 1.],
        "inner_radius": 1., "outer_radius": 2., "distance": 3., "max_chord_error": .05}}


def mixed_blocks():
    before, after, blocks, aggregate = _two_block_fixture()
    output = blocks[0]["output"]
    output["elements"][0].update(type="CPENTA", nodes=[1, 2, 3, 4, 5, 6])
    output["nodes"] = [r for r in output["nodes"] if r["id"] not in (7, 8)]
    output["integrity"].update(element_types={"CPENTA": 1}, node_count=6)
    raw = _row(after, "SOLID", 100)
    raw["fields"].update(type="CPENTA")
    for name in ("G7", "G8"):
        raw["fields"].pop(name)
        raw["card_fields"].remove(name)
    _row(after, "VOLUME", 1)["fields"].update(Hexas=0, Pentas=1, Grids=6, **{"Internal grids": 2})
    after["entities"] = [r for r in after["entities"] if (r["entity"]["type"], r["entity"]["id"]) not in (("GRID", 7), ("GRID", 8))]
    after["grids"] = [r for r in after["grids"] if r["id"] not in (7, 8)]
    return before, after, blocks, aggregate


class AnnularFillTests(unittest.TestCase):
    def test_explicit_route_keeps_legacy_strict_and_rejects_malformed_options(self):
        clean = fill.normalize(params())
        self.assertEqual(clean["volume_ids"], [1, 2, 3, 4])
        self.assertEqual(fill.normalize({"deck": 1, "volume_ids": [2, 1]}), {"deck": 1, "volume_ids": [1, 2]})
        bad = [dict(params(), volume_id=1), dict(params(), volume_ids=[1, 2, 3]), dict(params(), annular_options=None)]
        for key, value in (("axis", [0, 0, 2]), ("inner_radius", 2), ("distance", float("inf")),
                           ("source_face_ids", [1, 1, 2, 3]), ("extra", True)):
            row = params()
            row["annular_options"][key] = value
            bad.append(row)
        for row in bad:
            with self.subTest(row=row), self.assertRaises((ValueError, TypeError)):
                fill.normalize(row)

    def test_cap_gap_and_nodes_outside_the_declared_hole_or_axial_extent_fail(self):
        cap = ring()
        opts = {**options(), "distance": 3.}
        self.assertEqual(annular._cap_audit(cap, opts, [0, 0, 0])["status"], "verified_mesh_template")
        cap["elements"].pop()
        with self.assertRaises(ValueError):
            annular._cap_audit(cap, opts, [0, 0, 0])
        for point in ([0, 0, 1], [2.1, 0, 1], [1.5, 0, 3.1]):
            with self.subTest(point=point), self.assertRaises(ValueError):
                annular._node_envelope({"nodes": [{"id": 1, "xyz": point}]}, opts)

    def test_existing_solid_rejected_before_export_or_native_write(self):
        with patch.object(annular.inspection, "_deck", return_value=1), patch.object(annular.native, "_check_nastran"), \
             patch.object(annular.guards, "_execution_context", return_value={}), \
             patch.object(annular.seeds, "_collect", return_value=[object()]), \
             patch.object(annular.map_block_inspection, "capture") as capture:
            mesh = Mock()
            with self.assertRaises(ValueError):
                fill.preflight(SimpleNamespace(assert_main=Mock()), Mock(), mesh, SimpleNamespace(NASTRAN=1), params())
            mesh.VolumesRemesh.assert_not_called()
            capture.assert_not_called()

    def test_one_native_call_and_recovery_inventory_on_failure_without_replay(self):
        clean = annular.normalize(params())
        prepared = {"annular": True, "clean": clean, "context": {}, "before": {"entities": []}, "plan_sha256": "a"*64}
        after = {"entities": [{"entity": {"type": "SOLID", "id": 99}}, {"entity": {"type": "GRID", "id": 45}}]}
        for returned, readback_error in ((0, False), (1, True), (1, False)):
            mesh = Mock()
            mesh.VolumesRemesh.return_value = returned
            with patch.object(annular.native, "_backup_preflight", return_value=()), \
                 patch.object(annular.native, "_verify_backup", return_value={"verified": True}), \
                 patch.object(annular, "preflight", return_value=prepared), patch.object(annular.guards, "_same_context"), \
                 patch.object(annular.features, "_entity", side_effect=lambda b, d, k, i: i), \
                 patch.object(annular, "readback", side_effect=RuntimeError("readback unavailable") if readback_error else None,
                              return_value={"new_solid_ids": [99], "new_grid_ids": [45]}), \
                 patch.object(annular.cad_map, "_snapshot", return_value=after):
                result = fill.apply_prepared(Mock(), Mock(), mesh, Mock(),
                    {**clean, "expected_plan_sha256": "a"*64, "backup_path": "owned.ansa"}, prepared)
            mesh.VolumesRemesh.assert_called_once_with([1, 2, 3, 4])
            self.assertEqual(result["ok"], returned == 1 and not readback_error)
            self.assertEqual(result["recovery_required"], not result["ok"])
            self.assertEqual(result["new_solid_ids"], [99])

    def test_changed_plan_prevents_backup_and_unknown_readback_stays_unknown(self):
        clean = annular.normalize(params())
        prepared = {"annular": True, "clean": clean, "context": {}, "before": {"entities": []}, "plan_sha256": "a"*64}
        mesh = Mock()
        with patch.object(annular.native, "_backup_preflight") as backup:
            result = annular.apply_prepared(Mock(), Mock(), mesh, Mock(),
                {**clean, "expected_plan_sha256": "b"*64, "backup_path": "owned.ansa"}, prepared)
        backup.assert_not_called()
        mesh.VolumesRemesh.assert_not_called()
        self.assertFalse(result["possible_effects"])
        with patch.object(annular.native, "_backup_preflight", return_value=()), \
             patch.object(annular.native, "_verify_backup", side_effect=RuntimeError("save uncertain")), \
             patch.object(annular.guards, "_same_context"), patch.object(annular.cad_map, "_snapshot", side_effect=RuntimeError("unreadable")):
            result = annular.apply_prepared(Mock(), Mock(), mesh, Mock(),
                {**clean, "expected_plan_sha256": "a"*64, "backup_path": "owned.ansa"}, prepared)
        self.assertTrue(result["recovery_required"])
        self.assertIsNone(result["new_solid_ids"])
        self.assertFalse(result["inventory_observation_complete"])
        mesh.VolumesRemesh.assert_not_called()

    def test_mixed_scope_is_opt_in_and_retains_counts_connectivity_and_card_protection(self):
        before, after, blocks, aggregate = mixed_blocks()
        with self.assertRaises(RuntimeError):
            audit_many_changes(before, after, blocks=blocks, part_aggregate=aggregate)
        result = audit_many_changes(before, after, blocks=blocks, part_aggregate=aggregate, allow_penta=True)
        self.assertEqual(result["new_solid_ids"], [100, 101])
        for label, mutate in (("count", lambda a, b: _row(a, "VOLUME", 1)["fields"].update(Pentas=2)),
                              ("connectivity", lambda a, b: _row(a, "SOLID", 100)["fields"].update(G6=9)),
                              ("material", lambda a, b: _row(a, "MAT1", 9)["fields"].update(E=1)),
                              ("ownership", lambda a, b: b[0]["output"]["elements"][0].update(block_id="VOLUME:4"))):
            modified_after, modified_blocks = copy.deepcopy(after), copy.deepcopy(blocks)
            mutate(modified_after, modified_blocks)
            with self.subTest(label=label), self.assertRaises((RuntimeError, ValueError)):
                audit_many_changes(before, modified_after, blocks=modified_blocks, part_aggregate=aggregate, allow_penta=True)


if __name__ == "__main__":
    unittest.main()
