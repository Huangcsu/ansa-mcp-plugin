from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from pydantic import ValidationError
from ansa_mcp_bridge.operations import map_block_fill as fill
from ansa_mcp_plugin.__main__ import create_server
from ansa_mcp_shared.operations import writes_model


class MapBlockFillTests(unittest.TestCase):
    def test_strict_actions_schema_dispatch_and_classification(self):
        calls = []
        def submit(tool, params, operation_id, expected_target, expected_model, ttl_seconds):
            calls.append((tool, params))
            return {'task': {'target': expected_target, 'model': expected_model}, 'state': 'queued',
                    'result': None, 'dispatch': None, 'cancel_pending': False, 'acked': False,
                    'operation_id': operation_id, 'task_id': 'task-'+operation_id}
        server = create_server(SimpleNamespace(submit_task=submit)); tool = server._tool_manager.get_tool('ansa_mesh_map_block_fill')
        model = tool.fn_metadata.arg_model
        common = {'operation_id': 'fill', 'expected_target': {'backend_id': 'b', 'instance_id': 'i', 'session_id': 's', 'generation': 1},
                  'expected_model': {'epoch': 1}, 'deck': 1, 'volume_id': 1}
        def invoke(delta):
            row = model.model_validate({**common, **delta}); fields = row.model_dump()
            fields.update(expected_target=row.expected_target, expected_model=row.expected_model)
            return tool.fn(**fields)
        invoke({'action': 'plan'}); self.assertEqual(calls[-1], ('mesh_map_block_fill_plan', {'deck': 1, 'volume_id': 1}))
        invoke({'action': 'apply', 'expected_plan_sha256': 'a'*64, 'backup_path': str(Path.cwd()/'new-fill.ansa')})
        self.assertEqual(calls[-1][0], 'mesh_map_block_fill_apply')
        for delta in ({'action': 'apply'}, {'action': 'plan', 'expected_plan_sha256': 'a'*64}):
            with self.subTest(delta=delta), self.assertRaises(ValueError): invoke(delta)
        for delta in ({'volume_id': True}, {'volume_id': 0}, {'unexpected': True}):
            with self.subTest(delta=delta), self.assertRaises(ValidationError): model.model_validate({**common, 'action': 'plan', **delta})
        self.assertFalse(writes_model('mesh_map_block_fill_plan')); self.assertTrue(writes_model('mesh_map_block_fill_apply'))

    def test_native_cap_order_controls_source_and_direction(self):
        block = {'cad_witness': {}, 'volumes': [{'caps_match_complete_boundary': True,
                 'caps': {'master': [2], 'slave': [1], 'round': [3, 4, 5, 6]}}]}
        choice = {'source_face_id': 1, 'target_face_id': 2, 'direction': [0., 0., 1.], 'distance': 10.}
        with patch.object(fill, 'discover', return_value={'components': [{'alternatives': [{'map_options': choice}]}]}), \
             patch.object(fill, 'plan_translation', return_value={'side_face_ids': [3,4,5,6]}) as plan:
            _, source, target = fill._topology(block)
        self.assertEqual((source, target), (2, 1)); self.assertEqual(plan.call_args.args[3], [0., 0., -1.])
        block['volumes'][0]['caps']['round'] = [3,4,5]
        with self.assertRaises(ValueError): fill._topology(block)

    def test_existing_solid_is_rejected_before_any_boundary_export_or_write(self):
        base = Mock(); mesh = Mock(); state = SimpleNamespace(assert_main=Mock())
        with patch.object(fill.inspection, '_deck', return_value=1), patch.object(fill.native, '_check_nastran'), \
             patch.object(fill.guards, '_execution_context', return_value={}), patch.object(fill.seeds, '_collect', return_value=[object()]), \
             patch.object(fill.map_block_inspection, 'capture') as capture, self.assertRaises(ValueError):
            fill.preflight(state, base, mesh, SimpleNamespace(NASTRAN=1), {'deck': 1, 'volume_id': 1})
        capture.assert_not_called(); mesh.VolumesRemesh.assert_not_called()

    def test_stale_or_different_plan_parameters_prevent_backup_and_native_call(self):
        prepared = {'clean': {'deck': 1, 'volume_id': 1}, 'before': {'entities': []}, 'plan_sha256': 'a'*64}
        for delta in ({'expected_plan_sha256': 'b'*64}, {'volume_id': 2}):
            with patch.object(fill.native, '_backup_preflight') as backup, patch.object(fill.cad_map, '_snapshot', return_value={'entities': []}):
                mesh = Mock()
                result = fill.apply_prepared(Mock(), Mock(), mesh, Mock(),
                    {'deck': 1, 'volume_id': 1, 'expected_plan_sha256': 'a'*64, 'backup_path': 'new.ansa', **delta}, prepared)
            backup.assert_not_called(); mesh.VolumesRemesh.assert_not_called(); self.assertFalse(result['possible_effects'])

    def test_failed_observation_does_not_attribute_entities_from_a_different_model(self):
        prepared = {'clean': {'deck': 1, 'volume_id': 1}, 'context': {'epoch': 1},
                    'before': {'entities': []}, 'plan_sha256': 'a'*64}
        with patch.object(fill.guards, '_same_context', side_effect=RuntimeError('model lifetime changed')), \
             patch.object(fill.cad_map, '_snapshot') as snapshot:
            result = fill.apply_prepared(Mock(), Mock(), Mock(), Mock(),
                {'deck': 1, 'volume_id': 1, 'expected_plan_sha256': 'b'*64, 'backup_path': 'new.ansa'}, prepared)
        snapshot.assert_not_called()
        self.assertEqual(result['new_solid_ids'], []); self.assertEqual(result['new_grid_ids'], [])
        self.assertIn('lifetime changed', result['observation_error'])

    def test_native_failure_or_readback_failure_retains_effects_without_retry(self):
        clean = {'deck': 1, 'volume_id': 1}
        for native_result, fail_readback in ((0, False), (1, True), (1, False)):
            prepared = {'clean': clean, 'before': {'entities': []}, 'plan_sha256': 'a'*64,
                        'target_length': 5., 'context': {}, 'settings': {'snapshot_sha256': 'c'*64}}
            after = {'entities': [{'entity': {'type': 'SOLID', 'id': 89}}, {'entity': {'type': 'GRID', 'id': 92}}]}
            mesh = Mock(); mesh.VolumesRemesh.return_value = native_result
            def run(state, base, mesh, length, callback, result, **kwargs):
                try: return callback()
                finally: result['restored'] = True
            with patch.object(fill.native, '_backup_preflight', return_value=()), patch.object(fill.native, '_verify_backup', return_value={'verified': True}), \
                 patch.object(fill, 'preflight', return_value=prepared), patch.object(fill.guards, '_same_context'), \
                 patch.object(fill.features, '_entity', return_value=object()), patch.object(fill.meshing_context, 'run_with_context', side_effect=run), \
                 patch.object(fill, 'readback', side_effect=RuntimeError('boundary mismatch') if fail_readback else None,
                              return_value={'new_solid_ids': [89], 'new_grid_ids': [92]}), patch.object(fill.cad_map, '_snapshot', return_value=after):
                result = fill.apply_prepared(Mock(), Mock(), mesh, Mock(), {**clean, 'expected_plan_sha256': 'a'*64, 'backup_path': 'new.ansa'}, prepared)
            self.assertEqual(mesh.VolumesRemesh.call_count, 1); self.assertTrue(result['restored'])
            self.assertEqual(result['new_solid_ids'], [89]); self.assertEqual(result['new_grid_ids'], [92])
            self.assertEqual(result['ok'], native_result == 1 and not fail_readback)
            self.assertEqual(result['recovery_required'], not result['ok'])


if __name__ == '__main__': unittest.main()
