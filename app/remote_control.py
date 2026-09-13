"""Idempotent process control inside a dedicated SSH worker directory."""
import argparse,json,os,re,subprocess,sys,time
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent


def folder(ident):
    if not re.fullmatch(r'[0-9a-f]{32}',ident):raise ValueError('Invalid run ID')
    return ROOT/'jobs'/ident


def status(ident):
    path=folder(ident)/'status.json'
    if path.exists():return json.loads(path.read_text(encoding='utf8'))
    if (folder(ident)/'launch.json').exists():return {'state':'starting'}
    return {'state':'missing'}


def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=['launch','status','cancel','ack']);p.add_argument('ids',nargs='+');a=p.parse_args()
    result={}
    for ident in a.ids:
        root=folder(ident)
        if a.action=='launch':
            s=status(ident)
            if s['state']=='missing':
                config=json.loads((root/'job.json').read_text(encoding='utf8'))
                gpu=int(config['gpu'])
                if not 0<=gpu<16:raise ValueError('Invalid GPU')
                libs=list((ROOT/'venv/lib').glob('python*/site-packages/nvidia/*/lib'))
                env={**os.environ,'PYTHONUNBUFFERED':'1','CUDA_VISIBLE_DEVICES':str(gpu),
                     'LD_LIBRARY_PATH':':'.join(str(x) for x in libs)+':'+os.environ.get('LD_LIBRARY_PATH','')}
                with (root/'worker.log').open('ab') as log:
                    proc=subprocess.Popen([sys.executable,'-m','app.remote_worker','--job',str(root/'job.json'),'--gpu',str(gpu)],cwd=ROOT,
                                          stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,start_new_session=True,env=env)
                (root/'launch.json').write_text(json.dumps({'pid':proc.pid,'created':time.time()}),encoding='utf8')
                s={'state':'starting','pid':proc.pid}
            result[ident]=s
        elif a.action=='cancel':
            (root/'cancel').touch();result[ident]={'cancel_requested':True}
        elif a.action=='ack':
            s=status(ident)
            if s['state']=='completed':
                (root/'result.json').unlink(missing_ok=True)
                s['delivered']=True
                temporary=root/'status.tmp';temporary.write_text(json.dumps(s,ensure_ascii=False),encoding='utf8');temporary.replace(root/'status.json')
            result[ident]=s
        else:result[ident]=status(ident)
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
