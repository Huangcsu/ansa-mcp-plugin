"""Native state is not a successful Map result; freshness covers actual mesh."""
import copy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ansa_mcp_bridge.operations import map_block_inspection as operation


class Entity:
    def __init__(self, kind, ident): self.kind, self._id = kind, ident
    def ansa_type(self, deck): return self.kind


class MapBlockInspectionTests(unittest.TestCase):
    def setUp(self):
        self.faces = [Entity("FACE", i) for i in (1, 2, 3)]
        self.volume = Entity("VOLUME", 1)
        self.part = Entity("ANSAPART", 2)
        self.state = SimpleNamespace(assert_main=Mock())
        self.fields = {"Type": "Map Block", "State": "Ready to Mesh", "Status": "Unmeshed", "PID": 3}
        self.caps = {"master": [self.faces[0]], "slave": [self.faces[1]], "round": [self.faces[2]]}
        self.mesh = SimpleNamespace(GetVolumeBoundary=Mock(return_value=self.faces), GetMapVolumeBoundaries=Mock(side_effect=lambda v: self.caps))
        self.base = SimpleNamespace(CurrentDeck=lambda: 1, GetEntityPart=lambda e: self.part,
                                    CollectEntities=lambda d, v, k: self.faces,
                                    GetEntityCardValues=lambda d, e, f: {"__id__": e._id, "__type__": e.kind})
        self.xyz = [0., 0., 0.]
        membership = [{"face_id": i, "macro_shell_ids": [100+i], "membership_complete": True} for i in (1, 2, 3)]
        patches = [
            patch.object(operation.catalog, "_get_entity", return_value=(self.volume, {"type": "VOLUME", "id": 1})),
            patch.object(operation.guards, "_read_full_card", side_effect=lambda *a, **k: {"entity": {"type": "VOLUME", "id": 1}, "fields": copy.deepcopy(self.fields)}),
            patch.object(operation.structured_boundary, "capture_membership", return_value=(membership, {})),
            patch.object(operation.native, "_read_elements", return_value=[{"id": 101, "type": "CQUAD4", "nodes": [1, 2, 3, 4], "property_id": 1}]),
            patch.object(operation.native, "_read_grids", side_effect=lambda *a: [{"id": 1, "xyz": list(self.xyz)}]),
            patch.object(operation.features, "_witness", return_value=({"source_snapshot_sha256": "a"*64}, {})),
            patch.object(operation.features, "_context", return_value={"epoch": 1}),
        ]
        for item in patches:
            item.start(); self.addCleanup(item.stop)

    def capture(self):
        return operation.capture(self.state, self.base, self.mesh, SimpleNamespace(NASTRAN=1), 1, [1])

    def test_native_ready_requires_complete_disjoint_caps_but_grants_no_write(self):
        result = self.capture()
        self.assertEqual(result["summary"]["native_ready_count"], 1)
        self.assertTrue(result["volumes"][0]["caps_match_complete_boundary"])
        self.assertFalse(result["usable_for_mutation"])
        self.caps["round"] = []
        incomplete = self.capture()
        self.assertEqual(incomplete["summary"]["native_ready_count"], 0)
        self.assertEqual(incomplete["summary"]["incomplete_count"], 1)
        self.caps["round"] = [self.faces[0]]
        self.assertEqual(self.capture()["summary"]["native_ready_count"], 0)

    def test_non_mappable_native_success_remains_non_mappable(self):
        self.fields["State"] = "Non Mappable"
        self.caps = {k: [] for k in self.caps}
        result = self.capture()
        self.assertEqual(result["summary"]["native_non_mappable_count"], 1)
        self.assertEqual(result["summary"]["native_ready_count"], 0)

    def test_same_count_same_epoch_node_change_invalidates_evidence(self):
        result = self.capture()
        operation.validate_report(self.state, self.base, self.mesh, SimpleNamespace(NASTRAN=1), 1, "mesh_map_block_inspect", result)
        self.xyz[0] = .1
        with self.assertRaisesRegex(ValueError, "stale"):
            operation.validate_report(self.state, self.base, self.mesh, SimpleNamespace(NASTRAN=1), 1, "mesh_map_block_inspect", result)

    def test_wrong_volume_type_and_boundary_disagreement_are_rejected(self):
        self.fields["Type"] = "Map"
        with self.assertRaisesRegex(ValueError, "Map Block"):
            self.capture()
        self.fields["Type"] = "Map Block"
        self.mesh.GetVolumeBoundary.return_value = self.faces[:-1]
        with self.assertRaisesRegex(ValueError, "membership"):
            self.capture()


if __name__ == "__main__": unittest.main()
