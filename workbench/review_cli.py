"""Client for the already-running local workbench; never starts a server.

Examples:
  python -m workbench.review_cli check --run --wait
  python -m workbench.review_cli run --validation --wait
  python -m workbench.review_cli finish REVIEW_ID --input analysis.json
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path
from urllib.error import HTTPError,URLError
from urllib.request import Request,HTTPRedirectHandler,ProxyHandler,build_opener

BASE='http://127.0.0.1:8765'


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):raise URLError('redirect refused')


def request(method,path,payload=None):
    if not isinstance(path,str) or not path.startswith('/api/') or '://' in path or '\\' in path:
        raise ValueError('only local API paths are supported')
    headers={'Origin':BASE,'X-Workbench-Request':'1','Accept':'application/json'}
    data=None
    if payload is not None:
        data=json.dumps(payload,ensure_ascii=False,allow_nan=False).encode();headers['Content-Type']='application/json'
    req=Request(BASE+path,data=data,headers=headers,method=method)
    # Avoid both proxy routing and redirects away from the fixed local service.
    with build_opener(ProxyHandler({}),NoRedirects()).open(req,timeout=30) as response:
        raw=response.read(2_000_001)
    if len(raw)>2_000_000:raise ValueError('response too large')
    result=json.loads(raw)
    if not isinstance(result,dict):raise ValueError('invalid API response')
    return result


def identity(value):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',value):raise ValueError('invalid review id')
    return value


def wait_for_review(review_id,timeout=300):
    deadline=time.monotonic()+timeout
    while True:
        record=request('GET','/api/reviews/'+identity(review_id))
        if record.get('state') not in ('collecting','applying'):return record
        if time.monotonic()>=deadline:return {'id':review_id,'state':'timeout'}
        time.sleep(min(1,max(0,deadline-time.monotonic())))


def display(record):
    public={k:record[k] for k in ('id','date','validation','state','phase','error_code','due','due_at','reason','capability_validation','local_report_path') if k in record}
    if record.get('id') and record.get('state') in ('ready','completed','attention') and ('report' in record or record.get('phase')!='collection'):
        public['report']=request('GET','/api/reviews/'+identity(record['id'])+'/anonymous')
    result=record.get('policy_result')
    if isinstance(result,dict):public['policy_result']={k:result[k] for k in ('version','last_action','action') if k in result}
    return public


def _finish_input(path):
    if path=='-':text=sys.stdin.read(65_537)
    else:
        with Path(path).open(encoding='utf-8') as stream:text=stream.read(65_537)
    if len(text)>65_536:raise ValueError('analysis input too large')
    value=json.loads(text)
    if not isinstance(value,dict) or set(value)-{'findings','suggestions','changes','expected_version'}:
        raise ValueError('only findings, suggestions and bounded policy changes are accepted')
    return value


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='command',required=True)
    check=commands.add_parser('check');check.add_argument('--run',action='store_true');check.add_argument('--wait',action='store_true');check.add_argument('--timeout',type=float,default=300)
    run=commands.add_parser('run');run.add_argument('--date');run.add_argument('--validation',action='store_true');run.add_argument('--wait',action='store_true');run.add_argument('--timeout',type=float,default=300)
    for command in ('status','wait','anonymous'):
        sub=commands.add_parser(command);sub.add_argument('review_id')
        if command=='wait':sub.add_argument('--timeout',type=float,default=300)
    finish=commands.add_parser('finish');finish.add_argument('review_id');finish.add_argument('--input',required=True)
    args=parser.parse_args(argv)
    try:
        if hasattr(args,'timeout') and not 0<args.timeout<=3600:raise ValueError('invalid wait timeout')
        if args.command=='check':
            record=request('POST','/api/reviews/check',{})
            if record.get('due') is False and not record.get('id'):record={'state':'noop',**record}
            elif args.run and record.get('due') is True and not record.get('id'):
                record=request('POST','/api/reviews/run',{'target_date':record['date'],'validation':False})
            if args.wait and record.get('id'):record=wait_for_review(record['id'],args.timeout)
            output=display(record)
        elif args.command=='run':
            record=request('POST','/api/reviews/run',{'target_date':args.date,'validation':args.validation})
            if args.wait and record.get('id'):record=wait_for_review(record['id'],args.timeout)
            output=display(record)
        elif args.command=='anonymous':
            output=request('GET','/api/reviews/'+identity(args.review_id)+'/anonymous')
        elif args.command=='finish':
            record=request('POST','/api/reviews/'+identity(args.review_id)+'/finish',_finish_input(args.input))
            output=display(record)
        else:
            record=(wait_for_review(identity(args.review_id),args.timeout) if args.command=='wait'
                    else request('GET','/api/reviews/'+identity(args.review_id)))
            output=display(record)
        print(json.dumps(output,ensure_ascii=False,allow_nan=False))
        return 2 if output.get('state') in ('failed','attention','timeout') else 0
    except HTTPError as exc:
        print(json.dumps({'state':'unavailable','error':'local_api_error','status':exc.code}));return 2
    except (URLError,ConnectionError,TimeoutError,OSError):
        print(json.dumps({'state':'unavailable','error':'local_workbench_unavailable'}));return 2
    except (ValueError,KeyError,TypeError):
        print(json.dumps({'state':'invalid','error':'invalid_review_request'}));return 2


if __name__=='__main__':raise SystemExit(main())
