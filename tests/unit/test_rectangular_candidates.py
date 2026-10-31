import copy
import unittest

from ansa_mcp_bridge.operations import rectangular_candidates as candidate
from test_map_block_batch import assembly
from test_cad_sweep_candidates import _rehash


class RectangularCandidateTests(unittest.TestCase):
    def test_two_shared_rectangular_blocks_are_discovered_without_native_mapblock(self):
        block,w=assembly(((0,0,0),(20,30,5)),((0,0,5),(20,30,10)))
        r=candidate.discover(w,5.)
        self.assertEqual(r['status'],'topology_candidate')
        self.assertEqual({tuple(g) for g in r['face_groups']},{tuple(v['face_ids']) for v in block['volumes']})
        self.assertEqual((r['predicted_boundary_shells'],r['predicted_solids']),(112,48))
        self.assertFalse(r['continuous_export_domain_verified']);self.assertIsNone(r['prepare_options'])

    def test_three_and_four_block_manifold_junctions_are_supported(self):
        boxes=[((0,0,0),(5,5,5)),((5,0,0),(10,5,5)),((0,5,0),(5,10,5)),((5,5,0),(10,10,5))]
        for n in (3,4):
            _,w=assembly(*boxes[:n]);r=candidate.discover(w,5.)
            self.assertEqual(r['status'],'topology_candidate');self.assertEqual(len(r['face_groups']),n)

    def test_size_conflicts_and_square_face_budget_block_prepare_options(self):
        _,w=assembly(((0,0,0),(10,10,10)),((10,0,0),(20,10,10)))
        for h in (1.,3.):
            r=candidate.discover(w,h);self.assertEqual(r['status'],'unsupported');self.assertIsNone(r['prepare_options'])

    def test_incomplete_adjacency_or_nonrectangular_faces_are_not_guessed(self):
        _,w=assembly(((0,0,0),(5,5,5)),((0,0,5),(5,5,10)))
        outside=copy.deepcopy(w);outside['boundaries'][0]['face_ids'].append(999);_rehash(outside)
        self.assertIn('omits adjacent',candidate.discover(outside,5.)['reason'])
        skew=copy.deepcopy(w);skew['vertices'][0]['position'][0]+=.2;_rehash(skew)
        r=candidate.discover(skew,5.);self.assertEqual(r['status'],'unsupported');self.assertTrue(r['unsupported_faces'])

    def test_duplicate_native_hotpoints_or_faces_remain_unsupported(self):
        _,w=assembly(((0,0,0),(5,5,5)),((0,0,5),(5,5,10)))
        duplicate=copy.deepcopy(w);duplicate['vertices'][1]['position']=list(duplicate['vertices'][0]['position']);_rehash(duplicate)
        self.assertEqual(candidate.discover(duplicate,5.)['status'],'unsupported')
        face=copy.deepcopy(w['face_boundaries'][0]);face['face_id']=999
        w['face_boundaries'].append(face);w['source_scope']['face_ids'].append(999);w['parts'].append({'type':'FACE','id':999,'part_id':1})
        for row in w['boundaries']:
            if row['id'] in face['cons_ids']:row['face_ids'].append(999)
        _rehash(w)
        self.assertEqual(candidate.discover(w,5.)['status'],'unsupported')


if __name__=='__main__':unittest.main()
