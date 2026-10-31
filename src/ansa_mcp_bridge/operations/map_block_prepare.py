"""Backed-up native MapBlock preparation of a proved six-face CAD domain.

This creates a surface mesh and an unmeshed Map Block, never a final solid mesh.
The first contract deliberately excludes arbitrary industrial components.
"""
import copy
import json
import math

from . import cad_map, cons_seed as seeds, geometry_features as features, inspection
from . import map_block_inspection, meshing, part_aggregates
from . import structured_native as native, volume_meshing as guards
from .cad_sweep_geometry import capture as capture_geometry
from .cad_sweep_topology import plan_translation
from .mapped_surface_topology import four_sides
from .structured_batch import source_subset

_PARAMS = ('deck', 'face_ids', 'source_face_id', 'target_face_id', 'direction',
           'distance', 'target_length', 'property_id')


def normalize(params, applying=False):
    inspection._params(params, _PARAMS+(('expected_plan_sha256', 'backup_path') if applying else ()))
    clean = {k: params[k] for k in _PARAMS}
    for key in ('deck', 'source_face_id', 'target_face_id', 'property_id'):
        clean[key] = native._positive_int(clean[key], key)
    clean['face_ids'] = sorted(native._id_list(clean['face_ids'], 'face_ids', 6))
    if len(clean['face_ids']) != 6 or clean['source_face_id'] == clean['target_face_id']:
        raise ValueError('MapBlock requires six distinct FACEs and distinct caps')
    if not {clean['source_face_id'], clean['target_face_id']} <= set(clean['face_ids']):
        raise ValueError('both caps must belong to face_ids')
    vector = native._point(clean['direction'], 'direction')
    clean['direction'] = vector if abs(math.hypot(*vector)-1.) <= 1e-14 else native._unit_vector(vector, 'direction')
    for key in ('distance', 'target_length'):
        clean[key] = features._number(clean[key], key, True)
    if applying:
        clean['expected_plan_sha256'] = native._validate_digest(params['expected_plan_sha256'], 'expected_plan_sha256')
        clean['backup_path'] = params['backup_path']
    return clean


def preflight(state, base, mesh, constants, params):
    from . import meshing_context
    state.assert_main(); clean = normalize({k: params[k] for k in _PARAMS})
    deck = inspection._deck(base, clean); native._check_nastran(constants, deck)
    context = guards._execution_context(state, deck)
    for module, names in ((base, ('CalcElementMass', 'DeleteEntity', 'SetEntityCardValues')),
                          (mesh, ('MapBlock', 'GetVolumeBoundary', 'GetMapVolumeBoundaries'))):
        for name in names:
            if not callable(getattr(module, name, None)):
                raise NotImplementedError('required native API unavailable: '+name)
    if sorted(e._id for e in seeds._collect(base, deck, None, 'FACE')) != clean['face_ids']:
        raise ValueError('first MapBlock contract requires the complete six-FACE model')
    if any(seeds._collect(base, deck, None, k) for k in ('SHELL', 'SOLID', 'VOLUME', 'CURVE')):
        raise ValueError('MapBlock prepare requires an unmeshed model without SHELL/SOLID/VOLUME/CURVE')
    witness, _ = features._witness(state, base, deck, {'face_ids': clean['face_ids'], 'curve_ids': []}, {'scoped_export_calls': 0})
    topology = plan_translation(witness, clean['source_face_id'], clean['target_face_id'], clean['direction'], clean['distance'], cad_map.TOLERANCE)
    geometry = capture_geometry(state, base, deck, witness, topology)
    if geometry['status'] != 'verified_at_export_precision':
        raise ValueError('finite continuous CAD domain is unsupported')
    parts = {r['part_id'] for r in witness['parts']}
    if len(parts) != 1 or len(seeds._collect(base, deck, None, 'ANSAPART')) != 1:
        raise ValueError('first MapBlock contract requires one complete source Part')
    part_id = next(iter(parts)); part = features._entity(base, deck, 'ANSAPART', part_id)
    part_fields = seeds._card(base, deck, part)['fields']
    if part_fields.get('Freeze') != 'NO' or part_fields.get('Is Group') != 'NO':
        raise ValueError('source Part must be unfrozen and not a group')
    properties = set()
    for ident in clean['face_ids']:
        face, row, shell_ids = meshing._face_snapshot(base, mesh, deck, ident)
        fields = seeds._card(base, deck, face)['fields']
        if row['macro_face_ids'] != [ident] or shell_ids or fields.get('Frozen') != 'NO' or native._volume_count(fields.get('Num. Volumes')) != 0:
            raise ValueError('six independent unmeshed, unfrozen FACE macros are required')
        properties.add(native._face_assignments(base, deck, face, ident)['property_id'])
    if len(properties) != 1:
        raise ValueError('first MapBlock contract requires one source PSHELL')
    source_pid = next(iter(properties))
    prop, property_evidence = native._psolid_preflight(base, deck, clean['property_id'])
    density = features._number(property_evidence['mat1']['fields'].get('RHO'), 'target MAT1 RHO')
    if density < 0:
        raise ValueError('target MAT1 requires an explicit nonnegative density for mass readback')
    cons_ids = sorted(r['id'] for r in witness['boundaries'])
    seed = seeds.capture(state, base, deck, clean['face_ids'], cons_ids)
    if seed['geometry_witness']['source_snapshot_sha256'] != witness['source_snapshot_sha256']:
        raise RuntimeError('CAD source changed during MapBlock preflight')
    lengths = [math.dist(r['hot_points'][0]['xyz'], r['hot_points'][1]['xyz']) for r in seed['boundaries'] if r['status'] == 'supported']
    if len(lengths) != 12 or any(r['native_fields']['Frozen'] != 'NO' or r['native_fields']['Biasing'] != 'LINEAR' for r in seed['boundaries']):
        raise ValueError('twelve unfrozen, first-order linear CONS boundaries required')
    if min(lengths) < clean['target_length'] or max(lengths)/clean['target_length'] > 20:
        raise ValueError('target length must fit every edge, with at most 20 nominal segments per edge')
    before = cad_map._snapshot(state, base, deck)
    if len(before['entities'])+6*21*21+6*20*20+3 > 10000:
        raise ValueError('MapBlock reservation exceeds the model protection budget')
    old = native._scope_map(before)
    if native._volume_count(old[('PSHELL', source_pid)]['fields'].get('Num.Elem')) != 0:
        raise ValueError('source PSHELL element count must be explicitly zero')
    part_aggregates.capture(base, deck, part, old[('ANSAPART', part_id)])
    settings = meshing_context.capture(state, base, mesh)
    guards._same_context(state, deck, context)
    body = {'params': clean, 'context': context, 'scope_sha256': features._hash(before),
            'geometry': geometry, 'seed_sha256': seed['seed_snapshot_sha256'],
            'meshing_context_sha256': settings['snapshot_sha256'], 'property': property_evidence}
    return {'clean': clean, 'context': context, 'before': before, 'witness': witness,
            'geometry': geometry, 'topology': topology, 'seed': seed, 'part': part,
            'part_id': part_id, 'source_pid': source_pid, 'property': prop,
            'property_evidence': property_evidence, 'settings': settings,
            'plan_sha256': features._hash(body)}


def public_plan(prepared):
    return {'status': 'ready_for_controlled_apply', 'params': prepared['clean'],
            'plan_sha256': prepared['plan_sha256'], 'geometry': prepared['geometry'],
            'scope_sha256': features._hash(prepared['before']),
            'meshing_context_sha256': prepared['settings']['snapshot_sha256'],
            'source_snapshot_sha256': prepared['witness']['source_snapshot_sha256'],
            'usable_for_mutation': False, 'solid_mesh_generated': False}


def validate_report(state, base, mesh, constants, deck, result):
    current = preflight(state, base, mesh, constants, result['params'])
    if current['plan_sha256'] != result.get('plan_sha256'):
        raise ValueError('MapBlock prepare plan is stale')


def _bind_assets(base, deck, prepared, returned, result):
    """Only rebind/delete assets independently observed as new in this operation."""
    interim = cad_map._snapshot(prepared['state'], base, deck)
    old, new = native._scope_map(prepared['before']), native._scope_map(interim)
    if old.keys()-new.keys():
        raise RuntimeError('MapBlock removed original entities')
    added = new.keys()-old.keys()
    if any(kind not in ('GRID', 'SHELL', 'VOLUME', 'PSOLID', 'MAT1') for kind, _ in added):
        raise RuntimeError('MapBlock created unexpected entity types')
    ids = {kind: sorted(i for k, i in added if k == kind) for kind in ('VOLUME', 'PSOLID', 'MAT1')}
    if any(len(v) != 1 for v in ids.values()) or type(returned) not in (list, tuple) or len(returned) != 1:
        raise RuntimeError('MapBlock returned an unsupported native asset pattern')
    volume_id = ids['VOLUME'][0]
    if returned[0].ansa_type(deck) != 'VOLUME' or returned[0]._id != volume_id:
        raise RuntimeError('MapBlock return differs from independent VOLUME inventory')
    auto_pid, auto_mid = ids['PSOLID'][0], ids['MAT1'][0]
    if new[('VOLUME', volume_id)]['fields'].get('PID') != auto_pid or new[('PSOLID', auto_pid)]['fields'].get('MID') != auto_mid:
        raise RuntimeError('new native asset references differ from observed additions')
    volume = features._entity(base, deck, 'VOLUME', volume_id)
    if base.SetEntityCardValues(deck, volume, {'PID': prepared['clean']['property_id']}) != 0:
        raise RuntimeError('explicit VOLUME property assignment failed')
    if base.GetEntityCardValues(deck, volume, ['PID']).get('PID') != prepared['clean']['property_id']:
        raise RuntimeError('explicit VOLUME property assignment not effective')
    # force=False prevents deleting a referenced asset; compress=False protects
    # unrelated unused entities. A final full snapshot checks every original ID.
    result['native_asset_cleanup'] = []
    for kind, ident in (('PSOLID', auto_pid), ('MAT1', auto_mid)):
        entity = features._entity(base, deck, kind, ident)
        rc = base.DeleteEntity(entity, force=False, compress=False)
        absent = base.GetEntity(deck, kind, ident) is None
        result['native_asset_cleanup'].append({'type': kind, 'id': ident, 'return_code': rc, 'absent': absent})
        if rc != 0 or not absent:
            raise RuntimeError('unused native asset cleanup not confirmed')
    return volume_id


def _part_summaries(base, deck, prepared, row):
    fields = row['fields']; source_pid = prepared['source_pid']
    source = seeds._card(base, deck, features._entity(base, deck, 'PSHELL', source_pid))['fields']
    target = prepared['property_evidence']['psolid']['fields']; pid = target['PID']
    props, materials, names, thicknesses = [], [], {}, []
    for ident, kind, data in sorted(((source_pid, 'PSHELL', source), (pid, 'PSOLID', target))):
        props.append({'Id': str(ident), 'Name': data['Name'], 'Type': kind})
        mids = {native._positive_int(data[k], k) for k in (('MID1', 'MID2', 'MID3', 'MID4') if kind == 'PSHELL' else ('MID',)) if data.get(k) is not None}
        mats = []
        for mid in sorted(mids):
            mat = seeds._card(base, deck, features._entity(base, deck, 'MAT1', mid))['fields']
            mats.append({'Id': str(mid), 'Name': mat['Name'], 'Type': 'MAT1'}); names[mid] = mat['Name']
        materials.append({str(ident): mats})
        thicknesses.append(format(features._number(data['T'], 'PSHELL thickness', True), 'g') if kind == 'PSHELL' else '-')
    expected = {'PID': ', '.join(str(p['Id']) for p in props), 'PIDs Number': 2,
                'Thickness': ', '.join(thicknesses), 'Material Name': ', '.join(names[mid] for mid in sorted(names))}
    for key, value in expected.items():
        if fields.get(key) != value:
            raise RuntimeError('Part '+key+' differs from explicit geometry/volume property definitions')
    if json.loads(fields.get('Properties Info', '')) != props or json.loads(fields.get('Materials Info', '')) != materials:
        raise RuntimeError('Part material/property summaries differ from protected definitions')
    if type(fields.get('Last Edit')) is not str or not 0 < len(fields['Last Edit']) <= 128:
        raise RuntimeError('Part edit stamp is unavailable')
    evidence = part_aggregates.capture(base, deck, prepared['part'], row)
    return set(expected)|{'Properties Info', 'Materials Info', 'Last Edit'}|set(part_aggregates.MASS_FIELDS), evidence


def _check_face_center(before, after, native_mass, diagonal):
    """Bound the observed tessellation-center roundoff without ignoring geometry."""
    if type(native_mass) not in (list, tuple) or len(native_mass) != 22:
        raise RuntimeError('native FACE aggregate readback requires 22 values')
    native_cog = [features._number(v, 'native FACE COG') for v in native_mass[1:4]]
    left = native._point(before['geometry'].get('center'), 'original FACE center')
    right = native._point(after['geometry'].get('center'), 'current FACE center')
    tolerance = features._number(diagonal, 'Part diagonal', True)*1e-14
    if math.dist(left, right) > tolerance or math.dist(right, native_cog) > tolerance:
        raise RuntimeError('FACE center differs beyond independently recomputed roundoff bound')
    evidence = {'before': left, 'after': right, 'native_cog': native_cog, 'tolerance_mm': tolerance}
    # Only this checked derived value changes in the comparison copies. Area,
    # bounds, normal, complete cards and the entire CAD export remain exact.
    before['geometry']['center'] = after['geometry']['center'] = None
    return evidence


def _check_cons_vector_roundoff(before, after):
    """Accept only unit-tangent display rounding; keep raw values in evidence."""
    result = []; tolerance = 8*math.ulp(1.)
    for key in ('Start Vector', 'End Vector'):
        left, right = before['fields'].get(key), after['fields'].get(key)
        if left == right:continue
        def parse(value):
            if type(value) is not str:raise RuntimeError('CONS tangent display is unreadable')
            try:values = [float(v.strip()) for v in value.split(',')]
            except ValueError as exc:raise RuntimeError('CONS tangent display is not numeric') from exc
            if len(values) != 3 or not all(math.isfinite(v) for v in values) or abs(math.hypot(*values)-1.) > 1e-5:
                raise RuntimeError('CONS tangent display requires three finite unit-vector values')
            return values
        a,b = parse(left),parse(right); delta = max(abs(x-y) for x,y in zip(a,b))
        if delta > tolerance:raise RuntimeError('CONS tangent changed beyond numerical display roundoff')
        result.append({'field':key,'before':left,'after':right,'maximum_component_change':delta,'tolerance':tolerance})
        before['fields'][key] = after['fields'][key] = None
    return result


def _check_cons_bounds_roundoff(before, after, hot_points=None):
    left=before.get('geometry',{}).get('bounds'); right=after.get('geometry',{}).get('bounds')
    if left==right:return []
    if (type(left) not in (list,tuple) or type(right) not in (list,tuple) or len(left)!=6 or len(right)!=6
            or any(type(v) not in (int,float) or not math.isfinite(v) for v in list(left)+list(right))):
        raise RuntimeError('CONS derived bounds are unreadable')
    length_scale=max(1.,math.dist(left[:3],left[3:]),math.dist(right[:3],right[3:]))
    tolerances=[8*max(math.ulp(a),math.ulp(b),math.ulp(length_scale)) for a,b in zip(left,right)]
    if any(abs(a-b)>t for a,b,t in zip(left,right,tolerances)):
        raise RuntimeError('CONS derived bounds changed beyond numerical roundoff')
    if hot_points is not None:
        expected=[min(p[k] for p in hot_points) for k in range(3)]+[max(p[k] for p in hot_points) for k in range(3)]
        if any(abs(v-e)>t for values in (left,right) for v,e,t in zip(values,expected,tolerances)):
            raise RuntimeError('derived bounds differ from the unchanged native endpoint bounds')
    result=[{'before':list(left),'after':list(right),'component_tolerances_mm':tolerances}]
    before['geometry']['bounds']=after['geometry']['bounds']=None
    return result


def _check_face_geometry_roundoff(before, after, witness):
    """Recompute the proved planar quad's area/bounds before comparison cleanup."""
    from .cad_sweep_topology import _trace_face_loop, _polygon_area_vector
    ident=before['entity']['id']; face=next(r for r in witness['face_boundaries'] if r['face_id']==ident)
    boundaries={r['id']:{'endpoints':r['endpoint_topology']['hot_point_ids']} for r in witness['boundaries']}
    loop,_=_trace_face_loop(ident,face['cons_ids'],boundaries)
    points={r['id']:r['position'] for r in witness['vertices']};corners=[points[i] for i in loop]
    result=[]
    left,right=before['geometry'],after['geometry']
    if left.get('area')!=right.get('area'):
        expected=math.hypot(*_polygon_area_vector(corners));a=features._number(left.get('area'),'original FACE area',True);b=features._number(right.get('area'),'current FACE area',True)
        tolerance=8*max(math.ulp(a),math.ulp(b),math.ulp(expected))
        if abs(a-expected)>tolerance or abs(b-expected)>tolerance or abs(a-b)>tolerance:
            raise RuntimeError('FACE area changed beyond independently recomputed roundoff')
        result.append({'field':'area','before':a,'after':b,'analytic_quad_area':expected,'tolerance_mm2':tolerance})
        left['area']=right['area']=None
    for row in _check_cons_bounds_roundoff(before,after,corners):result.append({'field':'bounds',**row})
    if left.get('normal')!=right.get('normal'):
        a=native._point(left.get('normal'),'original FACE normal');b=native._point(right.get('normal'),'current FACE normal')
        tolerance=8*math.ulp(max(1.,math.hypot(*a),math.hypot(*b)))
        if max(abs(x-y) for x,y in zip(a,b))>tolerance:raise RuntimeError('FACE normal changed beyond numerical roundoff')
        result.append({'field':'normal','before':a,'after':b,'tolerance':tolerance});left['normal']=right['normal']=None
    return result


def readback(state, base, mesh, constants, prepared, volume_id):
    clean = prepared['clean']; deck = clean['deck']
    guards._same_context(state, deck, prepared['context'])
    block = map_block_inspection.capture(state, base, mesh, constants, deck, [volume_id])
    v = block['volumes'][0]
    if (v['native_mapping_status'] != 'native_ready' or not v['caps_match_complete_boundary']
            or not v['face_mesh_membership_complete'] or v['face_ids'] != clean['face_ids']
            or v['part_id'] != prepared['part_id'] or v['card']['fields'].get('PID') != clean['property_id']
            or v['card']['fields'].get('Status') != 'Unmeshed'
            or sorted(len(v['caps'][k]) for k in ('master', 'slave', 'round')) != [1, 1, 4]):
        raise RuntimeError('native Map Block state, caps, ownership or property mismatch')
    volume_fields = v['card']['fields']
    for name in ('Hexas', 'Pentas', 'Tetras', 'Pyramids', 'Polyhedrals', 'Grids', 'Internal grids'):
        if native._volume_count(volume_fields.get(name)) != 0:
            raise RuntimeError('unmeshed MapBlock '+name+' count is not zero')
    cad_map._close_number(volume_fields.get('Volume'), prepared['topology']['expected_volume'], 'MapBlock volume')
    density = prepared['property_evidence']['mat1']['fields']['RHO']
    cad_map._close_number(volume_fields.get('Mass'), prepared['topology']['expected_volume']*density, 'MapBlock mass')
    current_seed = seeds.capture(state, base, deck, clean['face_ids'], [r['cons_id'] for r in prepared['seed']['boundaries']])
    # Actual perimeter grids may be instantiated only inside this backed-up write.
    boundaries = [seeds._boundary(base, deck, features._entity(base, deck, 'CONS', r['cons_id']), include_points=True) for r in current_seed['boundaries']]
    if any(r['status'] != 'supported' for r in boundaries):
        raise RuntimeError('native CONS boundary point validation failed')
    shell_ids = sorted(e['id'] for e in block['surface_mesh']['elements'])
    if not shell_ids or len(shell_ids) > 5000:
        raise RuntimeError('MapBlock SHELL budget exceeded or empty')
    source = native.capture_source(base, constants, deck, shell_ids)
    face_cons = {r['face_id']: r['cons_ids'] for r in current_seed['geometry_witness']['face_boundaries']}
    lattice = []
    for face in block['face_membership']:
        sides = four_sides(face_cons[face['face_id']], boundaries)
        by_id = {r['cons_id']: r for r in boundaries}
        for side in sides:
            points = {r['id']: r['xyz'] for r in by_id[side['cons_id']]['hot_points']}
            side['start_xyz'], side['end_xyz'] = points[side['start']], points[side['end']]
        subset = source_subset(source, face['macro_shell_ids'])
        lattice.append({'face_id': face['face_id'], 'lattice': cad_map._face_lattice(subset, sides)})
    after = cad_map._snapshot(state, base, deck)
    old, new = native._scope_map(prepared['before']), native._scope_map(after)
    if old.keys()-new.keys():
        raise RuntimeError('MapBlock removed original entities')
    added = new.keys()-old.keys()
    if any(k not in ('SHELL', 'GRID', 'VOLUME') for k, _ in added) or sorted(i for k, i in added if k == 'VOLUME') != [volume_id]:
        raise RuntimeError('unexpected final MapBlock additions')
    if sorted(i for k, i in added if k == 'SHELL') != shell_ids:
        raise RuntimeError('new SHELL inventory differs from FACE membership')
    new_grid_ids = sorted(i for k, i in added if k == 'GRID')
    if not set(new_grid_ids) <= {n['id'] for n in source['nodes']}:
        raise RuntimeError('MapBlock created orphan/non-boundary GRID')
    from .mapped_surface import _check_new_grids
    _check_new_grids(base, deck, new_grid_ids)
    allowed, aggregates = _part_summaries(base, deck, prepared, new[('ANSAPART', prepared['part_id'])])
    seed_by_id = {r['cons_id']: r for r in boundaries}
    face_centers = []; cons_vectors = []; cons_bounds = []; face_geometry = []
    for key, before in old.items():
        a, b = copy.deepcopy(before), copy.deepcopy(new[key])
        if key == ('ANSAPART', prepared['part_id']):
            for field in allowed:
                a['fields'].pop(field, None); b['fields'].pop(field, None)
        elif key[0] == 'FACE':
            if native._volume_count(a['fields'].pop('Num. Volumes')) != 0 or native._volume_count(b['fields'].pop('Num. Volumes')) != 1:
                raise RuntimeError('FACE volume count mismatch')
            face = features._entity(base, deck, 'FACE', key[1])
            diagonal = new[('ANSAPART', prepared['part_id'])]['fields']['Diagonal']
            face_centers.append({'face_id': key[1], **_check_face_center(a, b,
                base.CalcElementMass(face, no_nsm=False, deck=deck), diagonal)})
            face_geometry.extend({'face_id':key[1],**v} for v in _check_face_geometry_roundoff(a,b,prepared['witness']))
        elif key == ('PSHELL', prepared['source_pid']):
            if native._volume_count(a['fields'].pop('Num.Elem')) != 0 or native._volume_count(b['fields'].pop('Num.Elem')) != len(shell_ids):
                raise RuntimeError('PSHELL element count mismatch')
        elif key[0] == 'CONS':
            r = seed_by_id[key[1]]
            cons_vectors.extend({'cons_id':key[1],**v} for v in _check_cons_vector_roundoff(a,b))
            cons_bounds.extend({'cons_id':key[1],**v} for v in _check_cons_bounds_roundoff(a,b,[p['xyz'] for p in r['hot_points']]))
            if b['fields'].get('Num. of Nodes') != r['point_count']-2:
                raise RuntimeError('CONS subdivision count mismatch')
            length = math.dist(r['hot_points'][0]['xyz'], r['hot_points'][1]['xyz'])/r['segments']
            if not math.isclose(features._number(b['fields'].get('Elem.Length'), 'CONS length', True), length, rel_tol=1e-9):
                raise RuntimeError('CONS element length mismatch')
            for field in ('Num. of Nodes', 'Elem.Length'):
                a['fields'].pop(field); b['fields'].pop(field)
        if a != b:
            raise RuntimeError('MapBlock changed protected original entity '+str(key))
    if prepared['before']['cad_sha256'] != after['cad_sha256'] or any(r != next((n for n in after['grids'] if n['id'] == r['id']), None) for r in prepared['before']['grids']):
        raise RuntimeError('MapBlock changed protected CAD or existing GRID cards')
    guards._same_context(state, deck, prepared['context'])
    return {'new_volume_ids': [volume_id], 'created_shell_ids': shell_ids, 'new_grid_ids': new_grid_ids,
            'after_scope_sha256': features._hash(after), 'native_mapping_status': v['native_mapping_status'],
            'face_lattices': lattice, 'native_part_aggregates': aggregates,
            'face_center_readback': face_centers,
            'cons_vector_roundoff': cons_vectors,
            'cons_bounds_roundoff': cons_bounds,
            'face_geometry_roundoff': face_geometry,
            'property_id': clean['property_id'], 'solid_mesh_generated': False,
            'continuous_export_domain_verified': True, 'exact_native_CAD_equivalence_proven': False,
            'project_quality_acceptance': 'not_evaluated'}


def apply_prepared(state, base, mesh, constants, params, prepared):
    from . import meshing_context
    clean = normalize(params, True)
    result = {'ok': False, 'scope_verified': False, 'api_called': False, 'possible_effects': False,
              'plan_sha256': prepared['plan_sha256'], 'new_volume_ids': [], 'created_shell_ids': [], 'new_grid_ids': []}
    try:
        if clean['expected_plan_sha256'] != prepared['plan_sha256']:
            raise ValueError('MapBlock plan changed')
        if {k: clean[k] for k in _PARAMS} != prepared['clean']:
            raise ValueError('MapBlock apply parameters differ from the prepared plan')
        backup_args = native._backup_preflight(base, clean)
        result['possible_effects'] = True
        result['backup'] = native._verify_backup(base, constants, *backup_args)
        refreshed = preflight(state, base, mesh, constants, prepared['clean'])
        if refreshed['plan_sha256'] != prepared['plan_sha256']:
            raise RuntimeError('model/settings changed during backup; MapBlock not called')
        prepared = refreshed; prepared['state'] = state
        def callback():
            guards._same_context(state, clean['deck'], prepared['context'])
            result['api_called'] = True
            returned = mesh.MapBlock([features._entity(base, clean['deck'], 'FACE', i) for i in clean['face_ids']])
            return _bind_assets(base, clean['deck'], prepared, returned, result)
        volume_id = meshing_context.run_with_context(state, base, mesh, clean['target_length'], callback, result,
                            expected_snapshot_sha256=prepared['settings']['snapshot_sha256'])
        result.update(readback(state, base, mesh, constants, prepared, volume_id))
        result.update(ok=True, scope_verified=True, possible_effects=False, recovery_required=False)
    except Exception as exc:
        result.update(error=type(exc).__name__+': '+str(exc), recovery_required=result['possible_effects'])
        if getattr(exc, 'diagnostics', None):
            result['diagnostics'] = exc.diagnostics
        try:
            after = cad_map._snapshot(state, base, clean['deck'])
            added = native._scope_map(after).keys()-native._scope_map(prepared['before']).keys()
            for kind, field in (('VOLUME', 'new_volume_ids'), ('SHELL', 'created_shell_ids'), ('GRID', 'new_grid_ids')):
                result[field] = sorted(i for k, i in added if k == kind)
        except Exception as read_error:
            result['observation_error'] = str(read_error)
    return result
