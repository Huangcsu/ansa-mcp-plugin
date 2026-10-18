"""Independent ABAQUS contact cards, oriented geometry and gap readback."""
import asyncio
from datetime import timedelta
import json
import os
from pathlib import Path
import sys

import stage_three_ansa as common
import stage_three_api_catalog_ansa as lifecycle


FIXTURE = '''*HEADING
Independent contact inspection fixture
*NODE
1001,0,0,0
1002,20,0,0
1003,20,10,0
1004,0,10,0
1005,0,0,3
1006,20,0,3
1007,20,10,3
1008,0,10,3
1011,0,0,-10
1012,20,0,-10
1013,20,10,-10
1014,0,10,-10
1015,0,0,0
1016,20,0,0
1017,20,10,0
1018,0,10,0
*ELEMENT,TYPE=S4R,ELSET=LOW
1001,1001,1002,1003,1004
*ELEMENT,TYPE=S4R,ELSET=UP
1002,1005,1006,1007,1008
*ELEMENT,TYPE=C3D8R,ELSET=SOLIDS
1010,1011,1012,1013,1014,1015,1016,1017,1018
*MATERIAL,NAME=STEEL
*DENSITY
7.85e-9
*ELASTIC
210000,0.3
*SHELL SECTION,ELSET=LOW,MATERIAL=STEEL,OFFSET=SPOS
2
*SHELL SECTION,ELSET=UP,MATERIAL=STEEL,OFFSET=0.25
2
*SOLID SECTION,ELSET=SOLIDS,MATERIAL=STEEL
*SURFACE,NAME=LOW_SURF,TYPE=ELEMENT
LOW,SPOS
*SURFACE,NAME=UP_SURF,TYPE=ELEMENT
UP,SNEG
*SURFACE,NAME=SOLID_SURF,TYPE=ELEMENT
SOLIDS,S2
*SURFACE INTERACTION,NAME=FRIC
*FRICTION
0.2
*CONTACT PAIR,INTERACTION=FRIC,TYPE=SURFACE TO SURFACE
UP_SURF,LOW_SURF
*TIE,NAME=GOOD_TIE,POSITION TOLERANCE=0.1,ADJUST=NO
SOLID_SURF,LOW_SURF
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
    return {kind: sorted(e._id for e in base.CollectEntities(d, None, kind)) for kind in ('TIE', 'CONTACT_PAIR', 'SET')}


def remote_oracle(pid, arguments):
    import os
    import threading
    from ansa import base, constants, session
    assert os.getpid() == pid and list(session.ProgramArguments()) == arguments
    assert threading.current_thread() is threading.main_thread()
    d = constants.ABAQUS
    def encode(value):
        if isinstance(value, base.Entity):
            return {'type': value.ansa_type(d), 'id': value._id}
        if isinstance(value, (list, tuple)):
            return [encode(v) for v in value]
        if isinstance(value, dict):
            return {str(k): encode(v) for k, v in value.items()}
        return value
    out = {}
    for kind in ('NODE', 'SHELL', 'SOLID', 'SHELL_SECTION', 'SOLID_SECTION', 'TIE', 'CONTACT_PAIR', 'SURFACE_INTERACTION', 'FRICTION', 'SET'):
        out[kind] = []
        for entity in sorted(base.CollectEntities(d, None, kind), key=lambda e: e._id):
            labels = tuple(entity.card_fields(d))
            row = {'id': entity._id, 'fields': encode(base.GetEntityCardValues(d, entity, labels))}
            if kind == 'SET':
                members = base.CollectEntities(d, entity, '__ALL_ENTITIES__', False, no_expand_types=('SET',))
                row['members'] = sorted([[e.ansa_type(d), e._id] for e in members])
            out[kind].append(row)
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
    def stable(value):
        return {k: v for k, v in value.items()
                if k not in ('model_after', 'executor', 'thread_id', 'main_thread')}
    def gap(result, mode):
        return result['relationships'][0]['geometry_comparison'][mode]
    async with stdio_client(parameters, errlog=(run / 'contact-backend.stderr.log').open('w', encoding='utf-8')) as (reader, writer):
        async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=60)) as client:
            await client.initialize()
            h = common.McpHarness(client, report, run)
            await common._bind_for_run(h, identity, one, two, identity['port'])
            schemas = await h.list_schema()
            await h.direct_result('ansa_model_new', {'preservation': 'discard'})
            await h.snapshot_context()
            fixture = run / 'contact.inp'
            fixture.write_text(FIXTURE, encoding='utf-8')
            info = two.rpc(identity['port'], remote_fixture, identity['pid'], identity['program_args'], str(fixture))
            await h.snapshot_context()
            initial = str(run / 'contact-initial.ansa')
            await h.direct_result('ansa_model_save', {'path': initial})
            await h.direct_result('ansa_model_open', {'path': initial, 'preservation': 'discard'})
            await h.snapshot_context()
            before = oracle()
            common._json_write(run / 'independent-before.json', before)
            record('owned_contact_fixture_and_tool_schemas', {'fixture': info, 'schemas': schemas})
            tie = await h.direct_result('ansa_contact_inspect', {'deck': 4, 'relation_type': 'TIE', 'ids': info['TIE'], 'tolerance': 0.1})
            common._json_write(run / 'tie-result.json', tie)
            assert tie['complete'], tie
            assert len(tie['relationships']) == 1
            assert gap(tie, 'reference_gap')['slave_node_projection_coverage']['ratio'] == 1.
            assert gap(tie, 'uniform_section_physical_gap')['slave_to_master_node_distances']['maximum_distance'] == 0.
            assert all(s['face_area_sum'] == 200. and s['face_count'] == 1 for s in tie['surfaces'])
            assert tie['relationships'][0]['tie_tolerance']['solver_position_tolerance'] == .1
            record('tie_cards_oriented_faces_and_geometry', tie)
            request = {'deck': 4, 'relation_type': 'CONTACT_PAIR', 'ids': info['CONTACT_PAIR'], 'tolerance': 3.1}
            _, task = await h.task('ansa_contact_inspect', request, operation_id='contact-original-readback')
            contact = task['data']['result']
            common._json_write(run / 'contact-result.json', contact)
            assert contact['complete'], contact
            interaction = contact['relationships'][0]['interaction']
            assert interaction['surface_interaction']['card']['values']['*FRICTION'] == 'YES'
            assert interaction['friction']['values']['M'] == .2
            assert gap(contact, 'reference_gap')['slave_to_master_node_distances']['minimum_distance'] == 3.
            assert gap(contact, 'reference_gap')['slave_node_projection_coverage']['ratio'] == 1.
            assert gap(contact, 'uniform_section_physical_gap')['slave_to_master_node_distances']['minimum_distance'] == 1.5
            assert gap(contact, 'uniform_section_physical_gap')['slave_to_master_node_distances']['within_tolerance_nodes'] == 4
            for surface in contact['surfaces']:
                assert surface['face_area_sum'] == 200. and surface['face_count'] == 1
                face = surface['faces'][0]
                assert face['unit_normal'] == [0., 0., 1.]
                assert face['section_offset']['offset_ratio'] in (.5, .25)
            record('contact_interaction_friction_offsets_and_gap', contact)
            _, repeat = await h.task('ansa_contact_inspect', request, operation_id='contact-original-readback')
            assert repeat['task_id'] == task['task_id'] and repeat['data']['result'] == contact
            narrow = await h.direct_result('ansa_contact_inspect', {**request, 'tolerance': 1.0})
            assert gap(narrow, 'reference_gap')['slave_node_projection_coverage']['ratio'] == 0.
            assert gap(narrow, 'uniform_section_physical_gap')['slave_to_master_node_distances']['within_tolerance_nodes'] == 0
            record('explicit_tolerance_comparison_and_original_receipt', {'narrow': narrow, 'task_id': task['task_id']})
            missing = await h.direct_result('ansa_contact_inspect', {'deck': 4, 'relation_type': 'TIE', 'ids': [999999], 'tolerance': .1})
            assert not missing['complete'] and missing['status'] == 'partial'
            assert oracle() == before
            record('unknown_relationship_partial_and_model_unchanged', missing)
            final = str(run / 'contact-final.ansa')
            await h.direct_result('ansa_model_save', {'path': final})
            await h.direct_result('ansa_model_open', {'path': final, 'preservation': 'discard'})
            await h.snapshot_context()
            assert oracle() == before
            reopened = await h.direct_result('ansa_contact_inspect', request)
            assert stable(reopened) == stable(contact)
            record('save_reopen_independent_cards_and_contact_readback', {'path': final, 'contact': reopened})


if __name__ == '__main__':
    raise SystemExit(lifecycle.main(report_name='abaqus-contact-report.json', scope='read-only ABAQUS contact relationships and geometry', verify_callback=verify))
