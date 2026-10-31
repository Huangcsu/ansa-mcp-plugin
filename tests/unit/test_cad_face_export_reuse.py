from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from ansa_mcp_bridge.operations import cad_sweep_geometry as geometry,geometry_features as features
from test_cad_sweep_topology import _cube_witness


class CadFaceExportReuseTests(unittest.TestCase):
    def test_shared_proofs_are_bounded_to_one_call_and_return_independent_rows(self):
        witness=_cube_witness();state=SimpleNamespace(assert_main=Mock());base=Mock()
        def export(state,base,entities,stats):stats['scoped_export_calls']+=1;return b'export'
        proof={'status':'verified_at_export_precision','continuous_export_domain_verified':True,'cad_export_sha256':'a'*64}
        with patch.object(features,'_context',return_value={'epoch':1}),patch.object(features,'_entity',side_effect=lambda b,d,k,i:i), \
             patch.object(features,'_export',side_effect=export) as native,patch.object(geometry,'verify_planar_quad',return_value=proof):
            for _ in range(2):
                rows=geometry.capture_many(state,base,1,[(witness,{}),(witness,{})])
                self.assertEqual([r['scoped_export_calls'] for r in rows],[6,0])
                rows[0]['faces'][0]['status']='changed'
                self.assertEqual(rows[1]['faces'][0]['status'],'verified_at_export_precision')
            self.assertEqual(native.call_count,12)

    def test_context_change_rejects_the_whole_read(self):
        witness=_cube_witness();state=SimpleNamespace(assert_main=Mock())
        with patch.object(features,'_context',side_effect=[{'epoch':1},{'epoch':2}]),patch.object(geometry,'_capture',return_value={}),self.assertRaisesRegex(RuntimeError,'context changed'):
            geometry.capture_many(state,Mock(),1,[(witness,{})])


if __name__=='__main__':unittest.main()
