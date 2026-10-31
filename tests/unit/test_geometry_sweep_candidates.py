"""Native freshness, proof reuse and read-only boundaries of sweep discovery."""
import copy
import unittest
from unittest.mock import patch

import test_geometry_features as feature_tests
from ansa_mcp_bridge.operations import geometry_sweep_candidates as candidates, structured_evidence


class GeometrySweepCandidateTests(unittest.TestCase):
    setUp = feature_tests.GeometryFeatureTests.setUp

    def capture(self, length=1.):
        return candidates.capture(self.state, self.base, 1, [1], length)

    def test_cache_reuses_proof_only_after_fresh_native_witness_and_keys_length(self):
        component = {"face_ids": [1], "status": "topology_candidate", "alternatives": [{"topology": {}}]}
        proof = {"status": "verified_at_export_precision", "scoped_export_calls": 6, "source_snapshot_sha256": "b"*64}
        with patch.object(candidates.cad_sweep_candidates, "discover", side_effect=lambda *a: {"components": [copy.deepcopy(component)], "coverage": {}}) as discover, \
             patch.object(candidates.cad_sweep_candidates, "subset_witness", return_value={"derived_topology_sha256": "b"*64}), \
             patch.object(candidates.cad_sweep_geometry, "capture", side_effect=lambda *a: copy.deepcopy(proof)) as geometry:
            first, second, third = self.capture(), self.capture(), self.capture(2.)
            self.assertEqual(discover.call_count, 2)
            self.assertEqual(geometry.call_count, 2)
            self.assertEqual(first["summary"]["scoped_export_calls"], 7)
            self.assertEqual(second["summary"]["scoped_export_calls"], 1)
            self.assertEqual(third["summary"]["verified_export_components"], 1)
            first["classification"]["components"].clear()
            self.assertEqual(len(self.capture()["classification"]["components"]), 1)
            self.assertFalse(second["usable_for_mutation"])
            self.assertEqual(second["classification"]["coverage"]["template_family"], "T1_complete_six_face_component")
            self.assertEqual(second["summary"]["legacy_component_statistics_template"], "T1_complete_six_face_component")

    def test_evidence_rechecks_native_geometry_and_context(self):
        report = structured_evidence.store_report(self.state, 1, "geometry_sweep_candidates", self.capture())
        validator = lambda kind, result: candidates.validate_report(self.state, self.base, 1, kind, result)
        with self.assertRaisesRegex(ValueError, "native freshness"):
            structured_evidence.read_page(self.state, 1, report["report_id"], "classification")
        self.assertTrue(structured_evidence.read_page(self.state, 1, report["report_id"], "classification", freshness_validator=validator)["ok"])
        self.geometry = b"changed same epoch"
        with self.assertRaisesRegex(ValueError, "stale"):
            structured_evidence.read_page(self.state, 1, report["report_id"], "classification", freshness_validator=validator)

    def test_empty_oversize_and_invalid_length_reject_before_native_export(self):
        for ids, length in (([], 1.), (list(range(1, 102)), 1.), ([1], True), ([1], float("inf"))):
            with self.subTest(ids=len(ids), length=length), self.assertRaises((ValueError, TypeError)):
                candidates.capture(self.state, self.base, 1, ids, length)
        self.base.SaveFileAsIGES.assert_not_called()

    def test_topology_match_does_not_bypass_export_proof(self):
        component = {"face_ids": [1], "status": "topology_candidate", "alternatives": [{"topology": {}}]}
        with patch.object(candidates.cad_sweep_candidates, "discover", return_value={"components": [component], "coverage": {}}), \
             patch.object(candidates.cad_sweep_candidates, "subset_witness", return_value={"derived_topology_sha256": "b"*64}), \
             patch.object(candidates.cad_sweep_geometry, "capture", return_value={"status": "unsupported", "scoped_export_calls": 6, "source_snapshot_sha256": "b"*64}):
            result = self.capture()
        self.assertEqual(result["summary"]["verified_export_components"], 0)
        self.assertEqual(result["summary"]["topology_candidate_components"], 1)
        self.assertFalse(result["classification"]["components"][0]["continuous_export_domain_verified"])
        self.assertEqual(result["classification"]["coverage"]["unsupported_face_ids"], [1])


if __name__ == "__main__":
    unittest.main()
