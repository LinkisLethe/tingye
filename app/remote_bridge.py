"""Relay a manifest's jobs to SSH GPUs and publish only Markdown locally."""
import argparse,json,os,shlex,subprocess,time,uuid
from pathlib import Path
from .common import UserError
from .exporter import export_note
from .settings import atomic_json,data_directory
from .store import Store


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--manifest',type=Path,required=True);args=parser.parse_args()
    from filelock import FileLock, Timeout
    lock=FileLock(str(args.manifest)+'.lock')
    try:lock.acquire(timeout=0)
    except Timeout:
        print('Remote bridge is already running',flush=True)
        return
    manifest=json.loads(args.manifest.read_text(encoding='utf8'))
    host=manifest['host'];key=manifest['key'];remote=manifest['remote_root']
    store=Store(data_directory());local=data_directory()/'remote-work';local.mkdir(exist_ok=True)
    ssh=['ssh','-i',key,'-o','IdentitiesOnly=yes','-o','BatchMode=yes','-o','ConnectTimeout=10','-o','ServerAliveInterval=20',host]
    scp=['scp','-i',key,'-o','IdentitiesOnly=yes','-o','BatchMode=yes','-o','ConnectTimeout=10']
    def run(command):
        proc=subprocess.run(ssh+[command],capture_output=True,text=True,encoding='utf8',timeout=90)
        if proc.returncode:raise RuntimeError(proc.stderr.strip()[:400])
        return proc.stdout
    def control(action,ids):
        cmd=f'cd {shlex.quote(remote)} && {shlex.quote(remote+"/venv/bin/python")} -m app.remote_control {action} '+ ' '.join(shlex.quote(x) for x in ids)
        return json.loads(run(cmd))
    def transfer(source,target):
        proc=subprocess.run(scp+[source,target],capture_output=True,text=True,encoding='utf8',timeout=180)
        if proc.returncode:raise RuntimeError(proc.stderr.strip()[:400])
    def persist():atomic_json(args.manifest,manifest)
    for entry in manifest['jobs']:
        job=store.get(entry['job_id'])
        if entry.get('state')=='delivered' or job['status']=='completed':continue
        if 'run_id' not in entry:
            if job['status'] not in ('paused','interrupted','failed','cancelled'):
                raise RuntimeError('Job is not available for remote claim')
            entry['run_id']=uuid.uuid4().hex;entry['state']='prepared';persist()
        ident=entry['run_id'];fence={'status':'running','attempt_id':ident}
        if job.get('attempt_id')!=ident or job['status']!='running':
            claimed=store.update_if(job['id'],{'status':job['status']},status='running',attempt_id=ident,cancel_requested=False,
                                    execution_backend='ssh',execution_host=host,progress=0,stage='remote_preparing',message=f'准备在远端 GPU {entry["gpu"]} 处理',error=None)
            if claimed is None:raise RuntimeError('Remote claim conflict')
        folder=local/ident;folder.mkdir(exist_ok=True)
        payload={'bvid':job['input'],'gpu':entry['gpu'],'settings':{**job['settings'],'_model_path':remote+'/model'}}
        atomic_json(folder/'job.json',payload)
        run('mkdir -p '+shlex.quote(remote+'/jobs/'+ident))
        transfer(str(folder/'job.json'),host+':'+remote+'/jobs/'+ident+'/job.json')
        control('launch',[ident]);entry['state']='launched';persist()
    while True:
        entries=[e for e in manifest['jobs'] if e.get('state') not in ('delivered','failed','cancelled')]
        if not entries:break
        try:
            states=control('status',[e['run_id'] for e in entries])
            for entry in entries:
                ident=entry['run_id'];job=store.get(entry['job_id']);s=states[ident]
                fence={'status':'running','attempt_id':ident}
                if job.get('cancel_requested'):
                    control('cancel',[ident]);store.update_if(job['id'],fence,status='cancelled',stage='cancelled',message='已请求远端取消')
                    entry['state']='cancelled';persist();continue
                if s['state']=='failed':
                    store.update_if(job['id'],fence,status='failed',stage='failed',error=s.get('error','远端任务失败'),message='远端处理未完成')
                    entry['state']='failed';persist();continue
                if s['state']=='cancelled':
                    store.update_if(job['id'],fence,status='cancelled',stage='cancelled',message='远端任务已取消')
                    entry['state']='cancelled';persist();continue
                if s['state']=='completed':
                    folder=local/ident;result_path=folder/'result.json'
                    store.update_if(job['id'],fence,progress=99,stage='saving',message='正在取回转写并保存到 Obsidian')
                    transfer(host+':'+remote+'/jobs/'+ident+'/result.json',str(result_path))
                    result=json.loads(result_path.read_text(encoding='utf8'))
                    current=store.get(job['id'])
                    if current['status']!='running' or current.get('attempt_id')!=ident:raise RuntimeError('Remote result ownership changed')
                    def check_cancel():
                        current=store.get(job['id'])
                        if current.get('cancel_requested') or current.get('attempt_id')!=ident:raise UserError('任务已取消')
                    saved=export_note(result['video'],result['transcript'],None,job['settings'],check_cancel)
                    t=result['transcript'];metadata=t.get('metadata') or {}
                    saved.update(execution_host=host,gpu=entry['gpu'],duration_seconds=t.get('duration'),processing_seconds=metadata.get('elapsed_seconds'),
                                 last_timestamp=max((x['end'] for x in t['segments']),default=0),device=metadata.get('device'),asr_model=t.get('model'))
                    done=store.update_if(job['id'],fence,status='completed',stage='completed',progress=100,title=result['video']['title'],result=saved,
                                         message='远端转写完成，已保存到 Obsidian',error=None)
                    if done is None:raise RuntimeError('Cannot commit remote result')
                    entry.update(state='delivered',result=saved);persist()
                    control('ack',[ident]);result_path.unlink(missing_ok=True);(folder/'job.json').unlink(missing_ok=True);folder.rmdir()
                else:
                    message=f'远端 GPU {entry["gpu"]} · '+s.get('message','正在准备')
                    store.update_if(job['id'],fence,title=s.get('title',job.get('title','')),progress=min(98,s.get('progress',0)*.98),stage='remote_transcription',message=message)
            print(json.dumps({e['job_id']:e['state'] for e in manifest['jobs']}),flush=True)
        except Exception as exc:
            print('REMOTE_RETRY '+str(exc),flush=True)
        time.sleep(5)
    print('REMOTE_BATCH_FINISHED',flush=True)


if __name__=='__main__':main()
