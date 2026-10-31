"""Freshness, read-only preflight and native-to-seed boundary regressions."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from ansa_mcp_bridge.operations import geometry_features as features, structured_evidence as evidence


class Entity:
    def __init__(self, kind, ident):
        self.kind, self._id = kind, ident

    def ansa_type(self, deck):
        return self.kind


class GeometryFeatureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.database = root / "saved.ansa"
        self.database.write_bytes(b"saved")
        self.state = SimpleNamespace(session_dir=root, model_epoch=1, instance_id="i", session_id="s",
            authorization={"backend_id": "b", "generation": 1}, assert_main=Mock(),
            identity=lambda: {"instance_id": "i", "session_id": "s", "pid": 2, "process_start": 3})
        self.entities = {(k, i): Entity(k, i) for k, i in (("FACE", 1), ("CONS", 2), ("CURVE", 3))}
        self.hots = [Entity("HOT POINT", 10), Entity("HOT POINT", 11)]
        self.hots[0].position, self.hots[1].position = [0., 0., 0.], [4., 0., 0.]
        self.boundary_edges = [self.entities["CONS", 2]]
        self.hots_by_cons = {2: self.hots}
        self.cons_by_hot = {10: self.boundary_edges, 11: self.boundary_edges}
        self.part = Entity("PART", 4)
        self.geometry = b"original"
        self.native_length = 4.
        def export(path, units, entities):
            self.assertEqual(units, "MILLIMETERS")
            self.assertTrue(entities)
            self.assertTrue(all(e.kind in ("FACE", "CURVE") for e in entities))
            Path(path).write_bytes(self.geometry)
            return 1
        self.base = SimpleNamespace(CurrentDeck=Mock(return_value=1), DataBaseName=Mock(return_value=str(self.database)),
            GetLengthUnits=Mock(return_value="millimeter"),
            GetEntity=Mock(side_effect=lambda d, k, i: self.entities.get((k, i))),
            GetEntityPart=Mock(return_value=self.part), CollectEntities=Mock(side_effect=lambda d, e, k:
                self.boundary_edges if k == "CONS" else self.hots_by_cons[e._id]),
            GetConsOfHotPoints=Mock(side_effect=lambda e: self.cons_by_hot[e._id]),
            GetFacesOfCons=Mock(return_value=[self.entities["FACE", 1]]),
            GetEntityCardValues=Mock(return_value={"Length": 6.2767, "Min Radius": 2.}),
            GetCurveLength=Mock(side_effect=lambda e: self.native_length), SaveFileAsIGES=Mock(side_effect=export),
            SurfaceInfo=Mock(return_value=[[1, 8, 1, 2., float("inf"), [0., 1., 0.], [0., 0., 1.], [1., 0., 0.]]]))
        digest = patch.object(features, "canonical_digest", side_effect=lambda b: hashlib.sha256(b).hexdigest())
        self.addCleanup(digest.stop); digest.start()
        analysis = patch.object(features, "analytic_curve", return_value={"status": "supported", "kind": "line",
            "length": 4., "start": [0., 0., 0.], "end": [4., 0., 0.]})
        self.analysis = analysis.start(); self.addCleanup(analysis.stop)
        self.options = {"target_length": 1., "max_chord_error": .02, "min_segments": 1, "multiple": 1, "max_segments": 10000}

    def capture(self, **kwargs):
        return features.capture(self.state, self.base, 1, [1], [3], **kwargs)

    def test_fresh_cache_hit_skips_only_derivation_and_keeps_valid_seed(self):
        first = self.capture(seed_options=self.options)
        second = self.capture(seed_options=self.options)
        self.assertEqual(first["source_snapshot_sha256"], second["source_snapshot_sha256"])
        self.assertEqual(first["summary"]["scoped_export_calls"], 2)
        self.assertEqual(second["summary"]["scoped_export_calls"], 1)
        self.assertEqual(second["summary"]["surface_info_calls"], 0)
        self.assertEqual(second["summary"]["cache_status"], "hit")
        self.assertEqual(self.base.GetEntityPart.call_count, 4)
        self.assertEqual(second["seed_plan"]["status"], "complete")
        self.assertEqual(second["seed_plan"]["seed_constraints"][0]["segments"], 4)
        self.assertFalse(second["usable_for_mutation"])
        radius = second["surfaces"][0]["radius_t"]
        self.assertEqual(radius, {"value": None, "infinite": True, "curvature": 0.})
        json.dumps(second, allow_nan=False)
        self.assertEqual(list((Path(self.temp.name) / "geometry-feature-exports").iterdir()), [])

    def test_uncomputed_cons_radius_is_unknown_without_inventing_analytic_geometry(self):
        self.base.GetEntityCardValues.return_value={"Length":6.2767,"Min Radius":0.}
        result=self.capture()
        row=result['boundaries'][0]
        self.assertIsNone(row['native_card_min_radius'])
        self.assertEqual(row['native_card_min_radius_status'],'unavailable')
        self.assertEqual(row['native_card_min_radius_raw'],'0.0')
        self.assertFalse(result['usable_for_mutation'])
        json.dumps(result,allow_nan=False)

    def test_same_epoch_same_length_geometry_edit_invalidates_cache_and_pages(self):
        first = self.capture()
        report = evidence.store_report(self.state, 1, "geometry_features", first)
        validator = lambda kind, result: features.validate_report(self.state, self.base, 1, kind, result)
        page = evidence.read_page(self.state, 1, report["report_id"], "curves", freshness_validator=validator)
        self.assertEqual(len(page["rows"]), 1)
        with self.assertRaisesRegex(ValueError, "native freshness"):
            evidence.read_page(self.state, 1, report["report_id"], "curves")
        self.geometry = b"different position, identical length"
        with self.assertRaisesRegex(ValueError, "stale.*geometry"):
            evidence.read_page(self.state, 1, report["report_id"], "curves", freshness_validator=validator)
        second = self.capture()
        self.assertNotEqual(first["source_snapshot_sha256"], second["source_snapshot_sha256"])
        self.assertEqual(second["summary"]["cache_status"], "miss")
        self.assertEqual(self.state.model_epoch, 1)

    def test_context_generation_and_native_adjacency_are_part_of_witness(self):
        first = self.capture()
        self.state.authorization["generation"] = 2
        with self.assertRaisesRegex(ValueError, "stale.*context"):
            features.validate_report(self.state, self.base, 1, "geometry_features", first)
        second = self.capture()
        self.assertEqual(second["summary"]["cache_status"], "miss")
        self.base.GetFacesOfCons.return_value = [self.entities["FACE", 1], Entity("FACE", 9)]
        with self.assertRaisesRegex(ValueError, "stale.*topology"):
            features.validate_report(self.state, self.base, 1, "geometry_features", second)

    def test_orphan_unsaved_wrong_unit_and_bad_scope_reject_before_export(self):
        self.base.GetEntityPart.return_value = None
        with self.assertRaisesRegex(ValueError, "assigned Part"):
            self.capture()
        self.base.GetEntityPart.return_value = self.part
        self.base.GetLengthUnits.return_value = "meter"
        with self.assertRaisesRegex(ValueError, "millimeter"):
            self.capture()
        self.base.GetLengthUnits.return_value = "millimeter"
        self.base.DataBaseName.return_value = ""
        with self.assertRaisesRegex(ValueError, "already saved"):
            self.capture()
        self.base.DataBaseName.return_value = str(self.database)
        for faces, curves in (([], []), ([1, 1], []), ([True], []), (list(range(1, 22)), [])):
            with self.assertRaises(ValueError):
                features.capture(self.state, self.base, 1, faces, curves)
        self.base.SaveFileAsIGES.assert_not_called()

    def test_unsupported_or_inconsistent_analytic_curve_has_no_seed_constraints(self):
        self.analysis.return_value = {"status": "unsupported", "reason": "B-spline cannot be confirmed analytic"}
        result = self.capture(seed_options=self.options)
        self.assertEqual(result["seed_plan"]["status"], "incomplete")
        self.assertNotIn("seed_constraints", result["seed_plan"])
        self.geometry = b"next"
        self.analysis.return_value = {"status": "supported", "kind": "line", "length": 5.}
        result = self.capture(seed_options=self.options)
        self.assertEqual(result["seed_plan"]["status"], "incomplete")
        self.assertIn("differs", result["curves"][0]["analysis"]["reason"])

    def test_failed_export_has_no_retry_or_cached_success(self):
        self.base.SaveFileAsIGES.side_effect = SystemError("translator failure")
        with self.assertRaisesRegex(RuntimeError, "no automatic save"):
            self.capture()
        self.assertEqual(self.base.SaveFileAsIGES.call_count, 1)
        self.assertFalse(hasattr(self.state, "_geometry_features_cache"))

    def test_export_budget_and_nonfinite_surface_fail_closed(self):
        with patch.object(features, "_EXPORT_LIMIT", 3):
            with self.assertRaisesRegex(ValueError, "export budget"):
                self.capture()
        self.base.SurfaceInfo.return_value[0][3] = float("nan")
        with self.assertRaisesRegex(ValueError, "radius"):
            self.capture()
        self.assertFalse(self.state._geometry_features_cache)

    def test_modified_evidence_kind_cannot_bypass_native_freshness_gate(self):
        report = evidence.store_report(self.state, 1, "geometry_features", self.capture())
        path = evidence._path(self.state, report["report_id"])
        envelope = json.loads(path.read_text())
        envelope["kind"] = "historical_audit"
        path.write_text(json.dumps(envelope))
        with self.assertRaisesRegex(ValueError, "kind mismatch"):
            evidence.read_page(self.state, 1, report["report_id"], "curves")

    def test_cache_is_bounded_and_returns_independent_values(self):
        for index in range(12):
            self.geometry = str(index).encode()
            result = self.capture()
        self.assertEqual(len(self.state._geometry_features_cache), 8)
        result["curves"].clear()
        self.assertEqual(len(self.capture()["curves"]), 1)

    def test_native_closed_loop_is_not_an_analytic_circle_or_seed_input(self):
        self.boundary_edges.append(Entity("CONS", 5))
        self.hots_by_cons[5] = list(reversed(self.hots))
        result = features.capture(self.state, self.base, 1, [1], [], self.options)
        self.assertEqual(result["contours"]["status"], "complete")
        face = result["contours"]["faces"][0]
        self.assertEqual(face["geometry_status"], "incomplete")
        self.assertEqual(len(face["loops"]), 1)
        self.assertEqual(face["loops"][0]["kind"], "closed_contour")
        self.assertEqual(face["loops"][0]["length_source"], "native_card_approximation")
        self.assertEqual(result["summary"]["vertex_count"], 2)
        self.assertEqual(result["summary"]["hot_point_adjacency_calls"], 2)
        self.assertEqual(self.base.GetConsOfHotPoints.call_count, 2)
        self.assertEqual(result["seed_plan"]["status"], "incomplete")
        self.assertNotIn("seed_constraints", result["seed_plan"])
        self.assertFalse(result["usable_for_mutation"])

    def test_precise_hot_point_change_invalidates_even_identical_export(self):
        first = self.capture()
        self.hots[0].position[0] = 0.00000000001
        with self.assertRaisesRegex(ValueError, "stale.*topology"):
            features.validate_report(self.state, self.base, 1, "geometry_features", first)
        second = self.capture()
        self.assertNotEqual(first["source_snapshot_sha256"], second["source_snapshot_sha256"])
        self.assertEqual(second["summary"]["cache_status"], "miss")

    def test_ambiguous_endpoint_count_is_incomplete_and_bad_reciprocity_rejects(self):
        self.hots_by_cons[2] = self.hots[:1]
        result = self.capture()
        self.assertEqual(result["boundaries"][0]["endpoint_topology"]["status"], "incomplete")
        self.assertEqual(result["contours"]["status"], "incomplete")
        self.assertEqual(result["contours"]["faces"][0]["loops"], [])
        self.cons_by_hot[10] = []
        self.base.SaveFileAsIGES.reset_mock()
        with self.assertRaisesRegex(ValueError, "inconsistent CONS/HOT POINT"):
            self.capture()
        self.base.SaveFileAsIGES.assert_not_called()

    def test_hot_point_position_is_not_coerced_from_missing_or_card_text(self):
        for invalid in (None, [0., 0.], [0., float("nan"), 0.], ["0", 0., 0.]):
            self.hots[0].position = invalid
            with self.assertRaisesRegex(ValueError, "HOT POINT"):
                self.capture()
        self.base.SaveFileAsIGES.assert_not_called()

    def test_sweep_request_rejects_bad_scope_before_any_native_export(self):
        options = {"source_face_id": 1, "target_face_id": 2,
                   "direction": [0., 0., 1.], "distance": 3.}
        for changed in ({}, {**options, "target_face_id": 1},
                        {**options, "tolerance": 0.1}, {**options, "distance": True},
                        {**options, "direction": [0., 0., 0.]}, {**options, "extra": 1}):
            with self.subTest(changed=changed), self.assertRaises((ValueError, TypeError)):
                features.capture(self.state, self.base, 1, [1, 2], [], sweep_options=changed)
        with self.assertRaisesRegex(ValueError, "FACE-only"):
            features.capture(self.state, self.base, 1, [1], [], sweep_options=options)
        with self.assertRaisesRegex(ValueError, "FACE-only"):
            features.capture(self.state, self.base, 1, [1, 2], [3], sweep_options=options)
        self.base.SaveFileAsIGES.assert_not_called()

    def test_sweep_correspondence_never_implies_continuous_geometry_or_write_readiness(self):
        from test_cad_sweep_topology import _cube_witness, SOURCE_FACE, TARGET_FACE
        witness = _cube_witness()
        options = {"source_face_id": SOURCE_FACE, "target_face_id": TARGET_FACE,
                   "direction": [0., 0., 1.], "distance": 3., "tolerance": 1e-7}
        result = features._sweep_plan(witness, options)
        self.assertEqual(result["topology_status"], "pass")
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["geometry_status"], "incomplete")
        self.assertFalse(result["cad_surface_geometry_verified"])
        self.assertFalse(result["usable_for_mutation"])
        self.assertEqual(result["native_execution_status"], "unverified")
        self.assertEqual(len(result["correspondence"]["side_pairs"]), 4)
        changed = features._sweep_plan(witness, {**options, "distance": 4.})
        self.assertEqual(changed["status"], "unsupported")
        self.assertNotIn("correspondence", changed)
        self.assertNotEqual(changed["plan_sha256"], result["plan_sha256"])

    def test_sweep_request_is_not_reused_from_geometry_derivation_cache(self):
        from test_cad_sweep_topology import _cube_witness, SOURCE_FACE, TARGET_FACE
        witness = _cube_witness()
        witness["feature_context"] = features._context(self.state, self.base, 1)
        options = {"source_face_id": SOURCE_FACE, "target_face_id": TARGET_FACE,
                   "direction": [0., 0., 1.], "distance": 3.}
        with patch.object(features, "_witness", return_value=(witness, {})), \
                patch.object(features, "_derive", return_value={"surfaces": [], "curves": [], "contours": {"status": "complete"}}) as derive:
            first = features.capture(self.state, self.base, 1, list(range(11, 17)), [], sweep_options=options)
            second = features.capture(self.state, self.base, 1, list(range(11, 17)), [], sweep_options={**options, "distance": 4.})
        self.assertEqual(derive.call_count, 1)
        self.assertEqual(second["summary"]["cache_status"], "hit")
        self.assertEqual(first["sweep_plan"]["topology_status"], "pass")
        self.assertEqual(second["sweep_plan"]["status"], "unsupported")
        descriptor = evidence.store_report(self.state, 1, "geometry_features", first)
        validator = lambda kind, result: features.validate_report(self.state, self.base, 1, kind, result)
        with patch.object(features, "_witness", return_value=(witness, {})):
            page = evidence.read_page(self.state, 1, descriptor["report_id"], "/sweep_plan/correspondence/side_pairs", freshness_validator=validator)
        self.assertEqual(len(page["rows"]), 4)
        with patch.object(features, "_witness", return_value=({**witness, "source_snapshot_sha256": "e"*64}, {})):
            with self.assertRaisesRegex(ValueError, "stale"):
                evidence.read_page(self.state, 1, descriptor["report_id"], "sweep_plan", freshness_validator=validator)


if __name__ == "__main__":
    unittest.main()
