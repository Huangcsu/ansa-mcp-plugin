"""Finite read-only rectangular block discovery from native FACE/CONS identities.

This does not cut CAD or invoke MapBlock. Topological rectangles are only
candidates until the caller verifies every complete native export domain.
"""
import itertools
import math

from . import cad_map, cad_sweep_candidates as candidates, map_block_batch as batch
from .cad_sweep_topology import _trace_face_loop


def _unsupported(parent, reason, **details):
    return {'status':'unsupported','template':'T2_rectangular_native_MapBlock_assembly',
            'source_snapshot_sha256':parent['source_snapshot_sha256'],'reason':reason,
            'face_groups':[],'prepare_options':None,'usable_for_mutation':False,
            'continuous_export_domain_verified':False,**details}


def discover(witness,target_length):
    parsed=candidates._validate_witness(witness)
    if type(target_length) not in (int,float) or not math.isfinite(target_length) or target_length<=0:
        raise ValueError('target_length must be finite and positive')
    if len(set(parsed['face_parts'].values()))!=1:
        return _unsupported(witness,'rectangular assembly requires one source Part')
    if any(set(r['face_ids'])-set(parsed['face_ids']) for r in witness['boundaries']):
        return _unsupported(witness,'selected scope omits adjacent native FACEs')
    points={r['id']:r['position'] for r in witness['vertices']}
    index=cad_map._SpatialIndex(cad_map.TOLERANCE)
    try:
        for ident,xyz in points.items():index.add_unique(tuple(xyz),ident,'native HOT POINTs')
    except ValueError as exc:return _unsupported(witness,str(exc))
    boundaries={r['id']:{'endpoints':r['endpoint_topology']['hot_point_ids']} for r in witness['boundaries']
                if r['endpoint_topology']['status']=='available'}
    rectangles=[];by_corners={};unsupported=[]
    for face in witness['face_boundaries']:
        ident=face['face_id']
        if len(face['cons_ids'])!=4 or any(c not in boundaries for c in face['cons_ids']):
            unsupported.append({'face_id':ident,'reason':'requires four native CONS with complete endpoints'});continue
        try:
            loop,_=_trace_face_loop(ident,face['cons_ids'],boundaries)
            corners=[points[i] for i in loop]
            low=[min(p[a] for p in corners) for a in range(3)];high=[max(p[a] for p in corners) for a in range(3)]
            flat=[a for a in range(3) if high[a]-low[a]<=cad_map.TOLERANCE]
            if len(loop)!=4 or len(flat)!=1:raise ValueError('not an axis-aligned planar rectangle')
            axis=flat[0];other=[a for a in range(3) if a!=axis]
            expected=list(itertools.product(*[(low[a],high[a]) for a in other]))
            matches=[[i for i,pair in enumerate(expected) if math.dist([p[a] for a in other],pair)<=cad_map.TOLERANCE] for p in corners]
            if any(len(m)!=1 for m in matches) or len({m[0] for m in matches})!=4:raise ValueError('rectangle corners are ambiguous')
            key=tuple(sorted(loop))
            if key in by_corners:raise ValueError('duplicate native FACE domains')
            row={'face_id':ident,'axis':axis,'plane':low[axis],'min':low,'max':high,'corner_ids':key}
            rectangles.append(row);by_corners[key]=ident
        except (ValueError,KeyError) as exc:unsupported.append({'face_id':ident,'reason':str(exc)})
    if unsupported:
        return _unsupported(witness,'one or more selected FACEs lie outside the rectangular topology template',unsupported_faces=unsupported)
    groups=set();pairs_checked=0
    for first,second in itertools.combinations(rectangles,2):
        if first['axis']!=second['axis']:continue
        pairs_checked+=1;axis=first['axis'];other=[a for a in range(3) if a!=axis]
        if abs(first['plane']-second['plane'])<=cad_map.TOLERANCE:continue
        if any(abs(first[k][a]-second[k][a])>cad_map.TOLERANCE for k in ('min','max') for a in other):continue
        low=list(first['min']);high=list(first['max']);low[axis]=min(first['plane'],second['plane']);high[axis]=max(first['plane'],second['plane'])
        corner_ids={}
        for bits in itertools.product((0,1),repeat=3):
            xyz=tuple((low,high)[bits[a]][a] for a in range(3));matched=index.matches(xyz)
            if len(matched)!=1:break
            corner_ids[bits]=matched[0][0]
        if len(corner_ids)!=8:continue
        six=[]
        for a in range(3):
            for side in (0,1):
                key=tuple(sorted(i for bits,i in corner_ids.items() if bits[a]==side))
                if key in by_corners:six.append(by_corners[key])
        if len(set(six))==6:groups.add(tuple(sorted(six)))
    if not 2<=len(groups)<=20:return _unsupported(witness,'no bounded unambiguous complete rectangular block assembly',candidate_block_count=len(groups))
    face_groups=[list(g) for g in sorted(groups)]
    if {i for g in face_groups for i in g}!=set(parsed['face_ids']):
        return _unsupported(witness,'rectangular candidates do not cover every selected native FACE',candidate_face_groups=face_groups)
    try:
        local={i:batch.local_witness(witness,g) for i,g in enumerate(face_groups,1)}
        block={'volumes':[{'volume_id':i,'face_ids':g} for i,g in enumerate(face_groups,1)]}
        layout=batch.assembly_layout(block,local)
        from .map_block_assembly_prepare import _segments_for_length
        counts={r['id']:_segments_for_length(math.dist(points[r['endpoint_topology']['hot_point_ids'][0]],points[r['endpoint_topology']['hot_point_ids'][1]]),target_length,'native rectangular edge') for r in witness['boundaries']}
        boundary_shells=0
        for face in witness['face_boundaries']:
            sides=sorted({counts[c] for c in face['cons_ids']})
            boundary_shells+=sides[0]**2 if len(sides)==1 else math.prod(sides)
        if boundary_shells>400:raise ValueError('rectangular assembly exceeds 400 boundary SHELL budget')
        predicted=sum(math.prod(_segments_for_length(b['max'][a]-b['min'][a],target_length,'rectangular dimension') for a in range(3)) for b in layout['bounds'].values())
        if predicted>5000:raise ValueError('rectangular assembly exceeds 5000 SOLID budget')
        alternatives={}
        for ident,w in local.items():
            found=candidates.discover(w,target_length)
            choices=found['components'][0].get('alternatives',[]) if len(found['components'])==1 else []
            if not choices:raise ValueError('rectangular block lacks complete T1 native topology')
            alternatives[ident]=choices
    except (ValueError,KeyError,RuntimeError) as exc:return _unsupported(witness,str(exc),candidate_face_groups=face_groups)
    return {'status':'topology_candidate','template':'T2_rectangular_native_MapBlock_assembly',
            'source_snapshot_sha256':witness['source_snapshot_sha256'],'face_groups':face_groups,
            'complete_selected_FACE_coverage':True,'pair_candidates_checked':pairs_checked,
            'predicted_boundary_shells':boundary_shells,'predicted_solids':predicted,
            'layout':layout,'blocks':[{'id':i,'face_ids':g,'alternatives':alternatives[i]} for i,g in enumerate(face_groups,1)],
            'prepare_options':None,'usable_for_mutation':False,'continuous_export_domain_verified':False,
            'whole_model_inventory_checked':False,'property_and_current_seeds_verified':False}
