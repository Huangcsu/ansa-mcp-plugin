"""官方原生安装包与独立 wheel 的隔离 GUI/TCP/模型验收。"""

import argparse
import asyncio
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import uuid
import xml.etree.ElementTree as ET

import grpc
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import stage_three_ansa as core


def remote_state():
    import builtins
    import os
    import sys
    import threading
    from ansa import base, constants, session
    bridge = getattr(builtins, '_ansa_mcp_plugin_bridge', None)
    origins = {name: getattr(module, '__file__', None)
               for name, module in tuple(sys.modules.items())
               if name.startswith(('AnsaMcpPlugin_root', 'ansa_mcp_bridge', 'ansa_mcp_shared'))}
    result = {'pid': os.getpid(), 'program_args': list(session.ProgramArguments()),
              'main_thread': threading.current_thread() is threading.main_thread(),
              'profile': base.BCSettingsGetCurrentProfileName(),
              'version': constants.app_version, 'bridge_present': bridge is not None,
              'module_origins': origins, 'sys_path': list(sys.path)}
    if bridge is not None:
        result.update(instance_id=bridge.state.instance_id, session_id=bridge.state.session_id,
                      runtime_home=str(bridge.runtime_home), closed=bridge.closed,
                      timer_id=bridge.timer._timer_id, ticks=bridge.executor.ticks,
                      ui=dict(bridge.state.panel_info))
    return result


def remote_fixture(inp, baseline):
    from ansa import base, constants
    deck = constants.ABAQUS
    if base.CollectEntities(deck, None, ('NODE', 'SHELL', 'SOLID')):
        raise RuntimeError('Independent fixture requires an empty model')
    base.SetCurrentDeck(deck)
    if base.InputAbaqus(inp, model_action='merge_model') != 1:
        raise RuntimeError('Abaqus fixture input failed')
    if base.SaveAs(baseline) != 0:
        raise RuntimeError('Baseline SaveAs failed')
    base.ZoomAll()
    base.RedrawAll()
    return {'path': base.DataBaseName(), 'deck': base.CurrentDeck(),
            'nodes': len(base.CollectEntities(deck, None, 'NODE')),
            'shells': len(base.CollectEntities(deck, None, 'SHELL'))}


def remote_readback():
    from ansa import base, constants
    deck = constants.ABAQUS
    nodes = []
    for entity in base.CollectEntities(deck, None, 'NODE'):
        available = entity.card_fields(deck)
        fields = ['X', 'Y', 'Z']
        if not all(field in available for field in fields):
            raise RuntimeError('Abaqus NODE fields unavailable')
        values = base.GetEntityCardValues(deck, entity, fields)
        nodes.append({'id': entity._id, **values})
    shells = []
    for entity in base.CollectEntities(deck, None, 'SHELL'):
        fields = ['type', 'PID', 'G1', 'G2', 'G3', 'G4']
        if not all(field in entity.card_fields(deck) for field in fields):
            raise RuntimeError('Abaqus SHELL fields unavailable')
        shells.append({'id': entity._id, **base.GetEntityCardValues(deck, entity, fields)})
    base.ZoomAll()
    base.RedrawAll()
    return {'path': base.DataBaseName(), 'deck': base.CurrentDeck(),
            'nodes': sorted(nodes, key=lambda e: e['id']), 'shells': shells}


def prepare(args):
    native = args.native_install.resolve(strict=True)
    external = args.external_install.resolve(strict=True)
    ppl = native / 'AnsaMcpPlugin.ppl'
    archive = native / 'AnsaMcpPlugin/src/AnsaMcpPlugin.bpkgo.zip'
    if not ppl.is_file() or not archive.is_file():
        raise ValueError('Official installation layout is incomplete')
    manifest = json.loads((external / 'install-manifest.json').read_text(encoding='utf-8'))
    python = external / manifest['venv_python']
    run = core.VERIFICATION_ROOT / ('stage-four-native-' + uuid.uuid4().hex)
    run.mkdir(parents=True, exist_ok=False)
    runtime = core._runtime_home(args.runtime_home)
    if runtime.exists():
        raise FileExistsError('Use a new private runtime: ' + str(runtime))
    env = os.environ.copy()
    env.pop('PYTHONPATH', None)
    env.pop('PYTHONHOME', None)
    init = "from pathlib import Path;from ansa_mcp_plugin.backend.service import Service;s=Service(Path(__import__('sys').argv[1]));s.close()"
    subprocess.run([str(python), '-I', '-c', init, str(runtime)], cwd=run, env=env, check=True)
    config = run / 'installation.json'
    core._json_write(config, {'runtime_home': str(runtime)})
    profiles = Path.home() / '.BETA/version_26.10.000/ANSA/profiles'
    original = profiles / 'default/gui/ANSA.xml'
    raw = original.read_bytes()
    profile_name = 'mcp-native-' + run.name.rsplit('-', 1)[1][:12]
    profile = profiles / profile_name
    if profile.exists():
        raise FileExistsError(profile)
    replacement = '<Script_Plugins>\n<plugin name="' + ppl.as_posix() + '" loaded="1" addedFrom="3" />\n</Script_Plugins>'
    xml, count = re.subn(r'<Script_Plugins>.*?</Script_Plugins>', replacement,
                         raw.decode('utf-8'), flags=re.S)
    parsed = ET.fromstring(xml)
    plugins = list(parsed.iter('plugin'))
    if count != 1 or len(plugins) != 1 or plugins[0].get('name') != ppl.as_posix():
        raise AssertionError('Isolated profile plugin scope is not unique')
    (profile / 'gui').mkdir(parents=True)
    (profile / 'profile.ini').write_bytes((profiles / 'default/profile.ini').read_bytes())
    (profile / 'gui/ANSA.xml').write_text(xml, encoding='utf-8')
    translator = run / 'native_install_isolated_translator.py'
    translator.write_text('# Isolated official native installation acceptance.\n', encoding='ascii')
    port = core._free_port()
    ansa = core._ansa_root(args.ansa_root)
    info = {'run_dir': str(run), 'port': port, 'translator': str(translator),
            'runtime_home': str(runtime), 'native_install': str(native),
            'external_install': str(external), 'external_python': str(python),
            'settings_profile': profile_name, 'profile': str(profile),
            'default_xml': str(original), 'default_sha256': hashlib.sha256(raw).hexdigest(),
            'archive': str(archive), 'archive_sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
            'launch_command': [str(ansa / 'ansa64.bat'), '-grpcport', str(port), '-grpchost', '127.0.0.1',
                               '-translfp', str(translator), '-nolauncher', '--skip-release-highlights',
                               '--settings-profile', profile_name],
            'environment': {'ANSA_MCP_PLUGIN_CONFIG': str(config), 'ANSA_MCP_SUM_AUTO_POLL': '0',
                            'PYTHONPATH': '', 'PYTHONDONTWRITEBYTECODE': '1'}}
    core._json_write(run / 'launch_instructions.json', info)
    (run / 'fixture.inp').write_text('*HEADING\nNative package acceptance\n*NODE\n1,0,0,0\n2,100,0,0\n3,100,60,0\n4,0,60,0\n*ELEMENT,TYPE=S4,ELSET=plate\n1,1,2,3,4\n*MATERIAL,NAME=steel\n*ELASTIC\n210000,0.3\n*DENSITY\n7.85e-9\n*SHELL SECTION,ELSET=plate,MATERIAL=steel\n2\n', encoding='ascii')
    print(json.dumps(info), flush=True)


async def operator(run, stage):
    marker = run / ('ui-' + stage + '.json')
    if marker.exists():
        raise FileExistsError('Do not reuse an old UI observation: ' + str(marker))
    core._json_write(run / 'ui-stage.json', {'stage': stage, 'marker': str(marker)})
    print(json.dumps({'ui_stage': stage}), flush=True)
    deadline = asyncio.get_running_loop().time() + 600
    while asyncio.get_running_loop().time() < deadline:
        if marker.is_file():
            return json.loads(marker.read_text(encoding='utf-8'))
        await asyncio.sleep(.5)
    raise TimeoutError('GUI observation pending: ' + stage)


async def verify(args):
    run = args.run_dir.resolve(strict=True)
    run.relative_to(core.VERIFICATION_ROOT)
    info = json.loads((run / 'launch_instructions.json').read_text(encoding='utf-8'))
    ownership = json.loads((run / 'cim-ownership.json').read_text(encoding='utf-8-sig'))
    if isinstance(ownership, list):
        if len(ownership) != 1:
            raise AssertionError('CIM ownership is not unique')
        ownership = ownership[0]
    if info['translator'].casefold() not in ownership['CommandLine'].casefold():
        raise AssertionError('CIM translator mismatch')
    first, second = core._import_runtime_helpers()
    original = core._bounded_grpc(grpc, first.DeadlineInterceptor)
    report = {'success': False, 'scope': 'official_native_and_installed_wheel_e2e',
              'cases': [], 'visual_acceptance': False, 'solver_executed': False}
    report_path = run / 'native-install-report.json'

    def record(name, **evidence):
        report['cases'].append({'name': name, 'ok': True, **evidence})
        core._json_write(report_path, report)
        print(json.dumps({'case': name, 'ok': True}), flush=True)

    def state():
        value = second.rpc(info['port'], remote_state)
        core._verify_program_arguments(value, Path(info['translator']), info['port'])
        if value['pid'] != ownership['ProcessId'] or value['profile'] != info['settings_profile']:
            raise AssertionError('ANSA PID/profile does not belong to this run')
        return value

    def installed_origins(value):
        archive = Path(info['archive']).resolve()
        for name, origin in value['module_origins'].items():
            if not origin:
                raise AssertionError('Missing installed module origin: ' + name)
            Path(origin).resolve().relative_to(archive)
        if not any(name.endswith('.app') for name in value['module_origins']):
            raise AssertionError('Official app entry was not loaded')
        for entry in value['sys_path']:
            if entry and Path(entry).is_absolute() and Path(entry).resolve() == core.PROJECT_ROOT / 'src':
                raise AssertionError('ANSA loaded the source tree')

    try:
        initial = state()
        if initial['bridge_present']:
            raise AssertionError('Bridge was already running before Ribbon action')
        seed = second.rpc(info['port'], remote_fixture, str(run / 'fixture.inp'), str(run / 'native-baseline.ansa'))
        if seed['deck'] != 4 or seed['nodes'] != 4 or seed['shells'] != 1:
            raise AssertionError('Independent seed mismatch')
        env = os.environ.copy()
        env.pop('PYTHONPATH', None)
        env.pop('PYTHONHOME', None)
        params = StdioServerParameters(command=info['external_python'],
            args=['-I', '-m', 'ansa_mcp_plugin', '--runtime-home', info['runtime_home']],
            cwd=str(run), env=env)
        with (run / 'backend.stderr.log').open('w', encoding='utf-8') as errlog:
            async with stdio_client(params, errlog=errlog) as (reader, writer):
                async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=30)) as client:
                    await client.initialize()
                    h = core.McpHarness(client, report, run)
                    schemas = await h.list_schema()
                    observed = await operator(run, 'start_and_bind')
                    status = await h.call('ansa_connection_status')
                    h.target = status['data']['active_target']
                    if h.target is None:
                        raise AssertionError('Panel binding was not completed')
                    snap = await h.snapshot_context()
                    entry = state()
                    installed_origins(entry)
                    if Path(entry['runtime_home']).resolve() != Path(info['runtime_home']).resolve():
                        raise AssertionError('Installed bridge runtime mismatch')
                    if snap['pid'] != initial['pid'] or not snap['ui']['alive'] or not snap['ui']['visible']:
                        raise AssertionError('Installed native panel is not visible in the owned ANSA')
                    report['identity'] = entry
                    record('official_ribbon_installed_origins_and_bound_panel', observation=observed, target=h.target)
                    record('installed_wheel_stdio_tcp_and_69_schemas', schemas=schemas, snapshot=snap)
                    fields = {'deck': 4, 'entity_type': 'NODE', 'fields': {'X': 125., 'Y': 30., 'Z': 0.}}
                    operation = 'native-node-' + run.name
                    submitted, completed = await h.task('ansa_entity_create', fields, operation_id=operation)
                    replay, _ = await h.task('ansa_entity_create', fields, operation_id=operation)
                    if replay['task_id'] != submitted['task_id']:
                        raise AssertionError('Idempotent replay created another task')
                    model = second.rpc(info['port'], remote_readback)
                    if len(model['nodes']) != 5 or len(model['shells']) != 1:
                        raise AssertionError('Installed write/readback count mismatch')
                    if sum(all(node[k] == v for k, v in fields['fields'].items()) for node in model['nodes']) != 1:
                        raise AssertionError('Installed NODE coordinates mismatch')
                    record('installed_dedicated_write_and_idempotency', task_id=submitted['task_id'], result=completed, readback=model)
                    final = run / 'native-installed-result.ansa'
                    await h.direct_result('ansa_model_save', {'path': str(final)})
                    previous = dict(h.model)
                    await h.direct_result('ansa_model_open', {'path': str(final), 'preservation': 'discard'})
                    await h.snapshot_context()
                    if previous == h.model:
                        raise AssertionError('Reopen did not advance model epoch')
                    reopened = second.rpc(info['port'], remote_readback)
                    if reopened['nodes'] != model['nodes'] or reopened['shells'] != model['shells']:
                        raise AssertionError('Saved/reopened model data differ')
                    if Path(reopened['path']).resolve() != final.resolve() or not final.is_file():
                        raise AssertionError('Reopened file path mismatch')
                    record('same_window_save_reopen_independent_abaqus_readback', readback=reopened, model_before=previous, model_after=h.model)
                    visual = await operator(run, 'reopened_graphics')
                    if visual.get('ok') is not True or visual.get('pid') != initial['pid']:
                        raise AssertionError('Graphics observation did not confirm the owned ANSA window')
                    screenshot = visual.get('screenshot')
                    if screenshot is not None:
                        screenshot = Path(screenshot).resolve(strict=True)
                        screenshot.relative_to(run)
                        if screenshot.stat().st_size < 1000:
                            raise AssertionError('Screenshot artifact is empty')
                        report['visual_artifact'] = str(screenshot)
                    elif visual.get('displayed') is True:
                        report['visual_artifact'] = 'inline-computer-use-screenshot'
                    else:
                        raise AssertionError('Graphics observation has no saved or displayed screenshot')
                    report['visual_acceptance'] = True
                    record('owned_window_graphics', observation=visual)
                    await operator(run, 'stop_bridge')
                    stopped = state()
                    if stopped['bridge_present'] and not stopped.get('closed'):
                        raise AssertionError('Stop did not close the installed bridge')
                    await operator(run, 'restart_and_bind')
                    status = await h.call('ansa_connection_status')
                    h.target = status['data']['active_target']
                    if h.target is None:
                        raise AssertionError('Restart binding unavailable')
                    rebound = await h.snapshot_context()
                    restarted = state()
                    installed_origins(restarted)
                    if (rebound['pid'] != restarted['pid']
                            or rebound['pid'] != initial['pid']
                            or not rebound['ui']['alive'] or not rebound['ui']['visible']
                            or h.target['instance_id'] != restarted['instance_id']
                            or h.target['session_id'] != restarted['session_id']):
                        raise AssertionError('Restart target/panel does not belong to the owned bridge')
                    if restarted['session_id'] == entry['session_id']:
                        raise AssertionError('Stopped bridge session was reused')
                    record('installed_stop_restart_rebind', stopped=stopped, restarted=restarted)
        if hashlib.sha256(Path(info['default_xml']).read_bytes()).hexdigest() != info['default_sha256']:
            raise AssertionError('Default profile changed')
        if hashlib.sha256(Path(info['archive']).read_bytes()).hexdigest() != info['archive_sha256']:
            raise AssertionError('Installed archive changed')
        report['success'] = True
    except BaseException as exc:
        report['error'] = type(exc).__name__ + ': ' + str(exc)
        raise
    finally:
        core._json_write(report_path, report)
        grpc.insecure_channel = original
    print(json.dumps({'success': True, 'cases': len(report['cases']), 'report': str(report_path)}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepare', action='store_true')
    parser.add_argument('--ansa-root', type=Path)
    parser.add_argument('--native-install', type=Path)
    parser.add_argument('--external-install', type=Path)
    parser.add_argument('--runtime-home', type=Path)
    parser.add_argument('--run-dir', type=Path)
    args = parser.parse_args()
    if args.prepare:
        if not all((args.ansa_root, args.native_install, args.external_install, args.runtime_home)):
            parser.error('--prepare requires ANSA/native/external/runtime paths')
        prepare(args)
    else:
        if args.run_dir is None:
            parser.error('--run-dir is required for verification')
        asyncio.run(verify(args))
