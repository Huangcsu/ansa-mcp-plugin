"""Bounded native multi-block fill of a complete rectangular CAD assembly.

All boundary nodes already exist. Global CAD, seeds, cards and settings bind the
plan; a batch uses one VolumesRemesh call. Existing filled blocks are immutable.
"""
import copy
import itertools
import math

from . import cad_map, cad_sweep_candidates as candidates, cons_seed as seeds
from . import geometry_features as features, inspection, map_block_inspection
from . import meshing_context, part_aggregates, structured_native as native
from . import volume_meshing as guards
from .cad_sweep_geometry import capture_many as capture_geometry_many
from .structured_batch import source_subset

_PARAMS = ('deck', 'volume_ids')


def normalize(params, applying=False):
    inspection._params(params, _PARAMS+(('expected_plan_sha256', 'backup_path') if applying else ()))
    clean = {'deck': native._positive_int(params['deck'], 'deck'),
             'volume_ids': sorted(native._id_list(params['volume_ids'], 'volume_ids', 20))}
    if applying:
        clean.update(expected_plan_sha256=native._validate_digest(params['expected_plan_sha256'], 'expected_plan_sha256'), backup_path=params['backup_path'])
    return clean


def local_witness(parent, face_ids):
    """Derive a closed volume boundary; keep the complete parent hash/adjacency.

    This differs from candidate subset_witness: a native block may be part of a
    larger CONS-connected assembly, with three FACEs incident to an interface
    edge. Only the topology solver's local adjacency is filtered.
    """
    parsed = candidates._validate_witness(parent)
    ids = native._id_list(face_ids, 'block face_ids', 6)
    selected = set(ids)
    if len(ids) != 6 or not selected <= set(parsed['face_ids']):
        raise ValueError('each block requires six FACEs present in the parent witness')
    result = copy.deepcopy(parent)
    result['source_scope'] = {'face_ids': sorted(ids), 'curve_ids': []}
    result['parts'] = [r for r in result['parts'] if r['id'] in selected]
    result['face_boundaries'] = [r for r in result['face_boundaries'] if r['face_id'] in selected]
    cons = {i for r in result['face_boundaries'] for i in r['cons_ids']}
    result['boundaries'] = [r for r in result['boundaries'] if r['id'] in cons]
    outside = {'cons_faces': {}, 'vertex_cons': {}}
    for row in result['boundaries']:
        outside['cons_faces'][str(row['id'])] = list(row['face_ids'])
        row['face_ids'] = sorted(set(row['face_ids']) & selected)
        if len(row['face_ids']) != 2:
            raise ValueError('selected block boundary is not locally closed')
    points = {i for r in result['boundaries'] for i in r['endpoint_topology']['hot_point_ids']}
    result['vertices'] = [r for r in result['vertices'] if r['id'] in points]
    for row in result['vertices']:
        outside['vertex_cons'][str(row['id'])] = list(row['cons_ids'])
        row['cons_ids'] = sorted(set(row['cons_ids']) & cons)
    if len(cons) != 12 or len(points) != 8:
        raise ValueError('block requires twelve CONS and eight native HOT POINTs')
    result['parent_source_snapshot_sha256'] = parent['source_snapshot_sha256']
    result['native_parent_adjacency'] = outside
    result['derived_topology_only'] = True
    result['source_snapshot_sha256'] = features._hash({k: result[k] for k in candidates._SOURCE_FIELDS})
    candidates._validate_witness(result)
    return result


def rectangular_bounds(witness):
    points = [r['position'] for r in witness['vertices']]
    low = [min(p[a] for p in points) for a in range(3)]
    high = [max(p[a] for p in points) for a in range(3)]
    if any(high[a]-low[a] <= cad_map.TOLERANCE for a in range(3)):
        raise ValueError('rectangular block has a degenerate dimension')
    corners = list(itertools.product(*zip(low, high)))
    matches = [[i for i, c in enumerate(corners) if math.dist(p, c) <= cad_map.TOLERANCE] for p in points]
    if any(len(m) != 1 for m in matches) or len({m[0] for m in matches}) != 8:
        raise ValueError('multi-block template requires axis-aligned rectangular CAD blocks')
    return {'min': low, 'max': high}


def assembly_layout(block, local):
    owners = {}
    for volume in block['volumes']:
        for face_id in volume['face_ids']:
            owners.setdefault(face_id, []).append(volume['volume_id'])
    if any(len(ids) not in (1, 2) for ids in owners.values()):
        raise ValueError('CAD FACE must have one exterior or two interface owners')
    bounds = {ident: rectangular_bounds(w) for ident, w in local.items()}
    for first, second in itertools.combinations(sorted(bounds), 2):
        a, b = bounds[first], bounds[second]
        overlap = [min(a['max'][k], b['max'][k])-max(a['min'][k], b['min'][k]) for k in range(3)]
        if all(x > cad_map.TOLERANCE for x in overlap):
            raise ValueError('rectangular CAD blocks overlap in positive volume')
        touching = all(x >= -cad_map.TOLERANCE for x in overlap) and sum(x > cad_map.TOLERANCE for x in overlap) == 2
        shared = [f for f, ids in owners.items() if set(ids) == {first, second}]
        if touching != bool(shared) or len(shared) > 1:
            raise ValueError('touching blocks must share exactly one complete native FACE')
        if shared:
            # The closed local six-face proof supplies identical native corners;
            # AABB overlap supplies their complete contact region, not a sample.
            w = local[first]
            by_cons = {r['id']: r for r in w['boundaries']}
            face = next(r for r in w['face_boundaries'] if r['face_id'] == shared[0])
            pids = {p for c in face['cons_ids'] for p in by_cons[c]['endpoint_topology']['hot_point_ids']}
            pts = [r['position'] for r in w['vertices'] if r['id'] in pids]
            for k in range(3):
                expected = [max(a['min'][k], b['min'][k]), min(a['max'][k], b['max'][k])]
                if abs(min(p[k] for p in pts)-expected[0]) > cad_map.TOLERANCE or abs(max(p[k] for p in pts)-expected[1]) > cad_map.TOLERANCE:
                    raise ValueError('shared FACE does not cover the complete rectangular contact')
    # Judge the complete union, not each diagonal pair: three/four blocks may
    # meet around one edge while their combined exterior remains manifold.
    outer = {f for f, ids in owners.items() if len(ids) == 1}
    faces = {r['face_id']: r for w in local.values() for r in w['face_boundaries']}
    edges = {r['id']: r for w in local.values() for r in w['boundaries']}
    vertices = {r['id']: r for w in local.values() for r in w['vertices']}
    point_index = cad_map._SpatialIndex(cad_map.TOLERANCE)
    for ident, row in vertices.items():
        point_index.add_unique(tuple(row['position']), ident, 'complete CAD assembly HOT POINTs')
    uses = {}
    for f in outer:
        for edge in faces[f]['cons_ids']:uses.setdefault(edge, set()).add(f)
    if any(len(fs) != 2 for fs in uses.values()):
        raise ValueError('complete assembly exterior is non-manifold along a native CAD edge')
    links = {}
    for edge, fs in uses.items():
        for point in edges[edge]['endpoint_topology']['hot_point_ids']:
            links.setdefault(point, []).append(fs)
    for pairs in links.values():
        unseen = set().union(*pairs); todo = [unseen.pop()]
        while todo:
            face = todo.pop()
            adjacent = set().union(*(fs for fs in pairs if face in fs))
            found = adjacent & unseen; unseen -= found; todo.extend(found)
        if unseen:
            raise ValueError('complete assembly exterior is non-manifold at a native CAD vertex')
    return {'face_owners': owners, 'bounds': bounds,
            'outer_face_ids': sorted(f for f, ids in owners.items() if len(ids) == 1),
            'shared_face_ids': sorted(f for f, ids in owners.items() if len(ids) == 2),
            'nonoverlap_basis': 'verified finite rectangular CAD domains; pairwise complete AABB intervals'}


def _local_block(block, volume, witness):
    ids = set(volume['face_ids'])
    return {**block, 'volumes': [volume], 'cad_witness': witness,
            'face_membership': [r for r in block['face_membership'] if r['face_id'] in ids]}


def _check_filled(base, constants, deck, row, proof):
    fields = row['fields']; ids = row.get('solid_ids')
    if fields.get('State') != 'Meshed' or fields.get('Status') != 'Meshed' or fields.get('Order') != '1st' or not ids:
        raise ValueError('unselected nonempty block must be a meshed first-order MapBlock')
    output = native.capture_mesh(base, constants, deck, ids)
    _verify_layers(proof, output)
    if (native._volume_count(fields.get('Hexas')) != len(ids)
            or native._volume_count(fields.get('Grids')) != len(output['nodes'])
            or native._volume_count(fields.get('Internal grids')) != len({r['id'] for r in output['nodes']}-{r['id'] for r in proof['boundary']['nodes']})):
        raise ValueError('existing filled block native counts differ from actual members')
    if any(r['type'] != 'CHEXA' or r['block_id'] != 'VOLUME:'+str(proof['volume_id']) or r['property_id'] != proof['property_id'] for r in output['elements']):
        raise ValueError('existing SOLID type/ownership/property mismatch')


def _verify_layers(proof, output):
    t = proof['topology']
    verification = cad_map.verify_translation(proof['source'], output, t['direction'], t['distance'], proof['steps'], cad_map.TOLERANCE)
    cad_map._close_number(verification['total_volume'], t['expected_volume'], 'filled rectangular volume')
    if {r['output_node_id'] for r in verification['node_layer_pairs'] if r['layer'] == proof['steps']} != {r['id'] for r in proof['target']['nodes']}:
        raise RuntimeError('filled last layer changed native target CAP GRID identities')
    return verification


def preflight(state, base, mesh, constants, params):
    from . import map_block_fill as single
    state.assert_main(); clean = normalize({k: params[k] for k in _PARAMS})
    deck = inspection._deck(base, clean); native._check_nastran(constants, deck)
    context = guards._execution_context(state, deck)
    if not callable(getattr(mesh, 'VolumesRemesh', None)):
        raise NotImplementedError('native VolumesRemesh is required')
    if seeds._collect(base, deck, None, 'CURVE'):
        raise ValueError('multi-block template excludes auxiliary CURVEs')
    ids = [v._id for v in seeds._collect(base, deck, None, 'VOLUME')]
    if not ids or len(ids) > 20 or not set(clean['volume_ids']) <= set(ids):
        raise ValueError('select existing MapBlocks from a complete model of at most twenty volumes')
    block = map_block_inspection.capture(state, base, mesh, constants, deck, ids)
    parent = block['cad_witness']; face_ids = sorted({i for v in block['volumes'] for i in v['face_ids']})
    if face_ids != [e._id for e in seeds._collect(base, deck, None, 'FACE')]:
        raise ValueError('every CAD FACE must belong to the explicit native block assembly')
    shells = sorted({i for v in block['volumes'] for i in v['surface_shell_ids']})
    if shells != [e._id for e in seeds._collect(base, deck, None, 'SHELL')] or len(shells) > 400:
        raise ValueError('complete block boundary must cover all SHELLs within the 400-SHELL budget')
    surface = native.capture_source(base, constants, deck, shells)
    surface_index = cad_map._SpatialIndex(cad_map.TOLERANCE)
    for node in surface['nodes']:
        surface_index.add_unique(tuple(node['xyz']), node['id'], 'complete assembly boundary nodes')
    before = cad_map._snapshot(state, base, deck); rows = native._scope_map(before)
    parts = seeds._collect(base, deck, None, 'ANSAPART')
    if len(parts) != 1 or any(v['part_id'] != parts[0]._id for v in block['volumes']):
        raise ValueError('multi-block template requires one complete source Part')
    part = parts[0]; part_row = rows[('ANSAPART', part._id)]
    if part_row['fields'].get('Freeze') != 'NO' or part_row['fields'].get('Is Group') != 'NO':
        raise ValueError('source Part must be unfrozen and not a group')
    local = {v['volume_id']: local_witness(parent, v['face_ids']) for v in block['volumes']}
    layout = assembly_layout(block, local)
    topology_rows = {v['volume_id']: single._topology(_local_block(block,v,local[v['volume_id']])) for v in block['volumes']}
    geometry_rows = capture_geometry_many(state,base,deck,[(local[v['volume_id']],topology_rows[v['volume_id']][0]) for v in block['volumes']])
    geometries = dict(zip(ids,geometry_rows))
    # Read each actual edge once; never collect CONS GRID in a plan/preflight.
    edge_rows = {r['id']: seeds._boundary(base, deck, features._entity(base, deck, 'CONS', r['id'])) for r in parent['boundaries']}
    if any(r['status'] != 'supported' or r['native_fields']['Frozen'] != 'NO' or r['native_fields']['Biasing'] != 'LINEAR' for r in edge_rows.values()):
        raise ValueError('assembly requires unfrozen first-order linear native CONS')
    proofs = []; properties = {}; all_solids = []; lengths = []
    for volume in block['volumes']:
        ident = volume['volume_id']; fields = volume['card']['fields']; witness = local[ident]
        if fields.get('Frozen') != 'NO' or not volume['face_mesh_membership_complete']:
            raise ValueError('every block must be unfrozen with complete FACE/SHELL membership')
        if any(native._volume_count(fields.get(k)) != 0 for k in ('Pentas', 'Tetras', 'Pyramids', 'Polyhedrals')):
            raise ValueError('rectangular template requires pure first-order CHEXA blocks')
        sliced = _local_block(block, volume, witness)
        topology, master, slave = topology_rows[ident]
        geometry = geometries[ident]
        if geometry['status'] != 'verified_at_export_precision':
            raise ValueError('finite continuous block CAD export domain is unsupported: '+str(ident))
        cons_ids = {r['id'] for r in witness['boundaries']}
        seed = {'boundaries': [edge_rows[i] for i in sorted(cons_ids)], 'geometry_witness': witness}
        boundary = single._boundary(base, constants, deck, sliced, seed, topology, master, slave, boundary_snapshot=surface)
        pid = native._positive_int(fields.get('PID'), 'MapBlock PID')
        if pid not in properties:
            _, properties[pid] = native._psolid_preflight(base, deck, pid)
        density = features._number(properties[pid]['mat1']['fields'].get('RHO'), 'MapBlock MAT1 density', True)
        for face in volume['face_ids']:
            r = rows[('FACE', face)]
            if r['assignments']['__part__'] != part._id or r['fields'].get('Frozen') != 'NO' or native._volume_count(r['fields'].get('Num. Volumes')) != len(layout['face_owners'][face]):
                raise ValueError('FACE ownership/freeze/native volume count differs from assembly')
        cad_map._close_number(fields.get('Volume'), topology['expected_volume'], 'native block volume')
        cad_map._close_number(fields.get('Mass'), topology['expected_volume']*density, 'native block mass')
        proof = {'volume_id': ident, 'part_id': part._id, 'property_id': pid, 'topology': topology,
                 'geometry': geometry, 'expected_mass': topology['expected_volume']*density, **boundary}
        member_ids = rows[('VOLUME', ident)].get('solid_ids')
        if member_ids:
            if ident in clean['volume_ids']:
                raise ValueError('selected block is already filled; regeneration is unsupported')
            _check_filled(base, constants, deck, rows[('VOLUME', ident)], proof)
            all_solids.extend(member_ids)
        else:
            if member_ids != [] or fields.get('State') != 'Ready to Mesh' or fields.get('Status') != 'Unmeshed' or fields.get('Order') != '':
                raise ValueError('empty block must be ready and explicitly unmeshed')
            if any(native._volume_count(fields.get(k)) != 0 for k in ('Hexas', 'Grids', 'Internal grids')):
                raise ValueError('empty block native counts must be zero')
        proofs.append(proof)
        lengths.extend(features._number(rows[('CONS', i)]['fields'].get('Elem.Length'), 'CONS element length', True) for i in cons_ids)
    if len(all_solids) != len(set(all_solids)) or sorted(all_solids) != [e._id for e in seeds._collect(base, deck, None, 'SOLID')]:
        raise ValueError('every existing SOLID must have exactly one verified filled block owner')
    for pid in properties:
        old_count = sum(r['fields'].get('PID') == pid for (kind, _), r in rows.items() if kind == 'SOLID')
        if native._volume_count(rows[('PSOLID', pid)]['fields'].get('Num.Elem')) != old_count:
            raise ValueError('PSOLID existing count differs from protected SOLID inventory')
    if any(not math.isclose(x, lengths[0], rel_tol=1e-9) for x in lengths):
        raise ValueError('multi-block template requires one common actual boundary edge length')
    selected = [p for p in proofs if p['volume_id'] in clean['volume_ids']]
    predicted = sum(p['predicted_solids'] for p in selected)
    if predicted+len(all_solids) > 5000:
        raise ValueError('multi-block mesh exceeds 5000 SOLID budget')
    cad_map._reserve_budget(before, predicted, sum(p['predicted_new_grids'] for p in selected))
    source = source_subset(surface, sorted({e['id'] for p in selected for e in p['source']['elements']}))
    part_aggregates.capture(base, deck, part, part_row)
    settings = meshing_context.capture(state, base, mesh)
    guards._same_context(state, deck, context)
    body = {'params': clean, 'context': context, 'scope_sha256': features._hash(before),
            'block_sha256': block['source_snapshot_sha256'], 'proofs': proofs, 'layout': layout,
            'seed_sha256': features._hash(edge_rows), 'settings_sha256': settings['snapshot_sha256']}
    return {'batch': True, 'clean': clean, 'context': context, 'before': before, 'block': block,
            'proofs': proofs, 'layout': layout, 'part': part, 'source': source,
            'settings': settings, 'target_length': lengths[0], 'plan_sha256': features._hash(body),
            'predicted_solids': predicted, 'predicted_new_grids': sum(p['predicted_new_grids'] for p in selected)}


def public_plan(prepared):
    return {'status': 'ready_for_controlled_apply', 'params': prepared['clean'],
            'plan_sha256': prepared['plan_sha256'], 'scope_sha256': features._hash(prepared['before']),
            'meshing_context_sha256': prepared['settings']['snapshot_sha256'],
            'template': 'T2_rectangular_native_MapBlock_assembly', 'layout': prepared['layout'],
            'predicted_solids': prepared['predicted_solids'], 'predicted_new_grids': prepared['predicted_new_grids'],
            'selected_volume_ids': prepared['clean']['volume_ids'], 'usable_for_mutation': False,
            'execution': {'native_fill_calls': 1, 'boundary_source_captures': 1, 'cons_grid_collection_calls': 0},
            'blocks': [{'volume_id': p['volume_id'], 'geometry': p['geometry'], 'steps': p['steps'], 'predicted_solids': p['predicted_solids']} for p in prepared['proofs']]}


def _active_audit(state, base, mesh, constants, prepared, after, *, tolerance=cad_map.TOLERANCE):
    from ansa import calc
    from . import mesh_audit, structured_boundary
    deck = prepared['clean']['deck']; rows = native._scope_map(after)
    active = {p['volume_id'] for p in prepared['proofs'] if rows[('VOLUME', p['volume_id'])].get('solid_ids')}
    solid_ids = sorted(i for ident in active for i in rows[('VOLUME', ident)]['solid_ids'])
    output = native.capture_mesh(base, constants, deck, solid_ids)
    owners = {f: sorted(set(ids) & active) for f, ids in prepared['layout']['face_owners'].items()}
    outer = sorted(f for f, ids in owners.items() if len(ids) == 1)
    cad = structured_boundary.capture(base, mesh, calc, constants, deck, output, {'face_ids': outer, 'tolerance': tolerance, 'cylinders': []})
    if cad['membership']['status'] != 'pass' or cad['projection']['status'] != 'pass':
        raise RuntimeError('active assembly CAD boundary attribution/projection failed')
    members = {r['face_id']: r for r in prepared['block']['face_membership']}
    interfaces = [{'id': 'FACE:'+str(f), 'blocks': ['VOLUME:'+str(i) for i in ids],
                   'facets': [s['nodes'] for s in members[f]['shells']]} for f, ids in sorted(owners.items()) if len(ids) == 2]
    pending = set(active); components = 0
    while pending:
        todo = [pending.pop()]; components += 1
        while todo:
            ident = todo.pop()
            adjacent = {i for ids in owners.values() if len(ids) == 2 and ident in ids for i in ids}
            found = pending & adjacent; pending -= found; todo.extend(found)
    audit = mesh_audit.audit_snapshot(cad['audit_snapshot'], expected_boundary=cad['expected_boundary'], expected_components=components, interfaces=interfaces, protected_cylinders=[])
    if audit['topology_status'] != 'pass' or audit['geometry_status'] != 'pass':
        error = RuntimeError('active assembly complete boundary/interface audit failed')
        error.diagnostics = {'mesh_audit': audit}; raise error
    return {'active_volume_ids': sorted(active), 'all_blocks_filled': len(active) == len(prepared['proofs']),
            'cad_boundary': {k: v for k, v in cad.items() if k != 'audit_snapshot'}, 'mesh_audit': audit,
            'interfaces': interfaces, 'expected_components': components}


def readback(state, base, mesh, constants, prepared):
    from . import map_block_fill_audit
    deck = prepared['clean']['deck']; guards._same_context(state, deck, prepared['context'])
    after = cad_map._snapshot(state, base, deck); rows = native._scope_map(after)
    blocks = []; matches = []; details = []
    for p in prepared['proofs']:
        if p['volume_id'] not in prepared['clean']['volume_ids']:
            continue
        output = native.capture_mesh(base, constants, deck, rows[('VOLUME', p['volume_id'])]['solid_ids'])
        verification = _verify_layers(p, output); matches.extend(verification['source_cell_matches'])
        details.append({'volume_id': p['volume_id'], 'verification': verification})
        blocks.append({k: p[k] for k in ('volume_id', 'part_id', 'property_id', 'expected_mass')} | {'solid_ids': [r['id'] for r in output['elements']], 'output': output, 'expected_volume': p['topology']['expected_volume']})
    aggregate = part_aggregates.capture(base, deck, prepared['part'], rows[('ANSAPART', prepared['part']._id)])
    checked = map_block_fill_audit.audit_many_changes(prepared['before'], after, blocks=blocks, part_aggregate=aggregate)
    current = map_block_inspection.capture(state, base, mesh, constants, deck, prepared['block']['volume_ids'])
    if (current['face_membership'] != prepared['block']['face_membership'] or current['surface_mesh'] != prepared['block']['surface_mesh']
            or [v['caps'] for v in current['volumes']] != [v['caps'] for v in prepared['block']['volumes']]):
        raise RuntimeError('batch fill changed protected CAP/side membership or surface mesh')
    audit = _active_audit(state, base, mesh, constants, prepared, after)
    guards._same_context(state, deck, prepared['context'])
    return {**checked, **audit, 'verification': {'source_cell_matches': matches, 'blocks': details},
            'native_part_aggregates': aggregate, 'filled_volume_ids': prepared['clean']['volume_ids'],
            'continuous_export_domain_verified': True, 'exact_native_CAD_equivalence_proven': False,
            'solid_mesh_generated': True, 'project_quality_acceptance': 'not_evaluated'}


def apply_prepared(state, base, mesh, constants, params, prepared):
    clean = normalize(params, True)
    result = {'ok': False, 'scope_verified': False, 'api_called': False, 'possible_effects': False,
              'plan_sha256': prepared['plan_sha256'], 'new_solid_ids': [], 'new_grid_ids': [], 'filled_volume_ids': clean['volume_ids']}
    try:
        if clean['expected_plan_sha256'] != prepared['plan_sha256'] or {k: clean[k] for k in _PARAMS} != prepared['clean']:
            raise ValueError('multi-block fill plan/parameters changed')
        backup_args = native._backup_preflight(base, clean); result['possible_effects'] = True
        result['backup'] = native._verify_backup(base, constants, *backup_args)
        refreshed = preflight(state, base, mesh, constants, prepared['clean'])
        if refreshed['plan_sha256'] != prepared['plan_sha256']:
            raise RuntimeError('model/settings changed during backup; VolumesRemesh not called')
        prepared = refreshed
        def callback():
            guards._same_context(state, clean['deck'], prepared['context']); result['api_called'] = True
            rc = mesh.VolumesRemesh([features._entity(base, clean['deck'], 'VOLUME', i) for i in clean['volume_ids']])
            result['native_return'] = rc if type(rc) is int else repr(rc)
            if type(rc) is not int or rc != 1:
                raise RuntimeError('VolumesRemesh did not return success')
        meshing_context.run_with_context(state, base, mesh, prepared['target_length'], callback, result, expected_snapshot_sha256=prepared['settings']['snapshot_sha256'])
        result.update(readback(state, base, mesh, constants, prepared))
        result.update(ok=True, scope_verified=True, possible_effects=False, recovery_required=False)
    except Exception as exc:
        result.update(error=type(exc).__name__+': '+str(exc), recovery_required=result['possible_effects'])
        if getattr(exc, 'diagnostics', None):result['diagnostics'] = exc.diagnostics
        try:
            guards._same_context(state, clean['deck'], prepared['context'])
            after = cad_map._snapshot(state, base, clean['deck'])
            added = native._scope_map(after).keys()-native._scope_map(prepared['before']).keys()
            result.update(new_solid_ids=sorted(i for k, i in added if k == 'SOLID'), new_grid_ids=sorted(i for k, i in added if k == 'GRID'))
        except Exception as read_error:result['observation_error'] = str(read_error)
    return result
