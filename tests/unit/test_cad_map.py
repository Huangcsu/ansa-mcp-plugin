from types import SimpleNamespace
import unittest
import copy
import importlib.util
from pathlib import Path
from unittest.mock import Mock,patch

from ansa_mcp_bridge.operations import cad_map as cm


def params():
    return {'deck':1,'face_ids':[1,2,3,4,5,6],'source_face_id':1,'target_face_id':2,
            'direction':[0.,0.,1.],'distance':10.,'steps':2,'property_id':2,'output_part_id':7}


def load_persistence_helper():
    path=Path(__file__).parents[1]/'integration'/'cad_map_persistence.py'
    spec=importlib.util.spec_from_file_location('cad_map_persistence_test',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


class CadMapTests(unittest.TestCase):
    def test_persistence_exception_is_only_the_observed_output_part_display_flag(self):
        module=load_persistence_helper()
        before={'entities':[{'entity':{'type':'ANSAPART','id':2},'fields':{'Is Color Active':'NO','Name':'output'}}]}
        after=copy.deepcopy(before);after['entities'][0]['fields']['Is Color Active']='YES'
        result=module.audit_persistence(before,after,2)
        self.assertFalse(result['full_snapshot_exact']);self.assertTrue(result['engineering_snapshot_exact'])
        after['entities'][0]['fields']['Name']='unexpected'
        self.assertFalse(module.audit_persistence(before,after,2)['engineering_snapshot_exact'])

    def test_input_path_validation_requires_an_existing_absolute_ansa_unless_finalizing(self):
        module=load_persistence_helper()
        root=Path(__file__).resolve().parents[2];valid=root/'input.ansa';wrong=root/'input.step'
        with patch.object(module.Path,'is_file',autospec=True,side_effect=lambda path:path==valid):
            self.assertEqual(module.resolve_input(valid),valid)
            with self.assertRaisesRegex(ValueError,'required'):module.resolve_input(None)
            with self.assertRaisesRegex(ValueError,'absolute'):module.resolve_input(Path('relative.ansa'))
            with self.assertRaisesRegex(ValueError,r'\.ansa'):module.resolve_input(wrong)
            with self.assertRaisesRegex(ValueError,'existing'):module.resolve_input(root/'missing.ansa')
            self.assertIsNone(module.resolve_input(None,finalize=True))
            with self.assertRaisesRegex(ValueError,'does not accept'):module.resolve_input(valid,finalize=True)

    def test_direction_normalization_is_bitwise_idempotent_for_oblique_plans(self):
        first=cm.normalize({**params(),'direction':[1.,3.,7.]})
        second=cm.normalize(first)
        third=cm.normalize(second)
        self.assertEqual(first,second);self.assertEqual(second,third)
        self.assertEqual(cm.features._hash(first),cm.features._hash(third))

    def test_full_snapshot_preserves_unrelated_derived_card_fields(self):
        reference={'type':'PSHELL','id':90}
        guarded={'entities':[{'entity':reference,'card_fields':['Name'],'fields':{'Name':'outside'}}],
                 'grids':[],'cad_sha256':'a'*64}
        count=[0]
        def raw(*args,**kwargs):
            self.assertFalse(kwargs['omit_derived'])
            return {'entity':reference,'card_fields':['Name','Num. Elem'],'fields':{'Name':'outside','Num. Elem':count[0]}}
        with patch.object(cm.seeds,'_guard_snapshot',side_effect=lambda *a:__import__('copy').deepcopy(guarded)), \
             patch.object(cm.features,'_entity',return_value=object()),patch.object(cm.guards,'_read_full_card',side_effect=raw):
            before=cm._snapshot(object(),object(),1);count[0]=999;after=cm._snapshot(object(),object(),1)
        self.assertNotEqual(cm.features._hash(before),cm.features._hash(after))

    def test_total_model_budget_is_reserved_before_any_map_write(self):
        cm._reserve_budget({'entities':[None]*9998},1,0)
        with self.assertRaisesRegex(ValueError,'10000-entity'):
            cm._reserve_budget({'entities':[None]*9999},1,0)
        with self.assertRaisesRegex(ValueError,'10000-entity'):
            cm._reserve_budget({'entities':[None]*9990},1,9)

    def test_face_volume_count_is_required_from_the_full_native_card(self):
        row={'macro_face_ids':[1],'fields':{'Frozen':'NO'}}
        seed={'boundaries':[],'geometry_witness':{'face_boundaries':[{'face_id':1,'cons_ids':[1,2,3,4]}]}}
        for fields,reason in (({},'Num. Volumes'),({'Num. Volumes':' 1'},'already assigned')):
            with self.subTest(fields=fields),patch.object(cm.meshing,'_face_snapshot',return_value=(object(),row,[10])), \
                 patch.object(cm.seeds,'_card',return_value={'fields':fields}),patch.object(cm,'four_sides') as sides:
                with self.assertRaisesRegex(ValueError,reason):cm._read_faces(object(),object(),object(),params(),seed)
                sides.assert_not_called()

    def test_strict_plan_scope_and_apply_only_parameters(self):
        for changed in ({'deck':True},{'face_ids':[1,1,2,3,4,5]},{'source_face_id':8},
                        {'steps':True},{'steps':101},{'distance':float('nan')},{'direction':[0.,0.,0.]},
                        {'backup_path':'not-a-plan-field'}):
            with self.subTest(changed=changed),self.assertRaises((ValueError,TypeError)):
                cm.normalize({**params(),**changed})
        self.assertEqual(cm.normalize(params())['steps'],2)

    def test_backup_drift_prevents_native_call_and_reports_possible_effects(self):
        clean=params();prepared={'clean':clean,'plan_sha256':'a'*64,'before':{'entities':[]},'context':{}}
        base,mesh=Mock(),Mock()
        with patch.object(cm.native,'_backup_preflight',return_value=('b','c',{})), \
             patch.object(cm.native,'_verify_backup',return_value={'verified':True}), \
             patch.object(cm,'preflight',return_value={'plan_sha256':'b'*64}), \
             patch.object(cm,'_snapshot',return_value={'entities':[]}):
            result=cm.apply_prepared(object(),base,mesh,object(),{**clean,'expected_plan_sha256':'a'*64,'backup_path':'b.ansa'},prepared)
        self.assertFalse(result['ok']);self.assertFalse(result['api_called'])
        self.assertTrue(result['possible_effects']);self.assertTrue(result['recovery_required'])
        mesh.VolumesMap.assert_not_called()

    def test_native_exception_never_retries_and_retains_observed_partial_ids(self):
        clean=params();prepared={'clean':clean,'plan_sha256':'a'*64,'before':{'entities':[]},'context':{},
                                'topology':{'side_face_ids':[3,4,5,6]},'part':object(),'property':object()}
        mesh=Mock();mesh.VolumesMap.side_effect=RuntimeError('native interrupted')
        observed={'entities':[{'entity':{'type':'SOLID','id':99}}]}
        with patch.object(cm.native,'_backup_preflight',return_value=('b','c',{})), \
             patch.object(cm.native,'_verify_backup',return_value={'verified':True}), \
             patch.object(cm,'preflight',return_value=prepared),patch.object(cm.guards,'_same_context'), \
             patch.object(cm.features,'_entity',return_value=object()),patch.object(cm,'_snapshot',return_value=observed):
            result=cm.apply_prepared(object(),object(),mesh,object(),{**clean,'expected_plan_sha256':'a'*64,'backup_path':'b.ansa'},prepared)
        self.assertFalse(result['ok']);self.assertTrue(result['api_called'])
        self.assertEqual(result['new_solid_ids'],[99]);self.assertTrue(result['recovery_required'])
        self.assertEqual(mesh.VolumesMap.call_count,1)

    def test_stale_plan_rejects_without_backup_or_native_call(self):
        clean=params();prepared={'clean':clean,'plan_sha256':'a'*64,'before':{'entities':[]}}
        mesh=Mock()
        with patch.object(cm.native,'_backup_preflight') as backup,patch.object(cm,'_snapshot',return_value={'entities':[]}):
            result=cm.apply_prepared(object(),object(),mesh,object(),{**clean,'expected_plan_sha256':'b'*64,'backup_path':'b.ansa'},prepared)
        backup.assert_not_called();mesh.VolumesMap.assert_not_called()
        self.assertFalse(result['possible_effects'])


if __name__=='__main__':unittest.main()
