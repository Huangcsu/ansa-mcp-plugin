"""GUI 续验失败应保留证据，并允许在原任务/模型上再次只读续验。"""

import importlib
import json
from pathlib import Path
import sys
import tempfile
import unittest


class RigidDependenciesGuiRecovery(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        integration = str(Path(__file__).resolve().parents[1] / 'integration')
        sys.path.insert(0, integration)
        try:
            cls.harness = importlib.import_module('stage_three_rigid_dependencies_ansa')
        finally:
            sys.path.remove(integration)

    def write_source(self, run):
        source = {'success': True, 'core_acceptance': True, 'visual_acceptance': False,
                  'cases': [{'name': name, 'ok': True} for name in self.harness.CORE_CASE_NAMES]}
        path = run / 'rigid-dependencies-report.json'
        path.write_text(json.dumps(source), encoding='utf-8')
        return path

    def test_failed_attempts_can_resume_without_overwriting_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary)
            source = self.write_source(run)
            original_bytes = source.read_bytes()
            failed = run / 'rigid-dependencies-followup-report.json'
            failed.write_text(json.dumps({'success': False, 'visual_acceptance': False,
                                          'finished_at': '2026-10-15T01:00:00+00:00',
                                          'error': {'type': 'TimeoutError', 'message': 'GUI not confirmed'}}), encoding='utf-8')
            failed_bytes = failed.read_bytes()
            _original, fingerprint = self.harness.gui_followup_source(run)
            first_prefix, first_name = self.harness.new_gui_attempt(run)
            first = run / (first_name + '.json')
            first.write_bytes(failed_bytes)
            self.harness.gui_followup_source(run)
            second_prefix, second_name = self.harness.new_gui_attempt(run)
            self.assertNotEqual(first_prefix, second_prefix)
            self.assertNotEqual(first_name, second_name)
            self.assertFalse((run / (second_name + '.json')).exists())
            self.assertEqual(failed.read_bytes(), failed_bytes)
            self.assertEqual(first.read_bytes(), failed_bytes)
            self.assertEqual(source.read_bytes(), original_bytes)
            self.assertEqual(self.harness.fingerprint(source), fingerprint)

    def test_completed_uncertain_or_partial_evidence_does_not_authorize_replay(self):
        for previous in ({'success': True, 'visual_acceptance': True},
                         {'success': False, 'visual_acceptance': True}, {},
                         {'success': False, 'visual_acceptance': False},
                         {'success': False, 'visual_acceptance': False,
                          'finished_at': '2026-10-15T01:00:00+00:00'},
                         {'success': False, 'visual_acceptance': False,
                          'finished_at': 'invalid',
                          'error': {'type': 'TimeoutError', 'message': 'not confirmed'}}):
            with self.subTest(previous=previous), tempfile.TemporaryDirectory() as temporary:
                run = Path(temporary)
                self.write_source(run)
                (run / 'rigid-dependencies-followup-example-report.json').write_text(
                    json.dumps(previous), encoding='utf-8')
                with self.assertRaisesRegex(RuntimeError, 'already accepted or prior followup is uncertain'):
                    self.harness.gui_followup_source(run)
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary)
            source = self.write_source(run)
            value = json.loads(source.read_text(encoding='utf-8'))
            value['cases'].pop()
            source.write_text(json.dumps(value), encoding='utf-8')
            with self.assertRaisesRegex(RuntimeError, 'six passed core cases'):
                self.harness.gui_followup_source(run)


if __name__ == '__main__':
    unittest.main()
