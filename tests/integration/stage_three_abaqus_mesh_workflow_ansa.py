"""Independent explicit ABAQUS extrusion, mirror, merge and release acceptance."""
import asyncio
from datetime import timedelta
import os
from pathlib import Path
import sys

import stage_three_ansa as common
import stage_three_api_catalog_ansa as lifecycle


FIXTURE = '''*HEADING
Independent mesh workflow
*NODE
101,10,0,0
102,30,0,0
103,30,10,0
104,10,10,0
201,50,0,0
202,60,0,0
203,60,10,0
204,50,10,0
205,60,0,0
206,70,0,0
207,70,10,0
208,60,10,0
301,50,0,0
401,10,30,0
402,30,30,0
403,30,40,0
404,10,40,0
405,10,30,10
406,30,30,10
407,30,40,10
408,10,40,10
501,40,30,0
502,50,30,0
503,50,40,0
504,40,40,0
*NSET,NSET=PROTECTED_NODE
201
*ELEMENT,TYPE=S4R,ELSET=SOURCE
1001,101,102,103,104
*ELEMENT,TYPE=S4R,ELSET=MERGE
1002,201,202,203,204
1003,205,206,207,208
*ELEMENT,TYPE=C3D8R,ELSET=ORPHAN_SOLID
1004,401,402,403,404,405,406,407,408
*ELEMENT,TYPE=S4R,ELSET=ORPHAN_QUAD
1005,501,502,503,504
*MATERIAL,NAME=STEEL
*DENSITY
7.85e-9
*ELASTIC
210000,0.3
*SHELL SECTION,ELSET=SOURCE,MATERIAL=STEEL
2
*SHELL SECTION,ELSET=MERGE,MATERIAL=STEEL
2
*SOLID SECTION,ELSET=ORPHAN_SOLID,MATERIAL=STEEL
*SHELL SECTION,ELSET=ORPHAN_QUAD,MATERIAL=STEEL
2
'''


def remote_fixture(pid, arguments, fixture):
    import os
    import threading
    from ansa import base, constants, session
    assert os.getpid() == pid and list(session.ProgramArguments()) == arguments
    assert threading.current_thread() is threading.main_thread()
    d = constants.ABAQUS
    base.SetCurrentDeck(d)
    assert base.InputAbaqus(fixture, model_action='merge_model') == 1
    shell = base.GetEntity(d, 'SHELL', 1001)
    part = base.GetEntityCardValues(d, shell, ('__part__',))['__part__']
    if type(part) is int:
        part = base.GetEntity(d, 'ANSAPART', part)
    assert part is not None
    mirror_part = base.GetEntityCardValues(d, base.GetEntity(d, 'SOLID', 1004), ('__part__',))['__part__']
    if type(mirror_part) is int:
        mirror_part = base.GetEntity(d, 'ANSAPART', mirror_part)
    assert mirror_part is not None
    mirror_shell_part = base.GetEntityCardValues(d, base.GetEntity(d, 'SHELL', 1005), ('__part__',))['__part__']
    if type(mirror_shell_part) is int:
        mirror_shell_part = base.GetEntity(d, 'ANSAPART', mirror_shell_part)
    assert mirror_shell_part is not None
    material = base.CollectEntities(d, None, 'MATERIAL')[0]
    prop = base.CreateEntity(d, 'SOLID_SECTION', {'Name': 'WORKFLOW_SOLID', 'MID': material._id})
    protected_refs = [{'type': e.ansa_type(d), 'id': e._id}
                      for e in base.ReferenceEntities(base.GetEntity(d, 'NODE', 201), group_results=False)]
    assert any(e['type'] == 'SET' for e in protected_refs), protected_refs
    return {'part': {'deck': d, 'type': 'ANSAPART', 'id': part._id,
                     'expected_fields': base.GetEntityCardValues(d, part, ('Name',))},
            'property': {'deck': d, 'type': 'SOLID_SECTION', 'id': prop._id,
                         'expected_fields': base.GetEntityCardValues(d, prop, ('MID',))},
            'protected_references': protected_refs,
            'mirror_part': {'deck': d, 'type': 'ANSAPART', 'id': mirror_part._id,
                            'expected_fields': base.GetEntityCardValues(d, mirror_part, ('Name',))},
            'mirror_shell_part': {'deck': d, 'type': 'ANSAPART', 'id': mirror_shell_part._id,
                                 'expected_fields': base.GetEntityCardValues(d, mirror_shell_part, ('Name',))}}


def remote_oracle(pid, arguments):
    import os
    import threading
    from ansa import base, constants, session
    assert os.getpid() == pid and list(session.ProgramArguments()) == arguments
    assert threading.current_thread() is threading.main_thread()
    d = constants.ABAQUS
    specs = {
        'NODE': ('X', 'Y', 'Z'),
        'SHELL': ('type', 'PID', 'G1', 'G2', 'G3', 'G4'),
        'SOLID': ('type', 'EID', 'PID', *(f'G{i}' for i in range(1, 9))),
        'VOLUME': ('ID', 'PID', 'Type', 'Algorithm', 'Status', 'Order',
                   'Extrude steps', 'Distance', 'Biasing', 'Hexas', 'Grids', 'Volume'),
        'FACE': ('ID', 'PID'),
        'SET': ('ID', 'Name'),
        'ANSAPART': ('Name',),
    }
    out = {}
    for kind, requested in specs.items():
        rows = []
        for entity in sorted(base.CollectEntities(d, None, kind), key=lambda e: e._id):
            labels = entity.card_fields(d)
            fields = tuple(field for field in requested if field in labels)
            row = {'id': entity._id, 'fields': base.GetEntityCardValues(d, entity, fields)}
            if kind == 'VOLUME':
                row['solid_ids'] = sorted(e._id for e in base.CollectEntities(d, entity, 'SOLID'))
            if kind == 'SET':
                row['members'] = sorted([[e.ansa_type(d), e._id] for e in base.CollectEntities(
                    d, entity, '__ALL_ENTITIES__', recursive=False, no_expand_types=('SET',))])
            if kind == 'ANSAPART':
                row['members'] = {k: sorted(e._id for e in base.CollectEntities(d, entity, k, recursive=True))
                                  for k in ('SHELL', 'SOLID', 'VOLUME')}
            rows.append(row)
        out[kind] = rows
    return out


def positive_rectangular_hexes(state, ids):
    nodes = {row['id']: tuple(row['fields'][k] for k in ('X', 'Y', 'Z')) for row in state['NODE']}
    volumes = []
    for row in state['SOLID']:
        if row['id'] not in ids:
            continue
        fields = row['fields']
        assert fields['type'] == 'HEXA'
        points = [nodes[fields[f'G{i}']] for i in range(1, 9)]
        a, b, c = [tuple(points[j][i] - points[0][i] for i in range(3)) for j in (1, 3, 4)]
        determinant = a[0]*(b[1]*c[2]-b[2]*c[1])-a[1]*(b[0]*c[2]-b[2]*c[0])+a[2]*(b[0]*c[1]-b[1]*c[0])
        assert determinant > 0
        dimensions = [max(p[i] for p in points)-min(p[i] for p in points) for i in range(3)]
        volume = dimensions[0]*dimensions[1]*dimensions[2]
        assert abs(volume - 2000.) < 1.e-8
        volumes.append(volume)
    assert len(volumes) == len(ids)
    return sum(volumes)


async def _verify(instructions, identity, helpers, run, report, path):
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
    def conditions(state, kind, ids):
        return [{'type': kind, 'id': row['id'], 'expected_fields': row['fields']}
                for row in state[kind] if row['id'] in ids]
    async with stdio_client(parameters, errlog=(run / 'mesh-workflow-backend.stderr.log').open('w', encoding='utf-8')) as (reader, writer):
        async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=60)) as client:
            await client.initialize()
            h = common.McpHarness(client, report, run)
            await common._bind_for_run(h, identity, one, two, identity['port'])
            schemas = await h.list_schema()
            await h.direct_result('ansa_model_new', {'preservation': 'discard'})
            await h.snapshot_context()
            fixture = run / 'mesh-workflow.inp'
            fixture.write_text(FIXTURE, encoding='utf-8')
            info = two.rpc(identity['port'], remote_fixture, identity['pid'], identity['program_args'], str(fixture))
            await h.snapshot_context()
            initial = str(run / 'mesh-workflow-initial.ansa')
            await h.direct_result('ansa_model_save', {'path': initial})
            await h.direct_result('ansa_model_open', {'path': initial, 'preservation': 'discard'})
            await h.snapshot_context()
            before = oracle()
            initial_solids = {row['id'] for row in before['SOLID']}
            record('owned_explicit_mesh_fixture_and_tool_schemas', {'fixture': info, 'schemas': schemas})
            request = {'action': 'extrude_quad', 'deck': 4, 'shells': conditions(before, 'SHELL', [1001]),
                'vector': [0., 0., 20.], 'steps': 2, 'part': info['part'], 'property': info['property'], 'backup_path': backup('extrude')}
            _, task = await h.task('ansa_mesh_workflow', {'request': request}, operation_id='mesh-workflow-original-extrude')
            extruded = task['data']['result']
            assert extruded['ok'] and extruded['scope_verified'], extruded
            after_extrude = oracle()
            solids = {row['id'] for row in after_extrude['SOLID']} - initial_solids
            assert len(solids) == 2 and positive_rectangular_hexes(after_extrude, solids) == 4000.
            assert len(after_extrude['VOLUME']) == 1
            volume = after_extrude['VOLUME'][0]
            assert set(volume['solid_ids']) == solids
            target_part = next(row for row in after_extrude['ANSAPART'] if row['id'] == info['part']['id'])
            original_part = next(row for row in before['ANSAPART'] if row['id'] == info['part']['id'])
            assert set(target_part['members']['SOLID']) == set(original_part['members']['SOLID']) | solids
            assert volume['id'] in target_part['members']['VOLUME']
            for field, value in {'Type': 'Extrude', 'Algorithm': 'Translate', 'Status': 'Meshed',
                                 'Order': '1st', 'Extrude steps': 2, 'Distance': 20.,
                                 'Hexas': 2, 'Grids': 12, 'Volume': 4000.}.items():
                assert volume['fields'][field] == value, (field, volume)
            assert after_extrude['SHELL'] == before['SHELL']
            _, repeat = await h.task('ansa_mesh_workflow', {'request': request}, operation_id='mesh-workflow-original-extrude')
            assert repeat['task_id'] == task['task_id'] and oracle() == after_extrude
            record('quad_linear_extrusion_two_hex8_and_original_receipt', {'mesh': extruded, 'oracle': after_extrude})
            plane = {'point1': [0., 0., 0.], 'point2': [0., 1., 0.], 'point3': [0., 0., 1.]}
            volume_mirror_backup = backup('volume-owned-mirror')
            _, rejected_volume = await h.task('ansa_mesh_workflow', {'request': {'action': 'mirror_copy', 'deck': 4,
                'sources': conditions(after_extrude, 'SOLID', solids), 'part': info['part'],
                'plane': plane, 'backup_path': volume_mirror_backup}}, expected='failed')
            assert 'mirror_copy refuses elements owned by a VOLUME' in rejected_volume['error']['message']
            assert not Path(volume_mirror_backup).exists() and oracle() == after_extrude
            record('volume_owned_mirror_rejected_before_backup_without_implicit_closure', rejected_volume['error'])
            mirrored = await h.direct_result('ansa_mesh_workflow', {'request': {'action': 'mirror_copy', 'deck': 4,
                'sources': conditions(after_extrude, 'SOLID', [1004]), 'part': info['mirror_part'],
                'plane': plane, 'backup_path': backup('mirror')}})
            assert mirrored['ok'] and mirrored['scope_verified'], mirrored
            after_mirror = oracle()
            assert after_mirror['VOLUME'] == after_extrude['VOLUME']
            copies = {row['id'] for row in after_mirror['SOLID']} - {row['id'] for row in after_extrude['SOLID']}
            assert len(copies) == 1 and positive_rectangular_hexes(after_mirror, copies) == 2000.
            record('mirror_copy_coordinates_positive_hexes_and_source_preservation', {'mesh': mirrored, 'oracle': after_mirror})
            mirrored_shell = await h.direct_result('ansa_mesh_workflow', {'request': {'action': 'mirror_copy', 'deck': 4,
                'sources': conditions(after_mirror, 'SHELL', [1005]), 'part': info['mirror_shell_part'],
                'plane': plane, 'backup_path': backup('mirror-shell')}})
            assert mirrored_shell['ok'] and mirrored_shell['scope_verified'], mirrored_shell
            after_shell_mirror = oracle()
            shell_copies = {r['id'] for r in after_shell_mirror['SHELL']} - {r['id'] for r in after_mirror['SHELL']}
            assert len(shell_copies) == 1 and after_shell_mirror['VOLUME'] == after_extrude['VOLUME']
            points = {r['id']: tuple(r['fields'][k] for k in ('X', 'Y', 'Z')) for r in after_shell_mirror['NODE']}
            fields = next(r['fields'] for r in after_shell_mirror['SHELL'] if r['id'] in shell_copies)
            assert fields['type'] == 'QUAD'
            p, q, r, s = [points[fields[f'G{i}']] for i in range(1, 5)]
            assert {p, q, r, s} == {(-40., 30., 0.), (-50., 30., 0.), (-50., 40., 0.), (-40., 40., 0.)}
            assert (q[0]-p[0])*(r[1]-p[1])-(q[1]-p[1])*(r[0]-p[0]) > 0
            record('quad_mirror_connectivity_reflected_normal_and_source_preservation', {'mesh': mirrored_shell, 'oracle': after_shell_mirror})
            after_mirror = after_shell_mirror
            rejected_backup = backup('protected-node-merge')
            _, rejected = await h.task('ansa_mesh_workflow', {'request': {'action': 'merge_nodes', 'deck': 4,
                'pairs': [{'source': conditions(after_mirror, 'NODE', [201])[0],
                           'target': conditions(after_mirror, 'NODE', [301])[0]}],
                'backup_path': rejected_backup}}, expected='failed')
            assert 'non-FE references' in rejected['error']['message']
            assert not Path(rejected_backup).exists() and oracle() == after_mirror
            record('node_set_reference_rejected_before_backup_without_model_change', rejected['error'])
            pairs = [{'source': conditions(after_mirror, 'NODE', [a])[0], 'target': conditions(after_mirror, 'NODE', [b])[0]}
                     for a, b in ((205, 202), (208, 203))]
            merged = await h.direct_result('ansa_mesh_workflow', {'request': {'action': 'merge_nodes', 'deck': 4,
                'pairs': pairs, 'backup_path': backup('merge')}})
            assert merged['ok'] and merged['scope_verified'], merged
            after_merge = oracle()
            assert len(after_merge['NODE']) == len(after_mirror['NODE']) - 2
            shell = next(row['fields'] for row in after_merge['SHELL'] if row['id'] == 1003)
            assert [shell[f'G{i}'] for i in range(1, 5)] == [202, 206, 207, 203]
            record('explicit_coincident_node_pairs_and_connected_shell_readback', {'mesh': merged, 'oracle': after_merge})
            released = await h.direct_result('ansa_mesh_workflow', {'request': {'action': 'release_mesh', 'deck': 4,
                'entities': conditions(after_merge, 'SOLID', copies) + conditions(after_merge, 'SHELL', shell_copies), 'backup_path': backup('release')}})
            assert released['ok'] and released['scope_verified'], released
            state = oracle()
            assert {row['id'] for row in state['SOLID']} == solids | initial_solids
            assert state['SHELL'] == [r for r in after_merge['SHELL'] if r['id'] not in shell_copies]
            assert state['NODE'] == after_merge['NODE'] and state['FACE'] == before['FACE']
            record('release_only_selected_solids_preserves_nodes_and_geometry_scope', {'mesh': released, 'oracle': state})
            final = str(run / 'mesh-workflow-final.ansa')
            await h.direct_result('ansa_model_save', {'path': final})
            await h.direct_result('ansa_model_open', {'path': final, 'preservation': 'discard'})
            await h.snapshot_context()
            assert oracle() == state
            record('save_reopen_independent_geometry_and_mesh_cards', {'path': final, 'oracle': state})


async def verify(instructions, identity, helpers, run, report, path):
    try:
        await _verify(instructions, identity, helpers, run, report, path)
    except BaseException:
        try:
            state = helpers[1].rpc(identity['port'], remote_oracle, identity['pid'], identity['program_args'])
            common._json_write(run / 'independent-at-failure.json', state)
        except Exception as exc:
            common._json_write(run / 'independent-at-failure-error.json', {'error': str(exc)})
        raise


if __name__ == '__main__':
    raise SystemExit(lifecycle.main(report_name='abaqus-mesh-workflow-report.json', scope='explicit ABAQUS mesh workflow primitives', verify_callback=verify))
