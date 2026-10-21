"""Analysis policy checks independent of ANSA and the task transport."""
import copy
import math
import unittest

from ansa_mcp_shared.mesh_quality import evaluate, normalize_profile


class QualityProfiles(unittest.TestCase):
    def test_linear_and_crash_have_distinct_size_and_estimate_contracts(self):
        linear = normalize_profile('linear', [{'name': 'ASPECT', 'max': 5}])
        crash = normalize_profile('explicit_crash', [{'name': 'ASPECT', 'max': 5}],
                                  minimum_length=.5, wave_speed=2., target_time_step=.1,
                                  element_family='solid')
        self.assertNotEqual(linear['profile_sha256'], crash['profile_sha256'])
        self.assertEqual(crash['estimated_minimum_length'], .2)
        self.assertEqual(crash['estimated_time_step'], .25)
        self.assertTrue(crash['estimate_only'])
        values = [{'id': 1, 'metrics': {'ASPECT': 3., 'MINL': .4}}]
        self.assertTrue(evaluate(linear, values)['passed'])
        self.assertEqual(evaluate(crash, values)['failed_entity_ids'], [1])

    def test_exact_bounds_pass_but_unknown_cannot_pass(self):
        profile = normalize_profile('linear', [{'name': 'JACOB', 'min': .7}])
        result = evaluate(profile, [{'id': 1, 'metrics': {'JACOB': .7}},
                                    {'id': 2, 'metrics': {'JACOB': None}}])
        self.assertTrue(result['rows'][0]['passed'])
        self.assertFalse(result['complete'])
        self.assertFalse(result['passed'])
        self.assertEqual(result['unknown_entity_ids'], [2])

    def test_hash_pins_family_threshold_and_normalized_numeric_values(self):
        first = normalize_profile('linear', [{'name': 'ASPECT', 'max': 5}])
        self.assertEqual(first, normalize_profile('linear', [{'max': 5., 'name': 'ASPECT'}]))
        tampered = copy.deepcopy(first)
        tampered['criteria'][0]['max'] = 10.
        with self.assertRaises(ValueError):
            evaluate(tampered, [{'id': 1, 'metrics': {'ASPECT': 2}}])

    def test_invalid_thresholds_and_nonfinite_values_are_rejected(self):
        invalid = ([{'name': 'ASPECT'}], [{'name': 'ASPECT', 'max': True}],
                   [{'name': 'ASPECT', 'min': 6, 'max': 5}],
                   [{'name': 'ASPECT', 'max': math.nan}],
                   [{'name': 'ASPECT', 'max': math.inf}],
                   [{'name': 'ASPECT', 'max': 5, 'auto_fix': True}],
                   [{'name': 'ASPECT', 'max': 5}, {'name': 'ASPECT', 'max': 4}])
        for criteria in invalid:
            with self.subTest(criteria=criteria), self.assertRaises(ValueError):
                normalize_profile('linear', criteria)
        with self.assertRaises(ValueError):
            normalize_profile('linear', [{'name': 'ASPECT', 'max': 5}], minimum_length=1)
        with self.assertRaises(ValueError):
            normalize_profile('explicit_crash', [{'name': 'ASPECT', 'max': 5}])

    def test_empty_or_duplicate_scope_and_invalid_measurement_cannot_pass(self):
        profile = normalize_profile('linear', [{'name': 'ASPECT', 'max': 5}])
        for rows in ([], [{'id': True, 'metrics': {}}],
                     [{'id': 1, 'metrics': {'ASPECT': math.inf}}],
                     [{'id': 1, 'metrics': {}}, {'id': 1, 'metrics': {}}]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                evaluate(profile, rows)


if __name__ == '__main__':
    unittest.main()
