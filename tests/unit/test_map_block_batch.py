import copy
import itertools
import math
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ansa_mcp_bridge.operations import map_block_batch as batch, map_block_fill as fill
from ansa_mcp_bridge.operations.cad_sweep_topology import plan_translation
from ansa_mcp_plugin.__main__ import create_server
from tests.unit.test_cad_sweep_candidates import _rehash


def assembly(*boxes):
    points = {}; faces = {}; cons = {}; volumes = []
    def point(xyz):
        if xyz not in points: points[xyz] = len(points)+1
        return points[xyz]
    for index, (low, high) in enumerate(boxes, 1):
        ids = []
        for axis in range(3):
            other = [k for k in range(3) if k != axis]
            for side in (low[axis], high[axis]):
                corners = []
                for a, b in ((0, 0), (1, 0), (1, 1), (0, 1)):
                    xyz = list(low); xyz[axis] = side
                    xyz[other[0]] = (low, high)[a][other[0]]
                    xyz[other[1]] = (low, high)[b][other[1]]
                    corners.append(point(tuple(xyz)))
                key = tuple(sorted(corners))
                if key not in faces:
                    edges = []
                    for a, b in zip(corners, corners[1:]+corners[:1]):
                        edge = tuple(sorted((a, b)))
                        if edge not in cons: cons[edge] = len(cons)+1
                        edges.append(cons[edge])
                    faces[key] = {'face_id': len(faces)+1, 'cons_ids': edges}
                ids.append(faces[key]['face_id'])
        volumes.append({'volume_id': index, 'face_ids': sorted(ids)})
    by_point = {i: xyz for xyz, i in points.items()}
    witness = {'feature_schema': 2, 'source_scope': {'face_ids': sorted(r['face_id'] for r in faces.values()), 'curve_ids': []},
               'feature_context': {'units': 'millimeter'}, 'parts': [{'type': 'FACE', 'id': r['face_id'], 'part_id': 1} for r in faces.values()],
               'face_boundaries': list(faces.values()), 'boundaries': [], 'vertices': [], 'curve_lengths': [], 'cad_export_sha256': 'a'*64}
    for ends, ident in cons.items():
        witness['boundaries'].append({'id': ident, 'face_ids': sorted(r['face_id'] for r in faces.values() if ident in r['cons_ids']),
            'native_card_length': math.dist(by_point[ends[0]], by_point[ends[1]]),
            'endpoint_topology': {'status': 'available', 'hot_point_ids': list(ends)}})
    for xyz, ident in points.items():
        witness['vertices'].append({'id': ident, 'position': list(xyz), 'cons_ids': sorted(i for ends, i in cons.items() if ident in ends)})
    _rehash(witness)
    return {'volumes': volumes}, witness


class MapBlockBatchTests(unittest.TestCase):
    def test_shared_native_boundary_is_local_closed_and_parent_bound(self):
        block, witness = assembly(((0,0,0),(1,1,1)), ((0,0,1),(1,1,2)))
        original = copy.deepcopy(witness)
        local = {v['volume_id']: batch.local_witness(witness, v['face_ids']) for v in block['volumes']}
        for row in local.values():
            self.assertEqual(row['parent_source_snapshot_sha256'], witness['source_snapshot_sha256'])
            self.assertEqual(len(row['boundaries']), 12)
            self.assertTrue(any(len(a)==3 for a in row['native_parent_adjacency']['cons_faces'].values()))
        layout = batch.assembly_layout(block, local)
        self.assertEqual(len(layout['outer_face_ids']), 10); self.assertEqual(len(layout['shared_face_ids']), 1)
        shared = layout['shared_face_ids'][0]
        first = local[1]
        source = next(r['face_id'] for r in first['face_boundaries']
                      if all(next(v['position'][2] for v in first['vertices'] if v['id']==p)==0 for c in r['cons_ids'] for p in next(e['endpoint_topology']['hot_point_ids'] for e in first['boundaries'] if e['id']==c)))
        self.assertAlmostEqual(plan_translation(first, source, shared, [0,0,1], 1., 1e-7)['expected_volume'], 1.)
        self.assertEqual(witness, original)
        with self.assertRaises(ValueError): batch.local_witness(witness, block['volumes'][0]['face_ids'][:-1])

    def test_overlap_partial_contact_and_duplicate_contact_are_rejected(self):
        for second in (((.5,0,0),(1.5,1,1)), ((1,.5,0),(2,1.5,1)), ((1,1,0),(2,2,1)), ((1,1,1),(2,2,2))):
            block, witness = assembly(((0,0,0),(1,1,1)), second)
            local = {v['volume_id']: batch.local_witness(witness, v['face_ids']) for v in block['volumes']}
            with self.assertRaises(ValueError): batch.assembly_layout(block, local)
        block, witness = assembly(((0,0,0),(1,1,1)), ((0,0,1),(1,1,2)))
        local = {v['volume_id']: batch.local_witness(witness, v['face_ids']) for v in block['volumes']}
        shared = next(iter(set(block['volumes'][0]['face_ids']) & set(block['volumes'][1]['face_ids'])))
        block['volumes'][1]['face_ids'].remove(shared); block['volumes'][1]['face_ids'].append(999)
        with self.assertRaises(ValueError): batch.assembly_layout(block, local)

    def test_public_batch_selection_is_explicit_and_keeps_single_schema(self):
        calls = []
        def submit(tool, params, operation_id, expected_target, expected_model, ttl_seconds):
            calls.append((tool, params)); return {'task': {'target': expected_target, 'model': expected_model}, 'state': 'queued', 'result': None,
                'dispatch': None, 'cancel_pending': False, 'acked': False, 'operation_id': operation_id, 'task_id': 't'}
        tool = create_server(SimpleNamespace(submit_task=submit))._tool_manager.get_tool('ansa_mesh_map_block_fill')
        params = dict(action='plan', operation_id='multi', expected_target={'backend_id':'b','instance_id':'i','session_id':'s','generation':1}, expected_model={'epoch':1}, deck=1, volume_ids=[2,1])
        parsed = tool.fn_metadata.arg_model.model_validate(params)
        values = parsed.model_dump(); values.update(expected_target=parsed.expected_target, expected_model=parsed.expected_model)
        tool.fn(**values)
        self.assertEqual(calls[-1][1], {'deck':1, 'volume_ids':[2,1]})
        for delta in ({'volume_id':1}, {'volume_ids':None}):
            with self.assertRaises(ValueError): tool.fn(**{**values, **delta})
        self.assertEqual(fill.normalize({'deck':1,'volume_ids':[2,1]}), {'deck':1,'volume_ids':[1,2]})
        with self.assertRaises(ValueError): fill.normalize({'deck':1,'volume_ids':[1,1]})

    def test_complete_manifold_three_and_four_block_junctions_are_allowed(self):
        boxes=[((0,0,0),(1,1,1)),((1,0,0),(2,1,1)),((0,1,0),(1,2,1)),((1,1,0),(2,2,1))]
        for count in (3,4):
            block,witness=assembly(*boxes[:count])
            local={v['volume_id']:batch.local_witness(witness,v['face_ids']) for v in block['volumes']}
            self.assertTrue(batch.assembly_layout(block,local)['shared_face_ids'])

    def test_duplicate_boundary_coordinates_are_rejected_before_lattices_or_native_fill(self):
        block={'volumes':[{'face_mesh_membership_complete':True}],
               'face_membership':[{'macro_shell_ids':[1]}]}
        duplicate={'nodes':[{'id':1,'xyz':[0.,0.,0.]},{'id':2,'xyz':[0.,0.,0.]}],
                   'elements':[{'id':1,'type':'CQUAD4','nodes':[1,3,4,5]}]}
        with patch.object(fill.native,'capture_source',return_value=duplicate), patch.object(fill.cad_map,'_face_lattice') as lattice:
            with self.assertRaises(ValueError):fill._boundary(Mock(),Mock(),1,block,{}, {},1,2)
        lattice.assert_not_called()

    def test_batch_is_one_native_call_with_failure_receipt_and_no_retry(self):
        clean = {'deck':1,'volume_ids':[1,2]}
        prepared = {'batch':True,'clean':clean,'context':{},'before':{'entities':[]},'plan_sha256':'a'*64,'target_length':5.,'settings':{'snapshot_sha256':'b'*64}}
        mesh = Mock(); mesh.VolumesRemesh.return_value = 0
        def run(state,base,mesh,length,callback,result,**kwargs):
            try: return callback()
            finally: result['restored']=True
        with patch.object(batch.native,'_backup_preflight',return_value=()), patch.object(batch.native,'_verify_backup',return_value={}), \
             patch.object(batch,'preflight',return_value=prepared), patch.object(batch.guards,'_same_context'), \
             patch.object(batch.features,'_entity',side_effect=lambda b,d,k,i:i), patch.object(batch.meshing_context,'run_with_context',side_effect=run), \
             patch.object(batch.cad_map,'_snapshot',return_value={'entities':[]}):
            result = fill.apply_prepared(Mock(),Mock(),mesh,Mock(),{**clean,'expected_plan_sha256':'a'*64,'backup_path':'new.ansa'},prepared)
        mesh.VolumesRemesh.assert_called_once_with([1,2])
        self.assertFalse(result['ok']); self.assertTrue(result['recovery_required']); self.assertTrue(result['restored'])


if __name__=='__main__':unittest.main()
