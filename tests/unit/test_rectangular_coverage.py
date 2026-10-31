import copy
import unittest

from ansa_mcp_bridge.operations import map_block_batch as batch, rectangular_coverage as coverage
from test_map_block_batch import assembly


def geometry(w):
    return {'status':'verified_at_export_precision','source_snapshot_sha256':w['source_snapshot_sha256'],
            'faces':[{'face_id':i,'status':'verified_at_export_precision','continuous_export_domain_verified':True,'cad_export_sha256':'a'*64} for i in w['source_scope']['face_ids']]}


class RectangularCoverageTests(unittest.TestCase):
    def check(self,boxes):
        _,source=assembly(((0.,0.,0.),(2.,1.,1.)))
        block,target=assembly(*boxes);groups=[v['face_ids'] for v in block['volumes']]
        proofs=[geometry(batch.local_witness(target,g)) for g in groups]
        return coverage.audit_partition(source,geometry(source),target,proofs,groups)

    def test_complete_partition_and_numerical_roundoff(self):
        result=self.check([((0.,0.,0.),(1.,1.,1.)),((1.,0.,0.),(2.,1.,1.))])
        self.assertEqual(result['status'],'pass');self.assertEqual(result['cell_count'],2)
        levels,changes=coverage._levels([0.,1e-16,1.,1.+2.22e-16])
        self.assertEqual(levels,[0.,1.]);self.assertEqual(len(changes),2)

    def test_missing_and_extra_domains_cannot_cancel_by_equal_total_volume(self):
        result=self.check([((0.,0.,0.),(.9,1.,1.)),((1.1,0.,0.),(2.2,1.,1.))])
        self.assertEqual(result['status'],'fail');self.assertTrue(result['missing_cells']);self.assertTrue(result['excess_cells'])
        with self.assertRaisesRegex(ValueError,'share exactly one'):
            self.check([((0.,0.,0.),(1.-1e-8,1.,1.)),((1.,0.,0.),(2.,1.,1.))])

    def test_missing_finite_proof_or_wrong_parent_is_rejected(self):
        block,w=assembly(((0.,0.,0.),(2.,1.,1.)))
        proof=geometry(w);wrong=copy.deepcopy(proof);wrong['source_snapshot_sha256']='b'*64
        with self.assertRaises(ValueError):coverage.audit_partition(w,wrong,w,[geometry(batch.local_witness(w,block['volumes'][0]['face_ids']))],[block['volumes'][0]['face_ids']])

    def test_numerically_collapsed_domain_cannot_pass_with_zero_cells(self):
        block,w=assembly(((1e16,0.,0.),(1e16+2.,1.,1.)))
        groups=[block['volumes'][0]['face_ids']]
        with self.assertRaisesRegex(ValueError,'precision|collapsed'):
            coverage.audit_partition(w,geometry(w),w,[geometry(batch.local_witness(w,groups[0]))],groups)


if __name__=='__main__':unittest.main()
