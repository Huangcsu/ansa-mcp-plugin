"""Finite cell-arrangement coverage proof for native rectangular CAD partitions.

Pure evidence analysis: callers must independently bind both native captures.
No approximate node merging or geometry modification is performed.
"""
import math
import re

from . import map_block_batch as batch


def _geometry(witness, proof):
    ids = set(witness['source_scope']['face_ids'])
    if (proof.get('status') != 'verified_at_export_precision'
            or proof.get('source_snapshot_sha256') != witness['source_snapshot_sha256']):
        raise ValueError('finite CAD proof is incomplete or bound to a different witness')
    faces = proof.get('faces')
    if type(faces) is not list or len(faces) != len(ids) or {r.get('face_id') for r in faces} != ids:
        raise ValueError('finite CAD proof must cover every declared native FACE exactly once')
    for row in faces:
        if (row.get('status') != 'verified_at_export_precision' or row.get('continuous_export_domain_verified') is not True
                or re.fullmatch('[0-9a-f]{64}', str(row.get('cad_export_sha256'))) is None):
            raise ValueError('every FACE requires a verified, hash-bound finite export domain')


def _levels(values):
    levels = []; changes = []
    for value in sorted(values):
        if levels and abs(value-levels[-1]) <= 8*max(math.ulp(value), math.ulp(levels[-1]), math.ulp(1.)):
            if value != levels[-1]:changes.append({'raw': value, 'representative': levels[-1], 'difference': value-levels[-1]})
        else:levels.append(value)
    return levels, changes


def audit_partition(source_witness, source_geometry, target_witness, target_geometries, face_groups):
    """Check complete original box coverage, not merely total volume agreement.

    Every nonempty cell of the common interval arrangement must have exactly one
    target owner. Missing/extra regions remain failures. Only eight-ULP derived
    endpoint roundoff is considered numerically consistent; raw inputs remain.
    """
    source = batch.local_witness(source_witness, source_witness['source_scope']['face_ids'])
    _geometry(source_witness, source_geometry)
    bounds = batch.rectangular_bounds(source)
    if type(face_groups) is not list or not 1 <= len(face_groups) <= 20 or len(target_geometries) != len(face_groups):
        raise ValueError('one finite geometry proof per group, with at most twenty groups, is required')
    volumes = [{'volume_id': i+1, 'face_ids': sorted(ids)} for i, ids in enumerate(face_groups)]
    local = {v['volume_id']: batch.local_witness(target_witness, v['face_ids']) for v in volumes}
    if {i for v in volumes for i in v['face_ids']} != set(target_witness['source_scope']['face_ids']):
        raise ValueError('target groups omit native FACEs from the complete target witness')
    for ident, proof in enumerate(target_geometries, 1):_geometry(local[ident], proof)
    layout = batch.assembly_layout({'volumes': volumes}, local)
    targets = list(layout['bounds'].values()); levels = []; roundoff = []
    for axis in range(3):
        values = [bounds[k][axis] for k in ('min', 'max')]+[b[k][axis] for b in targets for k in ('min','max')]
        row, delta = _levels(values); levels.append(row); roundoff.extend({'axis':axis,**r} for r in delta)
    count = math.prod(len(row)-1 for row in levels)
    if count < 1:raise ValueError('coverage domain is unresolved at coordinate floating-point precision')
    if count > 100000:raise ValueError('coverage interval arrangement exceeds 100000 cells')
    def snapped(value,axis):return min(levels[axis],key=lambda x:abs(x-value))
    all_bounds = [{k:[snapped(b[k][a],a) for a in range(3)] for k in ('min','max')} for b in [bounds]+targets]
    reference, targets = all_bounds[0], all_bounds[1:]
    if any(b['min'][a]>=b['max'][a] for b in all_bounds for a in range(3)):
        raise ValueError('coverage domain collapsed during derived endpoint roundoff handling')
    missing = []; excess = []; duplicate = []; checked = 0
    for i in range(len(levels[0])-1):
        for j in range(len(levels[1])-1):
            for k in range(len(levels[2])-1):
                indices = (i,j,k); low=[levels[a][indices[a]] for a in range(3)]; high=[levels[a][indices[a]+1] for a in range(3)]
                # Membership is constant on each open interval cell; this is a
                # complete arrangement classification, not point sampling of CAD.
                def contains(box):return all(box['min'][a]<=low[a] and high[a]<=box['max'][a] for a in range(3))
                wanted=contains(reference); owners=[n+1 for n,b in enumerate(targets) if contains(b)]
                checked+=1
                if (wanted and len(owners)==1) or (not wanted and not owners):continue
                item={'min':low,'max':high,'owners':owners,'volume_mm3':math.prod(high[a]-low[a] for a in range(3))}
                if wanted and not owners:missing.append(item)
                if not wanted and owners:excess.append(item)
                if len(owners)>1:duplicate.append(item)
    return {'status':'pass' if not (missing or excess or duplicate) else 'fail',
            'template':'finite_rectangular_partition','source_snapshot_sha256':source_witness['source_snapshot_sha256'],
            'target_snapshot_sha256':target_witness['source_snapshot_sha256'],
            'complete_source_face_ids':source_witness['source_scope']['face_ids'],
            'complete_target_face_ids':target_witness['source_scope']['face_ids'],
            'cell_count':checked,'missing_cells':missing,'excess_cells':excess,'duplicate_cells':duplicate,
            'derived_endpoint_roundoff':roundoff,'raw_inputs_modified':False,
            'continuous_export_domain_verified':True,'exact_native_CAD_equivalence_proven':False,
            'native_model_binding_verified':False,'mesh_quality_proven':False}
