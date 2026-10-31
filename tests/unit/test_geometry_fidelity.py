import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from ansa_mcp_bridge.operations import geometry_fidelity as fidelity


class GeometryFidelityTests(unittest.TestCase):
    def run_capture(self, evaluate, project):
        base = SimpleNamespace(EvaluateCurvePoint=Mock(side_effect=evaluate))
        calc = SimpleNamespace(ProjectPointToCons=Mock(side_effect=project))
        result = fidelity.capture(base, calc, "cons", "curve")
        return result, base.EvaluateCurvePoint, calc.ProjectPointToCons

    def test_samples_fixed_parameters_with_nine_calls_per_native_api(self):
        evaluated = []
        projected = []

        def evaluate(curve, u):
            evaluated.append((curve, u))
            return [u, 2, 3]

        def project(point, cons):
            projected.append((list(point), cons))
            return ((point[0], point[1], point[2]),)

        result, evaluate_mock, project_mock = self.run_capture(evaluate, project)

        expected_u = [0, 0.125, 0.25, 0.375, 0.5, 0.625, 0.75, 0.875, 1]
        self.assertEqual([row["u"] for row in result["samples"]], expected_u)
        self.assertEqual([item[1] for item in evaluated], expected_u)
        self.assertTrue(all(curve == "curve" for curve, _ in evaluated))
        self.assertTrue(all(cons == "cons" for _, cons in projected))
        self.assertEqual(evaluate_mock.call_count, 9)
        self.assertEqual(project_mock.call_count, 9)
        self.assertEqual(result["status"], "sampled_only")
        self.assertEqual(result["valid_sample_count"], 9)
        self.assertEqual(result["requested_sample_count"], 9)
        self.assertEqual(result["evaluate_curve_calls"], 9)
        self.assertEqual(result["project_point_to_cons_calls"], 9)
        self.assertEqual(result["maximum_observed_distance"], 0.0)
        self.assertEqual(result["rms_observed_distance"], 0.0)
        json.dumps(result, allow_nan=False)
        self.assertFalse(result["continuous_equivalence_proven"])
        self.assertFalse(result["reverse_coverage_proven"])
        self.assertFalse(result["usable_for_mutation"])

    def test_nonzero_interior_deviation_is_reported_without_a_pass_result(self):
        offsets = {0.0: 3.0, 0.125: 4.0}

        def evaluate(curve, u):
            return (u, 0, 0)

        def project(point, cons):
            offset = offsets.get(point[0], 0.0)
            return [(point[0] + offset, 0, 0)]

        result, _, _ = self.run_capture(evaluate, project)

        distances = [row["distance"] for row in result["samples"]]
        self.assertEqual(distances[:3], [3.0, 4.0, 0.0])
        self.assertEqual(result["maximum_observed_distance"], 4.0)
        self.assertAlmostEqual(result["rms_observed_distance"], 5.0 / 3.0)
        self.assertEqual(result["status"], "sampled_only")
        self.assertNotIn("pass", result)

    def test_extension_projection_uses_nearest_endpoint_for_distance(self):
        def evaluate(curve, u):
            return [0, 0, 0]

        def project(point, cons):
            return ((100, 0, 0), [2, 0, 0])

        result, _, _ = self.run_capture(evaluate, project)
        sample = result["samples"][0]

        self.assertEqual(sample["projection_point"], [100, 0, 0])
        self.assertEqual(sample["closest_point"], [2, 0, 0])
        self.assertEqual(sample["projection_mode"], "endpoint_snap")
        self.assertEqual(sample["distance"], 2.0)
        self.assertNotEqual(sample["distance"], 100.0)

    def test_zero_sentinels_keep_other_observed_distances_and_do_not_retry(self):
        def evaluate(curve, u):
            return 0 if u == 0.25 else [u, 0, 0]

        def project(point, cons):
            if point[0] == 0.625:
                return 0
            return ((point[0] + 1, 0, 0),)

        result, evaluate_mock, project_mock = self.run_capture(evaluate, project)

        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["valid_sample_count"], 7)
        self.assertEqual(result["maximum_observed_distance"], 1.0)
        self.assertEqual(result["rms_observed_distance"], 1.0)
        self.assertEqual(evaluate_mock.call_count, 9)
        self.assertEqual(project_mock.call_count, 8)
        self.assertTrue(any("returned 0" in error for error in result["samples"][2]["errors"]))
        self.assertTrue(any("returned 0" in error for error in result["samples"][5]["errors"]))
        self.assertEqual(result["samples"][0]["distance"], 1.0)

    def test_exceptions_are_recorded_per_sample_without_losing_prior_results(self):
        def evaluate(curve, u):
            if u == 0.375:
                raise RuntimeError("evaluation failure")
            return [u, 0, 0]

        def project(point, cons):
            if point[0] == 0.75:
                raise LookupError("projection failure")
            return ((point[0] + 0.5, 0, 0),)

        result, evaluate_mock, project_mock = self.run_capture(evaluate, project)

        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["valid_sample_count"], 7)
        self.assertEqual(result["maximum_observed_distance"], 0.5)
        self.assertEqual(result["samples"][3]["distance"], None)
        self.assertTrue(any("RuntimeError: evaluation failure" in e for e in result["samples"][3]["errors"]))
        self.assertTrue(any("LookupError: projection failure" in e for e in result["samples"][6]["errors"]))
        self.assertEqual(evaluate_mock.call_count, 9)
        self.assertEqual(project_mock.call_count, 8)

    def test_bool_nonfinite_and_malformed_native_results_make_capture_incomplete(self):
        invalid_curve_points = {
            0.125: [0.125, 0, True],
            0.25: [0.25, 0, float("nan")],
            0.375: [0.375, 0, float("inf")],
            0.5: [0.5, 0],
            0.625: 0,
        }

        def evaluate(curve, u):
            return invalid_curve_points.get(u, [u, 0, 0])

        def project(point, cons):
            if point[0] == 0.75:
                return ((point[0], 0, False),)
            if point[0] == 0.875:
                return ((point[0], 0, 0), (point[0], 0, 0), (point[0], 0, 0))
            if point[0] == 1.0:
                return ((point[0], 0, 0), (point[0], 0, float("inf")))
            return ((point[0], 0, 0),)

        result, evaluate_mock, project_mock = self.run_capture(evaluate, project)

        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["valid_sample_count"], 1)
        self.assertEqual(result["samples"][0]["curve_point"], [0, 0, 0])
        self.assertEqual(result["samples"][0]["distance"], 0.0)
        self.assertTrue(all(result["samples"][i]["errors"] for i in range(1, 9)))
        self.assertEqual(evaluate_mock.call_count, 9)
        self.assertEqual(project_mock.call_count, 4)
        self.assertEqual(result["maximum_observed_distance"], 0.0)
        self.assertEqual(result["rms_observed_distance"], 0.0)
        json.dumps(result, allow_nan=False)

        def extreme_evaluate(curve, u):
            return [1.7e308, 0, 0]

        def extreme_project(point, cons):
            return ((-1.7e308, 0, 0),)

        extreme, _, _ = self.run_capture(extreme_evaluate, extreme_project)
        self.assertEqual(extreme["status"], "incomplete")
        self.assertEqual(extreme["valid_sample_count"], 0)
        self.assertIsNone(extreme["maximum_observed_distance"])
        self.assertIsNone(extreme["rms_observed_distance"])
        self.assertTrue(all(row["errors"] for row in extreme["samples"]))
        json.dumps(extreme, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
