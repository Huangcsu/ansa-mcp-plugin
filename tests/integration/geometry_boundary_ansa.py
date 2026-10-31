"""Controlled auxiliary CONS conversion through real MCP in a new owned ANSA."""
import asyncio
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import traceback
import uuid

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'src'),str(Path(__file__).resolve().parent)]
import stage_three_ansa as stage
from geometry_features_ansa import FIXTURE, ORACLE


def original_rows(oracle, auxiliary_part_id, new_curves=()):
    """Independent native card/ownership check with explicit additions only."""
    rows=[]
    for row in oracle['rows']:
        if (row['type']=='CURVE' and row['id'] in new_curves) or (row['type']=='ANSAPART' and row['id']==auxiliary_part_id):
            continue
        # Native Part edit timestamps change when curves are temporarily created in
        # the source Part; all definition, geometry and assignment fields remain.
        rows.append({**row,'fields':{k:v for k,v in row['fields'].items()
                                   if not (row['type']=='ANSAPART' and k=='Last Edit')}})
    return rows


async def verify(run,instructions,identity,helpers,report):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    params=StdioServerParameters(command=sys.executable,
        args=['-B','-m','ansa_mcp_plugin','--runtime-home',instructions['runtime_home']],
        env={**os.environ,'PYTHONPATH':str(ROOT/'src'),'PYTHONDONTWRITEBYTECODE':'1'})
    path=run/'geometry-boundary-report.json'
    with (run/'geometry-boundary.stderr.log').open('w',encoding='utf-8') as err:
        async with stdio_client(params,errlog=err) as (reader,writer):
            async with ClientSession(reader,writer,read_timeout_seconds=timedelta(seconds=60)) as client:
                await client.initialize()
                h=stage.McpHarness(client,report,run)
                report['schema']=await h.list_schema()
                assert report['schema']['count']==83
                await stage._bind_for_run(h,identity,*helpers[:2],identity['port'])
                async def call(name,args,expected='succeeded'):
                    op='geometry-boundary-'+uuid.uuid4().hex
                    report['pending']={'tool':name,'operation_id':op}
                    stage._json_write(path,report)
                    start=time.monotonic()
                    submitted,completed=await h.task(name,args,operation_id=op,expected=expected)
                    value=completed['data']['result'] if expected=='succeeded' else completed
                    report['calls'].append({'tool':name,'operation_id':op,'task_id':submitted['task_id'],
                        'state':completed['status'],'elapsed_seconds':time.monotonic()-start,'result':value})
                    report['pending']=None;stage._json_write(path,report)
                    return value
                async def script(code):
                    return (await call('ansa_script_execute',{'script':code,'validate':True}))['result']
                fixture=await script(FIXTURE)
                aux=(await call('ansa_part_create',{'deck':1,'name':'controlled-boundary-curves'}))['entity']['id']
                source=run/'geometry-boundary-source.ansa'
                await call('ansa_model_save',{'path':str(source)})
                await call('ansa_model_open',{'path':str(source),'preservation':'backup','backup_path':str(run/'before-source-open.ansa')})
                await h.snapshot_context()
                before=await script(ORACLE)
                scope={'deck':1,'face_ids':fixture['face_ids'],'curve_ids':[]}
                descriptor=await call('ansa_geometry_features',scope)
                page=await call('ansa_mesh_evidence_page',{'deck':1,'report_id':descriptor['report_id'],
                    'section':'boundaries','offset':0,'limit':100})
                cons_ids=sorted(row['id'] for row in page['rows'])
                assert len(cons_ids)==6
                arguments={'deck':1,'face_ids':fixture['face_ids'],'cons_ids':cons_ids,
                    'auxiliary_part_id':aux,'expected_source_snapshot_sha256':'0'*64,
                    'backup_path':str(run/'bad-source-must-not-exist.ansa')}
                rejected=await call('ansa_geometry_boundary_prepare',arguments,expected='failed')
                assert 'source changed' in json.dumps(rejected).lower()
                assert not Path(arguments['backup_path']).exists()
                assert before==await script(ORACLE)
                report['cases'].append({'name':'stale_source_rejected_before_write','result':rejected})
                arguments.update(expected_source_snapshot_sha256=descriptor['source_snapshot_sha256'],
                                 backup_path=str(run/'before-boundary-conversion.ansa'))
                result=await call('ansa_geometry_boundary_prepare',arguments)
                assert result['ok'] and result['scope_verified'] and result['provenance_stage']=='verified'
                assert result['source_snapshot_sha256']==result['source_after_sha256']==descriptor['source_snapshot_sha256']
                assert result['summary']['provenance_complete'] and result['summary']['cons_to_curves_calls']==6
                ids=result['new_curve_ids'];assert len(ids)==6 and len(set(ids))==6
                assert sorted(row['cons_id'] for row in result['mappings'])==cons_ids
                supported=[row for row in result['mappings'] if row['analysis']['status']=='supported']
                assert len(supported)==2 and all(row['analysis']['kind']=='line' for row in supported)
                assert all(row['endpoint_check']['status']=='matched' for row in supported)
                assert all(row['source_geometry_equivalence']=='not_proven' for row in result['mappings'])
                audits=[row['conversion_fidelity'] for row in result['mappings']]
                assert all(row['status']=='sampled_only' and row['valid_sample_count']==9 for row in audits)
                assert all(row['continuous_equivalence_proven'] is False and row['reverse_coverage_proven'] is False for row in audits)
                assert result['summary']['curve_evaluation_calls']==54 and result['summary']['cons_projection_calls']==54
                assert all(row['conversion_fidelity']['maximum_observed_distance']<1e-10 for row in supported)
                assert any(row['maximum_observed_distance']>1e-7 for row in audits),'fixture must expose nonzero conversion deviation'
                assert result['geometry_status']=='incomplete' and result['usable_for_mutation'] is False
                after=await script(ORACLE)
                assert original_rows(before,aux)==original_rows(after,aux,ids),'original native definitions changed'
                added=[row for row in after['rows'] if row['type']=='CURVE' and row['id'] in ids]
                assert len(added)==6 and all(row['part_id']==aux for row in added)
                report['cases'].append({'name':'one_to_one_provenance_original_scope_and_analytic_limits',
                    'result':result,'before':before,'after':after})
                report['cases'].append({'name':'native_curve_to_original_cons_sampled_deviations',
                    'audits':audits,'continuous_equivalence_proven':False})
                arguments['backup_path']=str(run/'populated-part-must-not-exist.ansa')
                rejected=await call('ansa_geometry_boundary_prepare',arguments,expected='failed')
                assert 'must be empty' in json.dumps(rejected).lower()
                assert not Path(arguments['backup_path']).exists()
                assert after==await script(ORACLE)
                report['cases'].append({'name':'populated_auxiliary_part_rejected_before_write','result':rejected})
                saved=run/'geometry-boundary-final.ansa'
                await call('ansa_model_save',{'path':str(saved)})
                saved_oracle=await script(ORACLE)
                await call('ansa_model_open',{'path':str(saved),'preservation':'backup','backup_path':str(run/'before-final-open.ansa')})
                await h.snapshot_context()
                assert saved_oracle==await script(ORACLE)
                reopened=await call('ansa_geometry_features',scope)
                # Snapshot hashes bind database path/epoch, so a reopened model
                # requires a new witness even when native geometry is identical.
                assert reopened['source_snapshot_sha256']!=descriptor['source_snapshot_sha256']
                reopened_boundaries=await call('ansa_mesh_evidence_page',{'deck':1,'report_id':reopened['report_id'],
                    'section':'boundaries','offset':0,'limit':100})
                assert reopened_boundaries['rows']==page['rows']
                curve_scope={'deck':1,'face_ids':[],'curve_ids':[row['curve_id'] for row in supported],
                             'seed_options':{'target_length':1.,'max_chord_error':.02}}
                lines=await call('ansa_geometry_features',curve_scope)
                assert lines['summary']['seed_status']=='complete'
                report['cases'].append({'name':'save_reopen_independent_readback',
                    'saved_model':str(saved),'native_fingerprint':saved_oracle['fingerprint'],
                    'face_features':reopened,'explicit_auxiliary_lines':lines})
                stage._json_write(path,report)


def main():
    if len(sys.argv)!=2:raise ValueError('usage: geometry_boundary_ansa.py <new-owned-run-dir>')
    run=Path(sys.argv[1]).resolve();path=run/'geometry-boundary-report.json'
    if path.exists():raise FileExistsError('refuse replay of a prior native validation')
    instructions=json.loads((run/'launch_instructions.json').read_text(encoding='utf-8'))
    report={'complete':False,'cases':[],'calls':[],'pending':None}
    sources=[Path(__file__).resolve(),*(ROOT/'src'/p for p in (
        'ansa_mcp_bridge/operations/geometry_boundary.py','ansa_mcp_bridge/operations/geometry_features.py',
        'ansa_mcp_bridge/operations/geometry_fidelity.py',
        'ansa_mcp_bridge/operations/geometry_features_iges.py','ansa_mcp_bridge/operations/geometry_contours.py',
        'ansa_mcp_bridge/operations/structured_meshing.py','ansa_mcp_bridge/operations/structured_native.py',
        'ansa_mcp_plugin/mcp/structured_meshing.py','ansa_mcp_shared/operations.py'))]
    def hashes():return {p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}
    report['source_sha256']=hashes();helpers=None;grpc=None
    try:
        stage.PANEL_WAIT_SECONDS=60
        identity,_,grpc,helpers=stage._prepare_owned_ansa(run,instructions)
        report['identity']=identity;stage._json_write(path,report)
        asyncio.run(verify(run,instructions,identity,helpers,report))
        assert report['source_sha256']==hashes(),'source changed during native validation'
        report['complete']=True
        report['cleanup']=stage._cleanup_owned_ansa(identity,instructions,helpers)
    except Exception:
        report['error']=traceback.format_exc();print(report['error'],flush=True)
    finally:
        stage._json_write(path,report)
        if grpc is not None and helpers is not None:grpc.insecure_channel=helpers[2]
    return 0 if report['complete'] and report.get('cleanup',{}).get('stopped_owned_process') else 1


if __name__=='__main__':raise SystemExit(main())
