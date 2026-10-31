import copy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ansa_mcp_bridge.operations import map_block_prepare as mb
from ansa_mcp_shared.operations import writes_model


def params():
    return {'deck': 1, 'face_ids': [1, 2, 3, 4, 5, 6], 'source_face_id': 1,
            'target_face_id': 2, 'direction': [0., 0., 1.], 'distance': 10.,
            'target_length': 5., 'property_id': 2}


def row(kind, ident, **fields):
    return {'entity': {'type': kind, 'id': ident}, 'fields': fields}


class MapBlockPrepareTests(unittest.TestCase):
    def test_classification_and_strict_caps_direction_and_apply_fields(self):
        self.assertFalse(writes_model('mesh_map_block_plan')); self.assertTrue(writes_model('mesh_map_block_apply'))
        for delta in ({'source_face_id': 2}, {'face_ids': [1, 1, 2, 3, 4, 5]},
                      {'target_length': True}, {'direction': [0., 0., 0.]}, {'backup_path': 'x.ansa'}):
            with self.subTest(delta=delta), self.assertRaises((TypeError, ValueError)):
                mb.normalize({**params(), **delta})
        clean = mb.normalize({**params(), 'direction': [1., 3., 7.]})
        self.assertEqual(clean, mb.normalize(clean))

    def test_stale_plan_does_not_backup_set_parameters_or_call_native(self):
        prepared = {'clean': params(), 'before': {'entities': []}, 'plan_sha256': 'a'*64}
        with patch.object(mb.native, '_backup_preflight') as backup, patch.object(mb.cad_map, '_snapshot', return_value={'entities': []}):
            mesh = Mock()
            result = mb.apply_prepared(Mock(), Mock(), mesh, Mock(),
                        {**params(), 'expected_plan_sha256': 'b'*64, 'backup_path': 'new.ansa'}, prepared)
        backup.assert_not_called(); mesh.MapBlock.assert_not_called()
        self.assertFalse(result['possible_effects'])
        with patch.object(mb.native, '_backup_preflight') as backup, patch.object(mb.cad_map, '_snapshot', return_value={'entities': []}):
            result = mb.apply_prepared(Mock(), Mock(), mesh, Mock(),
                        {**params(), 'target_length': 4., 'expected_plan_sha256': 'a'*64, 'backup_path': 'new.ansa'}, prepared)
        backup.assert_not_called(); mesh.MapBlock.assert_not_called()
        self.assertFalse(result['possible_effects'])

    def test_only_new_native_assets_are_removed_after_effective_property_binding(self):
        before = {'entities': [row('MAT1', 1, E=123456.), row('PSOLID', 2, MID=1)]}
        after = {'entities': before['entities']+[row('PSOLID', 3, MID=3), row('MAT1', 3), row('VOLUME', 7, PID=3)]}
        volume = SimpleNamespace(_id=7, ansa_type=lambda deck: 'VOLUME')
        base = Mock(); base.SetEntityCardValues.return_value = 0
        base.GetEntityCardValues.return_value = {'PID': 2}; base.DeleteEntity.return_value = 0; base.GetEntity.return_value = None
        prepared = {'state': Mock(), 'before': before, 'clean': params()}; result = {}
        with patch.object(mb.cad_map, '_snapshot', return_value=after), patch.object(mb.features, '_entity', side_effect=lambda b, d, k, i: (k, i)):
            self.assertEqual(mb._bind_assets(base, 1, prepared, [volume], result), 7)
        self.assertEqual([c.args[0] for c in base.DeleteEntity.call_args_list], [('PSOLID', 3), ('MAT1', 3)])
        for call in base.DeleteEntity.call_args_list: self.assertEqual(call.kwargs, {'force': False, 'compress': False})
        base.GetEntityCardValues.return_value = {'PID': 3}; base.DeleteEntity.reset_mock()
        with patch.object(mb.cad_map, '_snapshot', return_value=after), patch.object(mb.features, '_entity', return_value=volume), self.assertRaises(RuntimeError):
            mb._bind_assets(base, 1, prepared, [volume], {})
        base.DeleteEntity.assert_not_called()

    def test_unexpected_or_missing_original_entities_prevent_asset_cleanup(self):
        before = {'entities': [row('MAT1', 1)]}; base = Mock()
        for after in ({'entities': []}, {'entities': before['entities']+[row('SOLID', 99)]}):
            with patch.object(mb.cad_map, '_snapshot', return_value=after), self.assertRaises(RuntimeError):
                mb._bind_assets(base, 1, {'state': Mock(), 'before': before, 'clean': params()}, [], {})
        base.SetEntityCardValues.assert_not_called(); base.DeleteEntity.assert_not_called()

    def test_part_material_names_are_unique_by_material_id_across_properties(self):
        source = {'MID1': 1, 'MID2': 1, 'MID3': 1, 'MID4': None, 'T': 1., 'Name': 'skin'}
        target = {'PID': 2, 'MID': 1, 'Name': 'solid'}
        prepared = {'source_pid': 1, 'property_evidence': {'psolid': {'fields': target}}, 'part': Mock()}
        fields = {'PID': '1, 2', 'PIDs Number': 2, 'Thickness': '1, -', 'Material Name': 'alloy', 'Last Edit': 'stamp',
                  'Properties Info': '[{"Id":"1","Name":"skin","Type":"PSHELL"},{"Id":"2","Name":"solid","Type":"PSOLID"}]',
                  'Materials Info': '[{"1":[{"Id":"1","Name":"alloy","Type":"MAT1"}]},{"2":[{"Id":"1","Name":"alloy","Type":"MAT1"}]}]'}
        def card(base, deck, entity):
            return {'fields': source if entity[0] == 'PSHELL' else {'Name': 'alloy'}}
        with patch.object(mb.features, '_entity', side_effect=lambda b, d, k, i: (k, i)), \
             patch.object(mb.seeds, '_card', side_effect=card), patch.object(mb.part_aggregates, 'capture', return_value={}):
            allowed, _ = mb._part_summaries(Mock(), 1, prepared, {'fields': fields})
            self.assertIn('Material Name', allowed)
            fields['Material Name'] = 'alloy, alloy'
            with self.assertRaises(RuntimeError): mb._part_summaries(Mock(), 1, prepared, {'fields': fields})

    def test_face_center_roundoff_requires_both_native_recomputation_and_tight_bound(self):
        before = {'geometry': {'center': [0., 0., -5.], 'area': 600.}}
        after = copy.deepcopy(before); after['geometry']['center'][0] = 2e-16
        values = [1., 2e-16, 0., -5.]+[1.]*18
        result = mb._check_face_center(copy.deepcopy(before), copy.deepcopy(after), values, 37.)
        self.assertEqual(result['after'][0], 2e-16)
        for delta in (1e-5, float('nan')):
            changed = copy.deepcopy(after); changed['geometry']['center'][0] = delta
            with self.subTest(delta=delta), self.assertRaises((ValueError, RuntimeError)):
                mb._check_face_center(copy.deepcopy(before), changed, values, 37.)
        wrong = values.copy(); wrong[1] = 1e-5
        with self.assertRaises(RuntimeError): mb._check_face_center(copy.deepcopy(before), copy.deepcopy(after), wrong, 37.)


if __name__ == '__main__': unittest.main()
