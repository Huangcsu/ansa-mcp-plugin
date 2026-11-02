"""One source-mode public MCP run of the finite native plate-hole recipe."""
from pathlib import Path
import json,sys,uuid

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'src'),str(Path(__file__).parent)]
import annular_map_fill_ansa as lifecycle
import stage_three_ansa as stage
from ansa_runtime_helpers import rpc
from map_block_batch_ansa import harness
from ansa_mcp_plugin.workflows import plate_hole_script
from ansa_mcp_shared.plate_hole import build_plan


def load_recipe(raw):
    text=raw if raw.lstrip().startswith('{') else Path(raw).read_text(encoding='utf-8')
    return build_plan(json.loads(text))['params']


def remote_oracle(image_path=None):
    import hashlib,json,threading
    from ansa import base,constants,mesh,utils
    from ansa_mcp_bridge.operations import structured_native as native
    if threading.current_thread() is not threading.main_thread():raise RuntimeError('oracle main thread required')
    d=base.CurrentDeck()
    shells=base.CollectEntities(d,None,'SHELL');solids=base.CollectEntities(d,None,'SOLID')
    se=native._read_elements(base,d,'SHELL',sorted(e._id for e in shells),{'CQUAD4','CTRIA3'})
    ve=native._read_elements(base,d,'SOLID',sorted(e._id for e in solids),{'CHEXA','CPENTA'})
    nodes=native._read_grids(base,d,{n for e in se+ve for n in e['nodes']})
    raw=json.dumps({'surface':se,'solid':ve,'nodes':nodes},sort_keys=True,separators=(',',':')).encode()
    frozen={}
    for e in shells:
        names=[f for f in e.card_fields(d) if 'frozen' in f.casefold()]
        values=base.GetEntityCardValues(d,e,names) if names else {}
        frozen[str(e._id)]=values
    result={'mesh_sha256':hashlib.sha256(raw).hexdigest(),'shell_count':len(se),'solid_count':len(ve),
        'grid_count':len(nodes),'types':sorted({e['type'] for e in ve}),
        'volume_ids':sorted(v._id for v in base.CollectEntities(d,None,'VOLUME')),
        'freeze_sha256':hashlib.sha256(json.dumps(frozen,sort_keys=True).encode()).hexdigest()}
    if image_path:
        base.SetCurrentMenu('VOLUME MESH');base.SetViewAngles(f_key='F10');base.ZoomAll();base.RedrawAll()
        result['snapshot_status']=utils.SnapShot(image_path,image_format='PNG',image_size=(1440,1000))
    return result


async def verify(run,cfg,identity,helpers,r,input_model,recipe,original_terminal=None):
    async with harness(run,cfg,identity,helpers,r) as h:
        async def task(name,fields):
            op='plate-hole-public-'+uuid.uuid4().hex
            r['pending']={'tool':name,'operation_id':op};stage._json_write(run/'report.json',r)
            sub=await h.call(name,{'operation_id':op,'expected_target':h.target,'expected_model':h.model,**fields},allow_error=True)
            if not sub.get('task_id'):raise RuntimeError(str(sub))
            r['pending']['task_id']=sub['task_id'];stage._json_write(run/'report.json',r)
            done=await h.wait_task(sub['task_id'],None,900)
            receipt=name+'-'+str(len(r['calls']))+'.json';stage._json_write(run/receipt,done)
            r['calls'].append({'tool':name,'operation_id':op,'task_id':sub['task_id'],'status':done.get('status'),'receipt':receipt})
            if done.get('status') in {'succeeded','failed','cancelled','expired'}:r['pending']=None
            stage._json_write(run/'report.json',r)
            print(name,done.get('status'),flush=True)
            if done.get('status')!='succeeded':raise RuntimeError(str(done.get('error')))
            return done['data']['result']
        async def script(code,*,allow_model_change=False):
            value=await task('ansa_script_execute',{'script':code,'validate':True,'ttl_seconds':900,
                'allow_model_change':allow_model_change})
            if value.get('result_paged') or type(value.get('result')) is not dict:
                raise RuntimeError('incomplete script result; query original task/evidence, never replay')
            return value['result']
        if original_terminal is not None:
            original=await h.call('ansa_task_query',{'task_id':original_terminal['task_id']},allow_error=True)
            r['confirmed_original_task']=original;stage._json_write(run/'report.json',r)
            if original.get('status')!=original_terminal['status'] or original.get('operation_id')!=original_terminal['operation_id']:
                raise RuntimeError('original terminal task identity/status changed; do not replay')
        if input_model is not None:
            await task('ansa_model_open',{'path':str(input_model),'preservation':'backup','backup_path':str(run/'before-open.ansa')})
        await h.snapshot_context()
        r['recipe']=build_plan(recipe)
        plan=await script(plate_hole_script('plan',recipe));r['plan']=plan
        stage._json_write(run/'report.json',r)
        sha=plan.get('plan_sha256')
        if type(sha) is not str or len(sha)!=64:raise RuntimeError('model-bound native plan digest is absent')
        output=run/'plate-hole-workflow.ansa'
        r['apply_started']=True;stage._json_write(run/'report.json',r)
        try:
            applied=await script(plate_hole_script('apply',recipe,backup_path=str(run/'before-plate.ansa'),
                output_path=str(output),expected_plan_sha256=sha),allow_model_change=True)
        except BaseException:
            r['keep_owned_instance']=True
            raise
        r['apply']=applied;stage._json_write(run/'report.json',r)
        if applied.get('ok') is not True:
            r['keep_owned_instance']=bool(applied.get('recovery_required'))
            raise RuntimeError('native plate workflow failed: '+str(applied))
        await h.snapshot_context()
        before=rpc(identity['port'],remote_oracle)
        await task('ansa_model_open',{'path':str(output),'preservation':'backup','backup_path':str(run/'before-reopen.ansa')})
        await h.snapshot_context()
        after=rpc(identity['port'],remote_oracle,str(run/'plate-hole-workflow.png'))
        r['oracle_before']=before;r['oracle_after']=after
        r['independent_readback_exact']=all(before[k]==after[k] for k in before)
        r['quality_accepted']=applied.get('accepted',applied.get('quality_accepted')) is True
        r['complete']=(applied.get('ok') is True and r['independent_readback_exact']
            and after['solid_count']==r['recipe']['predicted_solids'] and after['types']==['CHEXA'])
        r['acceptance_complete']=r['complete'] and r['quality_accepted']
        stage._json_write(run/'report.json',r)


if __name__=='__main__':
    raise SystemExit(lifecycle.main(verify=verify,run_prefix='plate-hole-public-',options_loader=load_recipe,
        options_help='JSON object/file containing the finite plate-hole recipe'))
