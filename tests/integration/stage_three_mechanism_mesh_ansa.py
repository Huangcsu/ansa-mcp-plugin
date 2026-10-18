"""Owned ABAQUS quad/HEX8 and kinematic interface acceptance."""
import asyncio
import json
import os
from datetime import timedelta
from pathlib import Path
import sys

import stage_three_ansa as common
import stage_three_api_catalog_ansa as lifecycle


def write_fixture(path):
    points = [(x, y, z) for z in (0, 10, 20) for y in (0, 10, 20) for x in (0, 10, 20)]
    nid = {point: 101 + i for i, point in enumerate(points)}
    groups, shells = {}, []
    definitions = [('master', 2, 0), ('slave', 2, 20), ('round', 0, 0),
                   ('round', 0, 20), ('round', 1, 0), ('round', 1, 20)]
    for name, axis, constant in definitions:
        other = [i for i in range(3) if i != axis]
        for b in (0, 10):
            for a in (0, 10):
                ids = []
                for u, v in ((a, b), (a + 10, b), (a + 10, b + 10), (a, b + 10)):
                    point = [0, 0, 0]
                    point[axis] = constant
                    point[other[0]], point[other[1]] = u, v
                    ids.append(nid[tuple(point)])
                eid = 1001 + len(shells)
                shells.append((eid, ids))
                groups.setdefault(name, []).append(eid)
    lines = ['*HEADING', 'Independent ABAQUS mechanism and mesh fixture', '*NODE']
    lines += [f'{101+i},{x},{y},{z}' for i, (x, y, z) in enumerate(points)]
    lines += ['201,100,0,0', '202,110,0,0', '*ELEMENT,TYPE=S4R,ELSET=BOUNDARY']
    lines += [str(eid) + ',' + ','.join(map(str, ids)) for eid, ids in shells]
    lines += ['*MATERIAL,NAME=STEEL', '*DENSITY', '7.85e-9', '*ELASTIC', '210000,0.3',
              '*SHELL SECTION,ELSET=BOUNDARY,MATERIAL=STEEL', '2']
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return groups


def remote_fixture(pid, arguments, fixture):
    import os
    import threading
    from ansa import base, constants, session
    if os.getpid() != pid or list(session.ProgramArguments()) != arguments or threading.current_thread() is not threading.main_thread():
        raise RuntimeError('Wrong owned fixture context')
    d = constants.ABAQUS
    base.SetCurrentDeck(d)
    assert base.InputAbaqus(fixture, model_action='merge_model') == 1
    material = base.CollectEntities(d, None, 'MATERIAL')[0]
    section = base.CollectEntities(d, None, 'SHELL_SECTION')[0]
    solid_section = base.CreateEntity(d, 'SOLID_SECTION', {'Name': 'HEX_SECTION', 'MID': material._id})
    assert solid_section is not None
    corners = [[40., 0., 0.], [60., 0., 0.], [60., 20., 0.], [40., 20., 0.]]
    curves = [base.CurvesNew([corners[i], corners[(i+1) % 4]]) for i in range(4)]
    assert all(curves)
    faces = base.FacesNewPlanar(curves, ret_ents=True)
    assert len(faces) == 1
    face = faces[0]
    assert 'PID' in face.card_fields(d)
    assert base.SetEntityCardValues(d, face, {'PID': section._id}) == 0
    return {'face_id': face._id, 'face_fields': base.GetEntityCardValues(d, face, tuple(face.card_fields(d))),
            'shell_section_id': section._id, 'solid_section_id': solid_section._id, 'material_id': material._id}


def remote_oracle(pid, arguments):
    import os
    import threading
    from ansa import base, constants, kinetics, session
    if os.getpid() != pid or list(session.ProgramArguments()) != arguments or threading.current_thread() is not threading.main_thread():
        raise RuntimeError('Wrong owned oracle context')
    d = constants.ABAQUS
    result = {}
    specs = [('NODE', ('X', 'Y', 'Z')), ('SHELL', ('PID', 'type', 'G1', 'G2', 'G3', 'G4')),
             ('SOLID', ('PID', 'type', 'G1', 'G2', 'G3', 'G4', 'G5', 'G6', 'G7', 'G8')),
             ('KIN_BODY', ('Name', 'GEN_TYPE')), ('KIN_JOINT', ('Name', 'TYPE', 'KIN_BODY_1', 'KIN_BODY_2',
                'DIRECTION_TYPE', 'ANCHOR_x', 'ANCHOR_y', 'ANCHOR_z', 'Z_x', 'Z_y', 'Z_z')),
             ('KIN_CONFIG', ('Name', 'ACTUATOR_JOINT', 'ACTUATOR_JOINT_TYPE'))]
    for kind, requested in specs:
        result[kind] = []
        for entity in sorted(base.CollectEntities(d, None, kind), key=lambda e: e._id):
            fields = tuple(f for f in requested if f in entity.card_fields(d))
            row = {'id': entity._id, 'fields': base.GetEntityCardValues(d, entity, fields)}
            if kind == 'KIN_CONFIG':
                row['joint_ids'] = sorted(e._id for e in kinetics.GetJointsFromConfig(entity))
            result[kind].append(row)
    return result


def remote_refresh(pid, arguments, diagnostics_path):
    import builtins
    import importlib
    import os
    import threading
    from ansa import session
    if os.getpid() != pid or list(session.ProgramArguments()) != arguments or threading.current_thread() is not threading.main_thread():
        raise RuntimeError('Wrong owned module refresh context')
    bridge = builtins._ansa_mcp_plugin_bridge
    assert not bridge.state.journal.busy()
    for name in ('ansa_mcp_shared.operations', 'ansa_mcp_bridge.operations',
                 'ansa_mcp_bridge.operations.meshing', 'ansa_mcp_bridge.operations.kinematics',
                 'ansa_mcp_bridge.operations.hexa_meshing'):
        importlib.reload(importlib.import_module(name))
    # A passive Python stack log locates a blocked native call without
    # interrupting or retrying the original model operation.
    import faulthandler
    handle = open(diagnostics_path, 'w', encoding='utf-8')
    builtins._mechanism_mesh_diagnostics = handle
    faulthandler.dump_traceback_later(30, repeat=True, file=handle)
    from ansa import base, mesh
    import time
    trace = open(diagnostics_path + '.calls', 'w', encoding='utf-8')
    builtins._mechanism_mesh_call_log = trace
    def wrap(owner, name):
        native = getattr(owner, name)
        def invoke(*args, **kwargs):
            trace.write(str(time.monotonic()) + ' ENTER ' + name + '\n')
            trace.flush()
            try:
                value = native(*args, **kwargs)
                if name == 'GetKeywordFieldLabels':
                    trace.write('RESULT ' + name + ' ' + repr(value)[:12000] + '\n')
                    trace.flush()
                return value
            finally:
                trace.write(str(time.monotonic()) + ' EXIT ' + name + '\n')
                trace.flush()
        setattr(owner, name, invoke)
    for owner, names in ((base, ('GetFacesOfCons', 'BoundBox', 'GetEntityCardValues', 'BCSettingsSetValues', 'GetKeywordFieldLabels')),
                          (mesh, ('ApplyNewLengthToMacros', 'Mesh', 'SaveMeshParams', 'ReadMeshParams'))):
        for name in names:
            wrap(owner, name)
    return {'refreshed': True}


def independent_mesh_checks(before, after, new_shells, solid_property):
    import math
    nodes = {row['id']: tuple(row['fields'][k] for k in ('X', 'Y', 'Z')) for row in after['NODE']}
    for row in before['NODE']:
        assert nodes[row['id']] == tuple(row['fields'][k] for k in ('X', 'Y', 'Z'))
    quads = [row for row in after['SHELL'] if row['id'] in new_shells]
    assert len(quads) == 4
    lengths = []
    for row in quads:
        v = row['fields']
        assert v['type'] == 'QUAD'
        ids = [v[f'G{i}'] for i in range(1, 5)]
        assert len(set(ids)) == 4
        for a, b in zip(ids, ids[1:] + ids[:1]):
            length = math.dist(nodes[a], nodes[b])
            assert math.isclose(length, 10., rel_tol=1e-9)
            lengths.append(length)
    assert len(after['SOLID']) == 8
    volume = 0.
    for row in after['SOLID']:
        v = row['fields']
        assert v['type'] == 'HEXA' and v['PID'] == solid_property
        ids = [v[f'G{i}'] for i in range(1, 9)]
        assert len(set(ids)) == 8
        xyz = [nodes[i] for i in ids]
        spans = [max(p[a] for p in xyz) - min(p[a] for p in xyz) for a in range(3)]
        assert all(math.isclose(s, 10., rel_tol=1e-9) for s in spans)
        # Trilinear HEX8 center Jacobian, independent of product topology checks.
        signs = [(-1,-1,-1),(1,-1,-1),(1,1,-1),(-1,1,-1),
                 (-1,-1,1),(1,-1,1),(1,1,1),(-1,1,1)]
        jac = [[sum(xyz[i][a] * signs[i][b] / 8 for i in range(8)) for b in range(3)] for a in range(3)]
        det = (jac[0][0]*(jac[1][1]*jac[2][2]-jac[1][2]*jac[2][1])
             - jac[0][1]*(jac[1][0]*jac[2][2]-jac[1][2]*jac[2][0])
             + jac[0][2]*(jac[1][0]*jac[2][1]-jac[1][1]*jac[2][0]))
        assert det > 0
        volume += det * 8
    assert math.isclose(volume, 8000., rel_tol=1e-9)
    return {'quad_count': 4, 'quad_edge_min': min(lengths), 'quad_edge_max': max(lengths),
            'hexa_count': 8, 'hex_center_jacobians_positive': True, 'volume_sum': volume}


async def verify(instructions, identity, helpers, run, report, path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    one, two, _ = helpers
    report['module_refresh'] = two.rpc(identity['port'], remote_refresh, identity['pid'], identity['program_args'], str(run / 'passive-stack.log'))
    parameters = StdioServerParameters(command=sys.executable, args=['-B', '-m', 'ansa_mcp_plugin',
        '--runtime-home', instructions['runtime_home']], env={**os.environ,
        'PYTHONPATH': str(common.PROJECT_ROOT / 'src'), 'PYTHONDONTWRITEBYTECODE': '1'})
    def oracle():
        return two.rpc(identity['port'], remote_oracle, identity['pid'], identity['program_args'])
    def record(name, value):
        lifecycle.record(report, path, name, value)
    def backup(name):
        return str(run / (name + '-before.ansa'))

    async with stdio_client(parameters, errlog=(run / 'mechanism-mesh-backend.stderr.log').open('w', encoding='utf-8')) as (reader, writer):
        async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=90)) as client:
            await client.initialize()
            h = common.McpHarness(client, report, run)
            await common._bind_for_run(h, identity, one, two, identity['port'])
            schemas = await h.list_schema()
            assert schemas['count'] == len(common.EXPECTED_TOOLS)
            await h.direct_result('ansa_model_new', {'preservation': 'discard'})
            await h.snapshot_context()
            fixture = run / 'mechanism-mesh.inp'
            groups = write_fixture(fixture)
            info = two.rpc(identity['port'], remote_fixture, identity['pid'], identity['program_args'], str(fixture))
            common._json_write(run / 'fixture.json', info)
            await h.snapshot_context()
            await h.direct_result('ansa_model_save', {'path': str(run / 'fixture.ansa')})
            # SaveAs writes an artifact but does not necessarily replace Untitled.
            await h.direct_result('ansa_model_open', {'path': str(run / 'fixture.ansa'), 'preservation': 'discard'})
            await h.snapshot_context()
            before = oracle()
            record('owned_abaqus_fixture_and_tool_schemas', {'fixture': info, 'schemas': schemas})

            read = await h.direct_result('ansa_mesh_faces', {'deck': 4, 'face_ids': [info['face_id']]})
            rows = read['faces']
            faces = [{'id': row['entity']['id'], 'expected_fields': row['fields'],
                      'expected_macro_id': row['macro']['id'], 'expected_shell_ids': row['shell_ids']} for row in rows]
            _, quad_task = await h.task('ansa_mesh_surface', {'deck': 4, 'faces': faces,
                                          'target_length': 10., 'element_type': 'quad'}, timeout_seconds=90)
            quad = quad_task['data']['result']
            common._json_write(run / 'quad-result.json', quad)
            assert quad['ok'] and quad['scope_verified'] and quad['parameters_restored'], quad
            after_quad = oracle()
            created_shells = set(r['id'] for r in after_quad['SHELL']) - set(r['id'] for r in before['SHELL'])
            assert len(created_shells) == 4
            record('quad_target_10_parameter_restore_and_scope', quad)

            by_shell = {row['id']: row['fields'] for row in before['SHELL']}
            request = {'deck': 4, 'property': {'deck': 4, 'type': 'SOLID_SECTION',
                'id': info['solid_section_id'], 'expected_fields': {'MID': info['material_id']}},
                'target_length': 10., 'backup_path': backup('hex')}
            for group in ('master', 'slave', 'round'):
                request[group] = [{'type': 'SHELL', 'id': eid, 'expected_fields': by_shell[eid]} for eid in groups[group]]
            hex_op = 'mm-hex-map-original'
            submission, completed = await h.task('ansa_mesh_hex_map', request, operation_id=hex_op)
            hex_result = completed['data']['result']
            common._json_write(run / 'hex-result.json', hex_result)
            assert hex_result['ok'] and hex_result['scope_verified'], hex_result
            after = oracle()
            independent = independent_mesh_checks(before, after, created_shells, info['solid_section_id'])
            _, repeated = await h.task('ansa_mesh_hex_map', request, operation_id=hex_op)
            assert repeated['task_id'] == completed['task_id'] and oracle() == after
            record('hex8_target_10_independent_volume_and_idempotency', {'mesh': hex_result, 'independent': independent})

            bodies = []
            for name, ground, node in (('MM_GROUND', True, None), ('MM_ARM', False, 201), ('MM_LINK', False, 202)):
                value = await h.direct_result('ansa_kinematics_body_create', {'deck': 4, 'name': name,
                    'ground': ground, 'members': [] if node is None else [{'deck': 4, 'type': 'NODE', 'id': node}],
                    'backup_path': backup(name)})
                assert value['ok'], value
                bodies.append(value['body']['entity']['id'])
            joints = []
            for name, kind, b1, b2, axis in (('MM_REVOLUTE', 'REVOLUTE', bodies[0], bodies[1], [2.,0.,0.]),
                                           ('MM_SLIDER', 'SLIDER', bodies[1], bodies[2], [0.,0.,3.])):
                value = await h.direct_result('ansa_kinematics_joint_create', {'deck': 4, 'name': name,
                    'joint_type': kind, 'body1_id': b1, 'body2_id': b2,
                    'anchor': [100.,0.,0.], 'axis': axis, 'backup_path': backup(name)})
                assert value['ok'], value
                joints.append(value['joint']['entity']['id'])
            config = await h.direct_result('ansa_kinematics_config_create', {'deck': 4, 'name': 'MM_CONFIG',
                'joint_ids': joints, 'actuator_joint_id': joints[1], 'backup_path': backup('config')})
            assert config['ok'], config
            inspected = await h.direct_result('ansa_kinematics_inspect', {'deck': 4,
                'config_id': config['config']['entity']['id']})
            common._json_write(run / 'mechanism-inspect.json', inspected)
            state = oracle()
            assert len(state['KIN_BODY']) == 3 and len(state['KIN_JOINT']) == 2 and len(state['KIN_CONFIG']) == 1
            assert state['KIN_CONFIG'][0]['joint_ids'] == sorted(joints)
            assert state['KIN_CONFIG'][0]['fields']['ACTUATOR_JOINT'] == joints[1]
            assert state['NODE'] == after['NODE'], 'Mechanism creation must not move FE nodes'
            record('rigid_bodies_revolute_slider_config_and_no_motion', {'config': config, 'inspect': inspected, 'oracle': state})

            rejected = await h.direct_result('ansa_kinematics_body_create', {'deck': 4, 'name': 'MM_ARM',
                'ground': False, 'members': [{'deck': 4, 'type': 'NODE', 'id': 201}],
                'backup_path': backup('duplicate')}, expected='failed')
            assert not Path(backup('duplicate')).exists() and oracle() == state
            record('duplicate_rejected_before_backup_without_effects', rejected)
            final = str(run / 'mechanism-mesh-final.ansa')
            await h.direct_result('ansa_model_save', {'path': final})
            await h.direct_result('ansa_model_open', {'path': final, 'preservation': 'discard'})
            await h.snapshot_context()
            reopened = oracle()
            assert reopened == state
            record('save_reopen_independent_mesh_and_kinematics_readback', {'path': final, 'oracle': reopened})


if __name__ == '__main__':
    raise SystemExit(lifecycle.main(report_name='mechanism-mesh-report.json',
        scope='ABAQUS quad/HEX8 and controlled kinematic interfaces', verify_callback=verify))
