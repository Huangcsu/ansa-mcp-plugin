"""Independent MCP kinematic interfaces, without meshing or native wrappers."""
import asyncio
from datetime import timedelta
import json
import os
from pathlib import Path
import sys

import stage_three_ansa as common
import stage_three_api_catalog_ansa as lifecycle


def remote_fixture(pid, arguments, fixture):
    import os
    import threading
    from ansa import base, constants, session
    assert os.getpid() == pid and list(session.ProgramArguments()) == arguments
    assert threading.current_thread() is threading.main_thread()
    base.SetCurrentDeck(constants.ABAQUS)
    assert base.InputAbaqus(fixture, model_action='merge_model') == 1
    return {'deck': base.CurrentDeck(), 'nodes': [e._id for e in base.CollectEntities(constants.ABAQUS, None, 'NODE')]}


def remote_oracle(pid, arguments):
    import os
    import threading
    from ansa import base, constants, kinetics, session
    assert os.getpid() == pid and list(session.ProgramArguments()) == arguments
    assert threading.current_thread() is threading.main_thread()
    d = constants.ABAQUS
    out = {}
    fields_by_type = {
        'NODE': ('X', 'Y', 'Z'),
        'KIN_BODY': ('EID', 'Name', 'GEN_TYPE'),
        'KIN_JOINT': ('ID', 'Name', 'TYPE', 'KIN_BODY_1', 'KIN_BODY_2', 'ANCHOR_x', 'ANCHOR_y', 'ANCHOR_z', 'Z_x', 'Z_y', 'Z_z'),
        'KIN_CONFIG': ('EID', 'Name', 'ACTUATOR_JOINT', 'ACTUATOR_JOINT_TYPE'),
    }
    for kind, requested in fields_by_type.items():
        rows = []
        for entity in sorted(base.CollectEntities(d, None, kind), key=lambda e: e._id):
            labels = entity.card_fields(d)
            fields = tuple(f for f in requested if f in labels)
            row = {'id': entity._id, 'fields': base.GetEntityCardValues(d, entity, fields)}
            if kind == 'KIN_BODY':
                members = base.CollectEntities(d, entity, '__ALL_ENTITIES__', False, hidden_entities=True, no_expand_types=('SET',))
                row['members'] = sorted([[e.ansa_type(d), e._id] for e in members])
            if kind == 'KIN_CONFIG':
                row['joint_ids'] = sorted(e._id for e in kinetics.GetJointsFromConfig(entity))
                row['body_ids'] = sorted(e._id for e in kinetics.GetBodiesFromConfig(entity, False))
                row['model_info'] = kinetics.GetModelInfo(entity)
            rows.append(row)
        out[kind] = rows
    return out


async def verify(instructions, identity, helpers, run, report, path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    one, two, _ = helpers
    parameters = StdioServerParameters(command=sys.executable, args=['-B', '-m', 'ansa_mcp_plugin', '--runtime-home', instructions['runtime_home']],
        env={**os.environ, 'PYTHONPATH': str(common.PROJECT_ROOT / 'src'), 'PYTHONDONTWRITEBYTECODE': '1'})
    def oracle():
        return two.rpc(identity['port'], remote_oracle, identity['pid'], identity['program_args'])
    def record(name, value):
        lifecycle.record(report, path, name, value)
    def backup(name):
        return str(run / (name + '-before.ansa'))
    async with stdio_client(parameters, errlog=(run / 'kinematics-backend.stderr.log').open('w', encoding='utf-8')) as (reader, writer):
        async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=60)) as client:
            await client.initialize()
            h = common.McpHarness(client, report, run)
            await common._bind_for_run(h, identity, one, two, identity['port'])
            schemas = await h.list_schema()
            await h.direct_result('ansa_model_new', {'preservation': 'discard'})
            await h.snapshot_context()
            fixture = run / 'kinematics.inp'
            fixture.write_text('*HEADING\nIndependent mechanism\n*NODE\n201,0,0,0\n202,10,0,0\n', encoding='utf-8')
            info = two.rpc(identity['port'], remote_fixture, identity['pid'], identity['program_args'], str(fixture))
            await h.snapshot_context()
            initial = str(run / 'kinematics-initial.ansa')
            await h.direct_result('ansa_model_save', {'path': initial})
            await h.direct_result('ansa_model_open', {'path': initial, 'preservation': 'discard'})
            await h.snapshot_context()
            before = oracle()
            record('owned_abaqus_fixture_and_tool_schemas', {'fixture': info, 'schemas': schemas, 'oracle': before})
            bodies = []
            for name, ground, node in (('KG_GROUND', True, None), ('KG_ARM', False, 201), ('KG_LINK', False, 202)):
                request = {'deck': 4, 'name': name, 'ground': ground,
                    'members': [] if ground else [{'deck': 4, 'type': 'NODE', 'id': node}], 'backup_path': backup(name)}
                operation = 'kinematics-' + name
                _, task = await h.task('ansa_kinematics_body_create', request, operation_id=operation)
                result = task['data']['result']
                common._json_write(run / (name + '-result.json'), result)
                assert result['ok'], result
                bodies.append(result['body']['entity']['id'])
                _, repeated = await h.task('ansa_kinematics_body_create', request, operation_id=operation)
                assert repeated['task_id'] == task['task_id']
            record('ground_rigid_bodies_members_and_original_receipt', {'body_ids': bodies, 'oracle': oracle()})
            joints = []
            for name, kind, a, b, axis in (('KG_REVOLUTE', 'REVOLUTE', bodies[0], bodies[1], [2., 0., 0.]),
                                          ('KG_SLIDER', 'SLIDER', bodies[1], bodies[2], [0., 0., 3.])):
                result = await h.direct_result('ansa_kinematics_joint_create', {'deck': 4, 'name': name, 'joint_type': kind,
                    'body1_id': a, 'body2_id': b, 'anchor': [0., 0., 0.], 'axis': axis, 'backup_path': backup(name)})
                common._json_write(run / (name + '-result.json'), result)
                assert result['ok'], result
                joints.append(result['joint']['entity']['id'])
            record('revolute_slider_body_order_anchor_and_normalized_axis', oracle())
            result = await h.direct_result('ansa_kinematics_config_create', {'deck': 4, 'name': 'KG_CONFIG',
                'joint_ids': joints, 'actuator_joint_id': joints[1], 'backup_path': backup('config')})
            assert result['ok'], result
            config_id = result['config']['entity']['id']
            inspected = await h.direct_result('ansa_kinematics_inspect', {'deck': 4, 'config_id': config_id})
            state = oracle()
            cfg = state['KIN_CONFIG'][0]
            assert cfg['joint_ids'] == sorted(joints) and cfg['body_ids'] == sorted(bodies[1:])
            assert sorted(row['entity']['id'] for row in inspected['bodies']) == sorted(bodies)
            assert inspected['native_body_ids'] == cfg['body_ids']
            assert cfg['model_info']['dofs'] == 2 and cfg['model_info']['redundant_constraints'] == 0, cfg
            assert inspected['model_info'] == cfg['model_info'] == result['model_info']
            assert state['NODE'] == before['NODE']
            record('config_membership_dofs_and_no_node_motion', {'created': result, 'inspect': inspected, 'oracle': state})
            rejected = await h.direct_result('ansa_kinematics_body_create', {'deck': 4, 'name': 'KG_ARM', 'ground': False,
                'members': [{'deck': 4, 'type': 'NODE', 'id': 201}], 'backup_path': backup('duplicate')}, expected='failed')
            assert not Path(backup('duplicate')).exists() and oracle() == state
            record('duplicate_rejected_before_backup_without_effects', rejected)
            final = str(run / 'kinematics-final.ansa')
            await h.direct_result('ansa_model_save', {'path': final})
            await h.direct_result('ansa_model_open', {'path': final, 'preservation': 'discard'})
            await h.snapshot_context()
            assert oracle() == state
            record('save_reopen_independent_cards_members_and_dofs', {'path': final, 'oracle': state})


if __name__ == '__main__':
    raise SystemExit(lifecycle.main(report_name='kinematics-report.json', scope='controlled ABAQUS kinematic tool interfaces', verify_callback=verify))
