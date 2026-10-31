"""Controlled native filling of an existing, unmeshed six-face Map Block."""
import math

from . import cad_map, cons_seed as seeds, geometry_features as features, inspection
from . import map_block_inspection, meshing_context, part_aggregates
from . import structured_native as native, volume_meshing as guards
from .cad_sweep_candidates import discover
from .cad_sweep_geometry import capture as capture_geometry
from .cad_sweep_topology import plan_translation
from .mapped_surface_topology import four_sides
from .structured_batch import source_subset

_PARAMS = ('deck', 'volume_id')


def normalize(params, applying=False):
    if 'annular_options' in params:
        from . import annular_map_fill
        return annular_map_fill.normalize(params, applying)
    if 'volume_ids' in params:
        from . import map_block_batch
        return map_block_batch.normalize(params, applying)
    inspection._params(params, _PARAMS+(('expected_plan_sha256', 'backup_path') if applying else ()))
    result = {k: native._positive_int(params[k], k) for k in _PARAMS}
    if applying:
        result.update(expected_plan_sha256=native._validate_digest(params['expected_plan_sha256'], 'expected_plan_sha256'),
                      backup_path=params['backup_path'])
    return result


def _topology(block):
    volume = block['volumes'][0]; caps = volume['caps']
    if not (volume['caps_match_complete_boundary'] and len(caps['master']) == 1
            and len(caps['slave']) == 1 and len(caps['round']) == 4):
        raise ValueError('MapBlock requires one master, one slave and four distinct side FACEs')
    master, slave = caps['master'][0], caps['slave'][0]
    witness = block['cad_witness']
    components = discover(witness, 1.)['components']
    if len(components) != 1:
        raise ValueError('MapBlock boundary must form one complete six-face component')
    choices = [row['map_options'] for row in components[0].get('alternatives', [])
               if {row['map_options']['source_face_id'], row['map_options']['target_face_id']} == {master, slave}]
    if len(choices) != 1:
        raise ValueError('native MapBlock caps do not form a finite straight sweep')
    chosen = choices[0]
    direction = chosen['direction'] if chosen['source_face_id'] == master else [-x for x in chosen['direction']]
    topology = plan_translation(witness, master, slave, direction, chosen['distance'], cad_map.TOLERANCE)
    if sorted(topology['side_face_ids']) != caps['round']:
        raise ValueError('native MapBlock side FACEs differ from the proved sweep')
    return topology, master, slave


def _boundary(base, constants, deck, block, seed, topology, master, slave, *, boundary_snapshot=None):
    if not all(row['face_mesh_membership_complete'] for row in block['volumes']):
        raise ValueError('MapBlock FACE/SHELL attribution is incomplete')
    members = block['face_membership']
    shell_ids = sorted({i for row in members for i in row['macro_shell_ids']})
    if not shell_ids or len(shell_ids) > 400 or sum(len(r['macro_shell_ids']) for r in members) != len(shell_ids):
        raise ValueError('MapBlock requires 1..400 uniquely attributed boundary SHELLs')
    boundary = (native.capture_source(base, constants, deck, shell_ids) if boundary_snapshot is None
                else source_subset(boundary_snapshot, shell_ids))
    boundary_index = cad_map._SpatialIndex(cad_map.TOLERANCE)
    for node in boundary['nodes']:
        boundary_index.add_unique(tuple(node['xyz']), node['id'], 'complete MapBlock boundary nodes')
    if any(row['type'] != 'CQUAD4' for row in boundary['elements']):
        raise ValueError('first MapBlock fill contract requires a complete CQUAD4 boundary')
    cons = {r['cons_id']: r for r in seed['boundaries']}
    face_cons = {r['face_id']: r['cons_ids'] for r in seed['geometry_witness']['face_boundaries']}
    lattices = []
    for face in members:
        sides = four_sides(face_cons[face['face_id']], seed['boundaries'])
        for side in sides:
            points = {p['id']: p['xyz'] for p in cons[side['cons_id']]['hot_points']}
            side.update(start_xyz=points[side['start']], end_xyz=points[side['end']])
        subset = source_subset(boundary, face['macro_shell_ids'])
        lattices.append({'face_id': face['face_id'], 'lattice': cad_map._face_lattice(subset, sides)})
    counts = {ident: row['segments'] for ident, row in cons.items()}
    longitudinal = {counts[ident] for row in topology['side_pairs'] for ident in row['connector_cons_ids']}
    if len(longitudinal) != 1 or type(next(iter(longitudinal))) is not int or not 1 <= next(iter(longitudinal)) <= 100:
        raise ValueError('longitudinal CONS require a common integer count in 1..100')
    steps = next(iter(longitudinal))
    if any(counts[row['source_cons_id']] != counts[row['target_cons_id']] for row in topology['side_pairs']):
        raise ValueError('master/slave CONS counts differ')
    source = source_subset(boundary, next(row['macro_shell_ids'] for row in members if row['face_id'] == master))
    target = source_subset(boundary, next(row['macro_shell_ids'] for row in members if row['face_id'] == slave))
    index = cad_map._SpatialIndex(cad_map.TOLERANCE)
    for row in target['nodes']:
        index.add_unique(tuple(row['xyz']), row['id'], 'MapBlock target nodes')
    matched = set()
    for row in source['nodes']:
        xyz = tuple(row['xyz'][i]+topology['direction'][i]*topology['distance'] for i in range(3))
        matches = index.matches(xyz)
        if len(matches) != 1:
            raise ValueError('master/slave grids do not translate one-to-one')
        matched.add(matches[0][0])
    if matched != {row['id'] for row in target['nodes']}:
        raise ValueError('target CAP has unmatched nodes')
    predicted_solids = len(source['elements'])*steps
    predicted_nodes = len(source['nodes'])*(steps+1)
    if predicted_solids > 5000 or predicted_nodes > 10000:
        raise ValueError('MapBlock fill exceeds 5000 SOLID/10000 node budget')
    return {'boundary': boundary, 'source': source, 'target': target, 'lattices': lattices,
            'steps': steps, 'predicted_solids': predicted_solids,
            'predicted_new_grids': predicted_nodes-len(boundary['nodes'])}


def preflight(state, base, mesh, constants, params):
    if 'annular_options' in params:
        from . import annular_map_fill
        return annular_map_fill.preflight(state, base, mesh, constants, params)
    if 'volume_ids' in params:
        from . import map_block_batch
        return map_block_batch.preflight(state, base, mesh, constants, params)
    state.assert_main(); clean = normalize({k: params[k] for k in _PARAMS})
    deck = inspection._deck(base, clean); native._check_nastran(constants, deck)
    if not callable(getattr(mesh, 'VolumesRemesh', None)):
        raise NotImplementedError('native VolumesRemesh is required')
    context = guards._execution_context(state, deck)
    if seeds._collect(base, deck, None, 'SOLID') or seeds._collect(base, deck, None, 'CURVE'):
        raise ValueError('first MapBlock fill contract excludes existing SOLID/CURVE')
    if [e._id for e in seeds._collect(base, deck, None, 'VOLUME')] != [clean['volume_id']]:
        raise ValueError('first MapBlock fill contract requires the single explicit VOLUME')
    block = map_block_inspection.capture(state, base, mesh, constants, deck, [clean['volume_id']])
    volume = block['volumes'][0]; fields = volume['card']['fields']
    if volume['native_mapping_status'] != 'native_ready' or fields.get('Status') != 'Unmeshed' or fields.get('Frozen') != 'NO':
        raise ValueError('MapBlock must be ready, unmeshed and unfrozen')
    if len(volume['face_ids']) != 6 or sorted(e._id for e in seeds._collect(base, deck, None, 'FACE')) != volume['face_ids']:
        raise ValueError('first MapBlock fill contract requires the complete six-FACE model')
    if sorted(e._id for e in seeds._collect(base, deck, None, 'SHELL')) != volume['surface_shell_ids']:
        raise ValueError('all existing SHELLs must belong to the six-FACE boundary')
    parts = seeds._collect(base, deck, None, 'ANSAPART')
    if len(parts) != 1 or parts[0]._id != volume['part_id']:
        raise ValueError('first MapBlock fill contract requires one complete source Part')
    topology, master, slave = _topology(block)
    geometry = capture_geometry(state, base, deck, block['cad_witness'], topology)
    if geometry['status'] != 'verified_at_export_precision':
        raise ValueError('finite continuous MapBlock CAD export domain is unsupported')
    seed = seeds.capture(state, base, deck, volume['face_ids'], [r['id'] for r in block['cad_witness']['boundaries']])
    if seed['geometry_witness']['source_snapshot_sha256'] != block['cad_witness']['source_snapshot_sha256']:
        raise RuntimeError('MapBlock CAD source changed during preflight')
    if len(seed['boundaries']) != 12 or any(row['status'] != 'supported' or row['native_fields']['Frozen'] != 'NO'
            or row['native_fields']['Biasing'] != 'LINEAR' for row in seed['boundaries']):
        raise ValueError('MapBlock requires twelve unfrozen, linear first-order CONS')
    boundary = _boundary(base, constants, deck, block, seed, topology, master, slave)
    before = cad_map._snapshot(state, base, deck); rows = native._scope_map(before)
    cad_map._reserve_budget(before, boundary['predicted_solids'], boundary['predicted_new_grids'])
    part = parts[0]; part_row = rows[('ANSAPART', part._id)]
    if part_row['fields'].get('Freeze') != 'NO' or part_row['fields'].get('Is Group') != 'NO':
        raise ValueError('source Part must be unfrozen and not a group')
    if any(rows[('FACE', i)]['assignments']['__part__'] != part._id
            or rows[('FACE', i)]['fields'].get('Frozen') != 'NO'
            or native._volume_count(rows[('FACE', i)]['fields'].get('Num. Volumes')) != 1 for i in volume['face_ids']):
        raise ValueError('source FACE ownership/freeze/volume counts differ')
    source_pids = {rows[('FACE', i)]['assignments']['__prop__'] for i in volume['face_ids']}
    if len(source_pids) != 1:
        raise ValueError('first fill contract requires one source PSHELL')
    prop_id = native._positive_int(fields.get('PID'), 'MapBlock PID')
    _, prop = native._psolid_preflight(base, deck, prop_id)
    density = features._number(prop['mat1']['fields'].get('RHO'), 'MapBlock MAT1 density')
    if density <= 0:
        raise ValueError('first MapBlock fill contract requires positive MAT1 density')
    if native._volume_count(rows[('PSOLID', prop_id)]['fields'].get('Num.Elem')) != 0:
        raise ValueError('target PSOLID must have explicitly zero SOLID count')
    for key in ('Hexas', 'Pentas', 'Tetras', 'Pyramids', 'Polyhedrals', 'Grids', 'Internal grids'):
        if native._volume_count(fields.get(key)) != 0:
            raise ValueError('unmeshed MapBlock '+key+' must be zero')
    if rows[('VOLUME', clean['volume_id'])].get('solid_ids') != []:
        raise ValueError('MapBlock already has SOLID members')
    cad_map._close_number(fields.get('Volume'), topology['expected_volume'], 'MapBlock volume')
    expected_mass = topology['expected_volume']*density
    cad_map._close_number(fields.get('Mass'), expected_mass, 'MapBlock mass')
    part_aggregates.capture(base, deck, part, part_row)
    lengths = [features._number(rows[('CONS', row['cons_id'])]['fields'].get('Elem.Length'), 'CONS target length', True) for row in seed['boundaries']]
    if any(not math.isclose(value, lengths[0], rel_tol=1e-9) for value in lengths):
        raise ValueError('first fill contract requires a common boundary target length')
    settings = meshing_context.capture(state, base, mesh)
    guards._same_context(state, deck, context)
    body = {'params': clean, 'context': context, 'scope_sha256': features._hash(before),
            'block_sha256': block['source_snapshot_sha256'], 'geometry': geometry,
            'seed_sha256': seed['seed_snapshot_sha256'], 'meshing_context_sha256': settings['snapshot_sha256'],
            'source_sha256': boundary['source']['snapshot_sha256'], 'steps': boundary['steps']}
    return {'clean': clean, 'context': context, 'before': before, 'block': block, 'geometry': geometry,
            'topology': topology, 'seed': seed, 'part': part, 'property_id': prop_id,
            'expected_mass': expected_mass, 'settings': settings, 'target_length': lengths[0],
            **boundary, 'plan_sha256': features._hash(body)}


def public_plan(prepared):
    if prepared.get('annular'):
        from . import annular_map_fill
        return annular_map_fill.public_plan(prepared)
    if prepared.get('batch'):
        from . import map_block_batch
        return map_block_batch.public_plan(prepared)
    volume = prepared['block']['volumes'][0]
    return {'status': 'ready_for_controlled_apply', 'params': prepared['clean'],
            'plan_sha256': prepared['plan_sha256'], 'scope_sha256': features._hash(prepared['before']),
            'meshing_context_sha256': prepared['settings']['snapshot_sha256'],
            'geometry': prepared['geometry'], 'caps': volume['caps'], 'steps': prepared['steps'],
            'predicted_solids': prepared['predicted_solids'], 'predicted_new_grids': prepared['predicted_new_grids'],
            'property_id': prepared['property_id'], 'usable_for_mutation': False}


def validate_report(state, base, mesh, constants, deck, result):
    current = preflight(state, base, mesh, constants, result['params'])
    if current['plan_sha256'] != result.get('plan_sha256'):
        raise ValueError('MapBlock fill plan is stale')


def readback(state, base, mesh, constants, prepared):
    from ansa import calc
    from . import map_block_fill_audit, mesh_audit, structured_boundary
    clean = prepared['clean']; deck = clean['deck']; volume_id = clean['volume_id']
    guards._same_context(state, deck, prepared['context'])
    after = cad_map._snapshot(state, base, deck)
    old, new = native._scope_map(prepared['before']), native._scope_map(after)
    solids = sorted(i for kind, i in new.keys()-old.keys() if kind == 'SOLID')
    output = native.capture_mesh(base, constants, deck, solids)
    verification = cad_map.verify_translation(prepared['source'], output, prepared['topology']['direction'],
                        prepared['topology']['distance'], prepared['steps'], cad_map.TOLERANCE)
    cad_map._close_number(verification['total_volume'], prepared['topology']['expected_volume'], 'filled total volume')
    target_nodes = {row['id'] for row in prepared['target']['nodes']}
    if {row['output_node_id'] for row in verification['node_layer_pairs'] if row['layer'] == prepared['steps']} != target_nodes:
        raise RuntimeError('filled last layer does not retain the existing target CAP node IDs')
    aggregate = part_aggregates.capture(base, deck, prepared['part'], new[('ANSAPART', prepared['part']._id)])
    checked = map_block_fill_audit.audit_changes(prepared['before'], after, volume_id=volume_id,
                part_id=prepared['part']._id, property_id=prepared['property_id'], solid_ids=solids,
                output=output, part_aggregate=aggregate, expected_volume=prepared['topology']['expected_volume'],
                expected_mass=prepared['expected_mass'])
    block = map_block_inspection.capture(state, base, mesh, constants, deck, [volume_id])
    if (block['volumes'][0]['caps'] != prepared['block']['volumes'][0]['caps']
            or block['face_membership'] != prepared['block']['face_membership']
            or block['surface_mesh'] != prepared['block']['surface_mesh']):
        raise RuntimeError('fill changed original CAP/side membership or boundary mesh')
    cad = structured_boundary.capture(base, mesh, calc, constants, deck, output,
            {'face_ids': prepared['block']['volumes'][0]['face_ids'], 'tolerance': cad_map.TOLERANCE, 'cylinders': []})
    if cad['membership']['status'] != 'pass' or cad['projection']['status'] != 'pass':
        raise RuntimeError('filled CAD boundary attribution/projection failed')
    # This proved single planar block has no inter-block interfaces or protected
    # cylinders. Explicit empty sets are different from unspecified coverage.
    topology_audit = mesh_audit.audit_snapshot(cad['audit_snapshot'], expected_boundary=cad['expected_boundary'],
                        expected_components=1, interfaces=[], protected_cylinders=[])
    if topology_audit['topology_status'] != 'pass' or topology_audit['geometry_status'] != 'pass':
        error = RuntimeError('filled boundary/topology audit failed')
        error.diagnostics = {'mesh_audit': {k: topology_audit[k] for k in
                            ('topology_status', 'geometry_status', 'summary', 'diagnostics')}}
        raise error
    guards._same_context(state, deck, prepared['context'])
    return {**checked, 'verification': verification, 'native_part_aggregates': aggregate,
            'cad_boundary': {k: v for k, v in cad.items() if k != 'audit_snapshot'},
            'mesh_audit': topology_audit, 'continuous_export_domain_verified': True,
            'exact_native_CAD_equivalence_proven': False, 'project_quality_acceptance': 'not_evaluated',
            'native_volume_id': volume_id, 'solid_mesh_generated': True}


def apply_prepared(state, base, mesh, constants, params, prepared):
    if prepared.get('annular'):
        from . import annular_map_fill
        return annular_map_fill.apply_prepared(state, base, mesh, constants, params, prepared)
    if prepared.get('batch'):
        from . import map_block_batch
        return map_block_batch.apply_prepared(state, base, mesh, constants, params, prepared)
    clean = normalize(params, True)
    result = {'ok': False, 'scope_verified': False, 'api_called': False, 'possible_effects': False,
              'plan_sha256': prepared['plan_sha256'], 'new_solid_ids': [], 'new_grid_ids': [],
              'filled_volume_id': clean['volume_id']}
    try:
        if clean['expected_plan_sha256'] != prepared['plan_sha256'] or {k: clean[k] for k in _PARAMS} != prepared['clean']:
            raise ValueError('MapBlock fill plan/parameters changed')
        backup_args = native._backup_preflight(base, clean)
        result['possible_effects'] = True
        result['backup'] = native._verify_backup(base, constants, *backup_args)
        refreshed = preflight(state, base, mesh, constants, prepared['clean'])
        if refreshed['plan_sha256'] != prepared['plan_sha256']:
            raise RuntimeError('model/settings changed during backup; VolumesRemesh not called')
        prepared = refreshed
        def callback():
            guards._same_context(state, clean['deck'], prepared['context'])
            result['api_called'] = True
            rc = mesh.VolumesRemesh([features._entity(base, clean['deck'], 'VOLUME', clean['volume_id'])])
            result['native_return'] = rc if type(rc) is int else repr(rc)
            if type(rc) is not int or rc != 1:
                raise RuntimeError('VolumesRemesh did not return success')
        meshing_context.run_with_context(state, base, mesh, prepared['target_length'], callback, result,
                    expected_snapshot_sha256=prepared['settings']['snapshot_sha256'])
        result.update(readback(state, base, mesh, constants, prepared))
        result.update(ok=True, scope_verified=True, possible_effects=False, recovery_required=False)
    except Exception as exc:
        result.update(error=type(exc).__name__+': '+str(exc), recovery_required=result['possible_effects'])
        if getattr(exc, 'diagnostics', None):
            result['diagnostics'] = exc.diagnostics
        try:
            guards._same_context(state, clean['deck'], prepared['context'])
            after = cad_map._snapshot(state, base, clean['deck'])
            added = native._scope_map(after).keys()-native._scope_map(prepared['before']).keys()
            result.update(new_solid_ids=sorted(i for k, i in added if k == 'SOLID'),
                          new_grid_ids=sorted(i for k, i in added if k == 'GRID'))
        except Exception as read_error:
            result['observation_error'] = str(read_error)
    return result
