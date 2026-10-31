"""One-shot owned native validation of the recoverable public MCP client job."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import traceback
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests/integration')]
import stage_three_ansa as stage
from map_block_batch_ansa import harness
from rectangular_candidates_ansa import SNAPSHOT
from ansa_mcp_plugin.workflows import MapBlockJobClient


class ClientInterrupted(BaseException):
    """Simulate loss of only the orchestration client after a real native fill."""


async def verify(run, cfg, identity, helpers, report, resume_prepared=False):
    out = run / report.get('receipt_name', 'map-block-job-report.json')
    async with harness(run, cfg, identity, helpers, report) as h:
        async def call(name, fields):
            op = 'job-native-' + uuid.uuid4().hex
            report['pending'] = {'tool': name, 'operation_id': op}
            stage._json_write(out, report)
            sub = await h.call(name, {'operation_id': op, 'expected_target': h.target,
                                     'expected_model': h.model, **fields}, allow_error=True)
            if not sub.get('task_id'):
                raise RuntimeError(str(sub))
            report['pending']['task_id'] = sub['task_id']
            stage._json_write(out, report)
            done = await h.wait_task(sub['task_id'], None, 55)
            report['calls'].append({'tool': name, 'operation_id': op,
                                    'task_id': sub['task_id'], 'result': done})
            report['pending'] = None
            stage._json_write(out, report)
            print(json.dumps({'tool': name, 'status': done['status']}), flush=True)
            if done['status'] != 'succeeded':
                raise RuntimeError(str(done.get('error')))
            return done['data']['result']

        async def script(code):
            result = await call('ansa_script_execute', {'script': code, 'validate': True})
            if result.get('result_paged'):
                raise RuntimeError('query the original paged task; do not repeat')
            return result['result']

        async def snapshot(label):
            return await script('SNAPSHOT_PATH=' + repr(str(run / (label + '.json'))) + '\n' + SNAPSHOT)

        # Re-run preparation once on the explicit raw CAD copy using the proof-reuse code.
        if not resume_prepared:
            await call('ansa_model_open', {'path': str(run / 'before-map-blocks.ansa'),
                       'preservation': 'backup', 'backup_path': str(run / 'before-job-raw-open.ansa')})
            await h.snapshot_context()
            prepare = {'deck': 1, 'face_groups': report['face_groups'], 'target_length': 5., 'property_id': 2}
            desc = await call('ansa_mesh_map_blocks_prepare', {'action': 'plan', **prepare})
            page = await call('ansa_mesh_evidence_page', {'deck': 1, 'report_id': desc['report_id'],
                             'section': 'plan_sha256', 'offset': 0, 'limit': 1})
            report['prepare'] = await call('ansa_mesh_map_blocks_prepare', {
                'action': 'apply', **prepare, 'expected_plan_sha256': page['rows'][0],
                'backup_path': str(run / 'before-job-preparation.ansa')})
            if not report['prepare'].get('ok') or not report['prepare'].get('scope_verified'):
                raise RuntimeError('optimized native prepare not verified')
            await call('ansa_model_save', {'path': str(run / 'job-prepared.ansa')})
            report['prepared'] = await snapshot('job-prepared-snapshot')
        else:
            if report.get('pending') or (run / 'public-map-block-job').exists():
                raise RuntimeError('only a pre-job binding refusal can use prepared continuation')
            original = next(c for c in report['calls'] if c['tool']=='ansa_mesh_map_blocks_prepare'
                            and c['result']['data']['result'].get('scope_verified'))
            recovered = await h.call('ansa_task_query', {'task_id': original['task_id']})
            if recovered['status'] != 'succeeded' or recovered['operation_id'] != original['operation_id']:
                raise RuntimeError('original prepare not confirmed succeeded')
            report['original_prepare_query'] = recovered
            current = await snapshot('job-prepared-continuation-readback')
            if current['scope_sha256'] != report['prepared']['scope_sha256']:
                raise RuntimeError('prepared native model changed; no write replay allowed')
        envelope = await h.call('ansa_model_snapshot', {'expected_target': h.target})
        job_target = envelope['target']

        interrupted = False
        first_apply_operation = None
        apply_operations = []
        queries = []

        async def adapter(name, args):
            nonlocal interrupted, first_apply_operation
            if name == 'ansa_mesh_map_block_fill' and args.get('action') == 'apply':
                apply_operations.append(args['operation_id'])
                first_apply_operation = first_apply_operation or args['operation_id']
            reply = await h.call(name, args, allow_error=True)
            if name == 'ansa_task_query':
                queries.append({'request': args, 'status': reply.get('status'),
                                'operation_id': reply.get('operation_id'), 'task_id': reply.get('task_id')})
            report['job_transport'] = {'apply_operations': apply_operations, 'queries': queries}
            # Lose the return on the first successful original native apply, before the
            # workflow can persist it. The server retains the original terminal result.
            if (not interrupted and reply.get('operation_id') == first_apply_operation
                    and reply.get('status') == 'succeeded'):
                interrupted = True
                report['interrupted_original'] = reply
                stage._json_write(out, report)
                raise ClientInterrupted()
            stage._json_write(out, report)
            return reply

        profile = {'analysis_type': 'linear', 'element_family': 'solid', 'criteria': [
            {'name': 'ASPECT', 'max': 5.}, {'name': 'JACOB', 'min': .2},
            {'name': 'MINL', 'min': 4.5}, {'name': 'MAXL', 'max': 5.5}]}
        directory = run / 'public-map-block-job'
        client = MapBlockJobClient(adapter, poll_interval=.1, wait_timeout_seconds=55)
        try:
            await client.run(deck=1, fill_groups=[[1], [2]], quality_profile=profile,
                             job_dir=directory, expected_target=job_target, expected_model=h.model)
        except ClientInterrupted:
            pass
        if not interrupted:
            raise RuntimeError('planned client interruption did not happen')
        manifest_path = directory / 'manifest.json'
        manifest = json.loads(manifest_path.read_text('utf-8'))
        report['manifest_at_interruption'] = manifest
        recovered = MapBlockJobClient(adapter, poll_interval=.1, wait_timeout_seconds=55)
        result = await recovered.resume(manifest_path, expected_target=job_target, expected_model=h.model)
        report['job_result'] = result
        report['final_manifest'] = json.loads(manifest_path.read_text('utf-8'))
        if result['status'] != 'complete' or len(apply_operations) != 2 or len(set(apply_operations)) != 2:
            raise RuntimeError('job failed or repeated an original native apply')
        if not any(q['operation_id'] == first_apply_operation and q['status'] == 'succeeded' for q in queries):
            raise RuntimeError('recovery did not query the original apply result')
        report['final'] = await snapshot('job-final-snapshot')
        checkpoint = directory / 'checkpoints' / 'group-02.ansa'
        await call('ansa_model_open', {'path': str(checkpoint), 'preservation': 'backup',
                   'backup_path': str(run / 'before-job-final-reopen.ansa')})
        await h.snapshot_context()
        report['reopened'] = await snapshot('job-reopened-snapshot')
        before = json.loads(Path(report['final']['path']).read_text('utf-8'))['snapshot']
        after = json.loads(Path(report['reopened']['path']).read_text('utf-8'))['snapshot']
        report['save_reopen_raw_snapshot_exact'] = before == after
        if before != after:
            raise RuntimeError('job checkpoint raw readback differs')
        report['complete'] = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run_dir', type=Path)
    parser.add_argument('--resume-prepared', action='store_true')
    args = parser.parse_args()
    run = args.run_dir.resolve()
    out = run / ('map-block-job-resume-report.json' if args.resume_prepared else 'map-block-job-report.json')
    if out.exists():
        raise FileExistsError('query original job/task; never replay this one-shot entry')
    prior = json.loads((run / 'map-block-batch-report.json').read_text('utf-8'))
    cfg = json.loads((run / 'launch_instructions.json').read_text('utf-8'))
    if not prior['complete'] or prior['pending']:
        raise RuntimeError('completed owned baseline required')
    identity = prior['identity']
    report = {'identity': identity, 'face_groups': prior['groups']['face_groups'],
              'complete': False, 'pending': None, 'calls': []}
    if args.resume_prepared:
        report = json.loads((run / 'map-block-job-report.json').read_text('utf-8'))
        if (report['complete'] or report['pending'] or report.get('job_transport')
                or 'expected_target must contain only' not in report.get('error', '')):
            raise RuntimeError('continuation requires the recorded pre-dispatch binding refusal')
        report['prior_error'] = report.pop('error')
        report['receipt_name'] = out.name
    import grpc
    from ansa_mcp_shared.identity import same_process_alive
    one, two = stage._import_runtime_helpers()
    original = stage._bounded_grpc(grpc, one.DeadlineInterceptor)
    helpers = one, two, original
    try:
        if not same_process_alive(identity['pid'], identity['process_start']):
            raise RuntimeError('owned process changed')
        basic = two.rpc(identity['port'], one.remote_basic)
        stage._verify_program_arguments(basic, Path(cfg['translator']), identity['port'])
        if basic['pid'] != identity['pid'] or basic['program_args'] != identity['program_args']:
            raise RuntimeError('owned identity changed')
        asyncio.run(verify(run, cfg, identity, helpers, report, args.resume_prepared))
    except Exception:
        report['error'] = traceback.format_exc()
        print(report['error'], flush=True)
    finally:
        stage._json_write(out, report)
        grpc.insecure_channel = original
    print(json.dumps({'complete': report['complete'], 'report': str(out)}), flush=True)
    return 0 if report['complete'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
