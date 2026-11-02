"""Completion follows engineering persistence, including native-derived roundoff."""

import copy
import unittest

from tests.integration.annular_persistence import complete_persistence
from tests.unit.test_part_aggregates import aggregate, snapshot


def capture(value):
    return {'snapshot': value, 'aggregates': {str(key): row for key, row in aggregate(value).items()}}


class AnnularSweepPersistenceTests(unittest.TestCase):
    def test_native_derived_roundoff_can_complete_with_raw_difference_recorded(self):
        before = snapshot()
        after = copy.deepcopy(before)
        after['entities'][0]['fields']['Ixy'] = 1e-15
        report = {'complete': False}

        complete_persistence(report, capture(before), capture(after))

        self.assertTrue(report['complete'])
        self.assertFalse(report['save_reopen_raw_exact'])
        self.assertEqual(report['persistence']['status'], 'pass')
        self.assertEqual(len(report['persistence']['derived_roundoff']), 1)

    def test_changed_engineering_data_invalidates_completion_and_preserves_failure(self):
        before = snapshot()
        after = copy.deepcopy(before)
        after['grids'][0]['xyz'][0] = 1e-16
        report = {'complete': True}

        with self.assertRaisesRegex(RuntimeError, 'protected snapshot'):
            complete_persistence(report, capture(before), capture(after))

        self.assertFalse(report['complete'])
        self.assertFalse(report['save_reopen_raw_exact'])
        self.assertEqual(report['persistence']['status'], 'failed')


if __name__ == '__main__':
    unittest.main()
