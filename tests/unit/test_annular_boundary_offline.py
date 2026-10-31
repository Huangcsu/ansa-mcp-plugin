"""Reject stale quality bindings when captured coordinates change."""

import copy
import importlib.util
import json
from pathlib import Path
import unittest

from ansa_mcp_bridge.operations import structured_native


_PATH = Path(__file__).resolve().parents[1] / "integration" / "annular_boundary_offline.py"
_SPEC = importlib.util.spec_from_file_location("annular_boundary_offline", _PATH)
_AUDIT = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_AUDIT)


class AnnularBoundaryBindingTests(unittest.TestCase):
    def snapshot(self):
        value = {"deck": 1, "complete": True, "nodes": [{"id": 1, "xyz": [0.0, 0.0, 16.0]}],
                 "elements": [], "integrity": {"complete": True, "requested_element_count": 0,
                 "element_count": 0, "node_count": 1, "missing_node_ids": [], "element_types": {}},
                 "ownership": {"origin": "not_applicable", "complete": False, "known_count": 0}}
        value["snapshot_sha256"] = structured_native._hash(value)
        return value

    def test_sorting_json_object_keys_does_not_change_the_native_capture_digest(self):
        snapshot = self.snapshot()
        reordered = json.loads(json.dumps(snapshot, sort_keys=True))
        self.assertEqual(_AUDIT._native_capture_digest(reordered), snapshot["snapshot_sha256"])

    def test_changed_coordinates_cannot_reuse_the_old_quality_digest(self):
        snapshot = self.snapshot()
        for changed in ("source", "output"):
            with self.subTest(changed=changed):
                capture = {"source": copy.deepcopy(snapshot), "output": copy.deepcopy(snapshot)}
                capture[changed]["nodes"][0]["xyz"][2] = 0.02
                public = {"complete": True, "before_save": {
                    "source_sha256": snapshot["snapshot_sha256"],
                    "mesh_sha256": snapshot["snapshot_sha256"]}}
                with self.assertRaisesRegex(ValueError, changed + " snapshot checksum"):
                    _AUDIT.audit(capture, {}, public)


if __name__ == "__main__":
    unittest.main()
