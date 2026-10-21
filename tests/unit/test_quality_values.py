"""原生质量数值明细与严格进度比较。"""

import json
import unittest

from ansa_mcp_bridge.operations.quality_values import measure, progress


class Entity:
    def __init__(self, entity_id):
        self._id = entity_id


class FakeBase:
    def __init__(self, values):
        self.values = values
        self.metric_calls = []
        self.f11_calls = []

    def F11ShellsOptionsGet(self, criterion):
        self.f11_calls.append(("shell", criterion))
        return {"calculation": "NASTRAN"}

    def F11SolidsOptionsGet(self, criterion):
        self.f11_calls.append(("solid", criterion))
        return {"calculation": "NASTRAN"}

    def ElementQuality(self, entity, metrics):
        self.metric_calls.append((entity._id, tuple(metrics)))
        values = self.values[entity._id]
        return [values.get(metric, "error") for metric in metrics]


def raw_levels(threshold):
    # Each .ansa_qual level stores the threshold and an opaque second value.
    return [[threshold, -1] for _ in range(4)]


def quality_detail(name, metric, value, *, calculation="NASTRAN", threshold=10.0,
                   direction="max", available=True):
    limits = None
    if direction in {"min", "max"}:
        limits = {
            "min": threshold if direction == "min" else None,
            "max": threshold if direction == "max" else None,
        }
    return {
        "criterion": name,
        "native_metric": metric,
        "calculation": calculation,
        "levels": raw_levels(threshold),
        "failed_range_index": 0,
        "direction": direction,
        "threshold": threshold,
        "limits": limits,
        "value": value if available else None,
        "available": available,
    }


def report(value, *, calculation="NASTRAN", threshold=10.0, available=True,
           violation=True, include_unmapped=False, entity_type="ELEMENT_SHELL",
           criterion_name="aspect ratio", metric="ASPECT", direction="max"):
    active = [{
        "name": criterion_name,
        "calculation": calculation,
        "levels": raw_levels(threshold),
    }]
    details = [quality_detail(
        criterion_name, metric, value, calculation=calculation,
        threshold=threshold, direction=direction, available=available,
    )]
    metrics = {metric: value if available else None}
    if include_unmapped:
        active.append({
            "name": "incomplete element",
            "calculation": "",
            "levels": raw_levels(None),
        })
        details.append(quality_detail(
            "incomplete element", None, None, calculation="", threshold=None,
            direction=None, available=False,
        ))
    violations = [criterion_name] if violation else []
    failed_ids = [7] if violation else []
    snapshot = {
        "sha256": "a" * 64,
        "source": "custom_text",
        "label": "test",
        "failed_range_index": 0,
        "active_criteria": active,
        "missing_criteria": [],
        "calculation_source": "current_ANSA_session",
    }
    return {
        "complete": True,
        "deck": 1,
        "analysis_type": "linear",
        "entity_type": entity_type,
        "profile": {
            "template": "linear_shell",
            "analysis_type": "linear",
            "element_family": "shell",
            "profile_sha256": "b" * 64,
        },
        "criteria_snapshot": snapshot,
        "rows": [{
            "id": 7,
            "family": "shell",
            "complete": True,
            "passed": not violation,
            "violations": violations,
            "metrics": metrics,
            "quality_details": details,
        }],
        "failed_entity_ids": failed_ids,
        "unknown_entity_ids": [],
    }


class QualityValuesTests(unittest.TestCase):
    def test_measure_batches_metrics_and_preserves_real_levels_and_directions(self):
        names = (
            ("aspect ratio", "ASPECT", "max"),
            ("warping", "WARP", "max"),
            ("skewness", "SKEW", "max"),
            ("jacobian", "JACOB", "min"),
            ("min length", "MINL", "min"),
            ("max length", "MAXL", "max"),
            ("crash time step", "CRASH", "min"),
            ("collapse", "COLLAPSE", "min"),
        )
        levels = raw_levels(1.3)
        snapshot = {
            "failed_range_index": 0,
            "active_criteria": [
                {"name": name, "calculation": "NASTRAN", "levels": levels}
                for name, _, _ in names
            ],
        }
        values = {metric: float(index + 1) for index, (_, metric, _) in enumerate(names)}
        base = FakeBase({7: values})

        result = measure(base, [Entity(7)], "ELEMENT_SHELL", snapshot)[7]
        self.assertEqual(base.metric_calls, [(7, tuple(metric for _, metric, _ in names))])
        self.assertEqual(result["metrics"], values)
        for detail, (name, metric, direction) in zip(result["quality_details"], names):
            self.assertEqual(detail["criterion"], name)
            self.assertEqual(detail["native_metric"], metric)
            self.assertEqual(detail["levels"], levels)
            self.assertEqual(detail["levels"][0], [1.3, -1])
            self.assertEqual(detail["threshold"], 1.3)
            self.assertEqual(detail["direction"], direction)
        json.dumps(result, allow_nan=False)

    def test_unmapped_criterion_is_unavailable_without_zero_default(self):
        snapshot = {
            "failed_range_index": 0,
            "active_criteria": [
                {"name": "aspect ratio", "calculation": "NASTRAN", "levels": raw_levels(10.0)},
                {"name": "incomplete element", "calculation": "", "levels": raw_levels(None)},
            ],
        }
        base = FakeBase({7: {"ASPECT": 12.5}})

        row = measure(base, [Entity(7)], "shell", snapshot)[7]
        self.assertEqual(row["metrics"], {"ASPECT": 12.5})
        self.assertTrue(row["quality_details"][0]["available"])
        self.assertEqual(row["quality_details"][0]["levels"][0], [10.0, -1])
        self.assertEqual(row["quality_details"][0]["limits"], {"min": None, "max": 10.0})
        self.assertFalse(row["quality_details"][1]["available"])
        self.assertIsNone(row["quality_details"][1]["value"])
        self.assertEqual(base.metric_calls, [(7, ("ASPECT",))])
        json.dumps(row, allow_nan=False)

    def test_failure_count_can_stay_constant_while_failing_metric_improves(self):
        result = progress(report(12.0), report(11.0))
        self.assertTrue(result["comparable"])
        self.assertTrue(result["improved"])
        self.assertFalse(result["regressed"])
        self.assertEqual(result["reason"], "metrics_improved_without_regression")

    def test_minimum_metric_direction_uses_first_column_as_lower_threshold(self):
        result = progress(
            report(1.0, threshold=1.3, criterion_name="jacobian", metric="JACOB", direction="min"),
            report(1.1, threshold=1.3, criterion_name="jacobian", metric="JACOB", direction="min"),
        )
        self.assertTrue(result["comparable"])
        self.assertTrue(result["improved"])
        self.assertAlmostEqual(result["improvements"][0]["before_risk"], 0.3)
        self.assertAlmostEqual(result["improvements"][0]["after_risk"], 0.2)

    def test_regression_refuses_improvement(self):
        result = progress(report(11.0), report(12.0))
        self.assertTrue(result["comparable"])
        self.assertFalse(result["improved"])
        self.assertTrue(result["regressed"])

    def test_unavailable_failing_metric_is_not_evidence_of_progress(self):
        result = progress(report(12.0), report(None, available=False))
        self.assertFalse(result["comparable"])
        self.assertFalse(result["improved"])
        self.assertEqual(result["reason"], "incomplete_metric_evidence")

    def test_unknown_nonviolating_detail_does_not_block_failed_metric_progress(self):
        result = progress(
            report(12.0, include_unmapped=True),
            report(11.0, include_unmapped=True),
        )
        self.assertTrue(result["comparable"])
        self.assertTrue(result["improved"])

    def test_passing_band_changes_are_not_counted_as_regression_or_progress(self):
        result = progress(
            report(9.0, violation=False),
            report(9.9, violation=False),
        )
        self.assertTrue(result["comparable"])
        self.assertFalse(result["regressed"])
        self.assertFalse(result["improved"])

    def test_failed_count_decrease_is_not_metric_progress(self):
        result = progress(report(12.0), report(9.0, violation=False))
        self.assertFalse(result["comparable"])
        self.assertFalse(result["improved"])
        self.assertEqual(result["reason"], "failure_count_decreased")

    def test_algorithm_change_makes_reports_incomparable(self):
        result = progress(report(12.0), report(11.0, calculation="PATRAN"))
        self.assertFalse(result["comparable"])
        self.assertFalse(result["improved"])
        self.assertEqual(result["reason"], "algorithm_changed")

    def test_any_criteria_snapshot_change_makes_reports_incomparable(self):
        after = report(11.0)
        after["criteria_snapshot"]["label"] = "different"
        result = progress(report(12.0), after)
        self.assertFalse(result["comparable"])
        self.assertEqual(result["reason"], "criteria_configuration_changed")


if __name__ == "__main__":
    unittest.main()
