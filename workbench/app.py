import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI,HTTPException,Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse,JSONResponse,Response
from fastapi.staticfiles import StaticFiles

from .browser import Browser,NeedsAttention
from .engine import Engine
from .llm import LLM
from .model_config import ModelConfig,load_model_values,save_model_values
from .parsing import select_items
from .schemas import Collection,DOMAINS,DraftEdit,GenerateRequest,Generation,ImportRequest,Schedule,PublishingSettings,PublicationConfirmation,PublishingKeywords,PublicationStage
from .store import Store
from .publication_ledger import PublicationLedger
from .publication_guard import PublicationGuard,digest
from .toutiao_publisher import ToutiaoPublisher
from .micro_runtime import MicroRuntime,DEFAULT_KEYWORDS
from .ai_tools import activate_profile

ROOT=Path(__file__).resolve().parent.parent


def create_app(data_dir=None,browser=None,llm=None,publisher=None):
    load_dotenv(ROOT/'.env')
    data_dir=Path(data_dir or os.environ.get('WORKBENCH_DATA_DIR',str(ROOT/'data')))
    data_dir.mkdir(parents=True,exist_ok=True)
    data_dir.chmod(0o700)
    store=Store(data_dir/'workbench.db')
    activate_profile(store)
    if store.setting('publishing',None) is None:
        store.set_setting('publishing',{'enabled':True})
    caps=json.loads((ROOT/'docs/capabilities.json').read_text())
    browser=browser or Browser(data_dir/'browser')
    llm=llm or LLM(*load_model_values(ROOT,data_dir))
    engine=Engine(store,browser,llm,caps)
    publications=PublicationLedger(store)
    guard=PublicationGuard(publications,getattr(llm,"judge_duplicate",None),
        ready=lambda account: bool(account) and store.setting("publishing_account",{}).get("id")==account
            and not store.setting("publication_import_error",False),
        expected_ai=lambda:store.setting("publishing",{}).get("ai_declaration",False))
    publisher=publisher or ToutiaoPublisher(browser,
        lambda:store.setting("publishing_account",{}).get("id",""),
        expected_ai=lambda:store.setting("publishing",{}).get("ai_declaration",False))
    micro=MicroRuntime(store,browser,llm,guard,publisher)

    @asynccontextmanager
    async def lifespan(app):
        publications.recover()
        import_verified_test()
        await engine.start()
        await micro.start()
        try:
            yield
        finally:
            await micro.close()
            await engine.close()

    app=FastAPI(title='头条内容工作台',lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
    app.state.engine=engine
    app.state.publications=publications
    app.state.publication_guard=guard
    app.state.micro_runtime=micro

    def import_verified_test():
        preview_path=data_dir/"publishing-test-preview.json"
        receipt_path=data_dir/"publishing-test-attempt.json"
        if not preview_path.exists() or not receipt_path.exists():
            return
        try:
            preview=json.loads(preview_path.read_text())
            receipt=json.loads(receipt_path.read_text())
            if not isinstance(preview,dict) or not isinstance(receipt,dict):
                raise ValueError("测试回执格式不正确")
            account_id=preview.get("account_id")
            body=preview["body"]
            if not (receipt.get("status")=="published" and receipt.get("body_verified")
                    and isinstance(body,str) and isinstance(account_id,str) and account_id
                    and account_id==receipt.get("account_id")
                    and digest(body)==receipt.get("body_sha256")==preview.get("body_sha256")):
                raise ValueError("回执与已确认正文不一致")
            publications.record_verified({"title":"已确认的测试微头条","body":body,
                "sources":preview["sources"],"verify":[]},account_id,receipt)
            store.set_setting("publishing_account",{"id":account_id,"name":preview.get("account","")})
            store.set_setting("publication_import_error",False)
        except (ValueError,KeyError,TypeError,OSError):
            store.set_setting("publication_import_error",True)

    @app.get("/api/publications")
    async def publication_history():
        account=store.setting("publishing_account",{})
        return {"account":account.get("name", ""),"items":publications.records(account.get("id",""))}

    @app.post("/api/drafts/{identity}/dedup-check")
    async def dedup_check(identity:str):
        candidate=store.draft(identity)
        account=store.setting("publishing_account",{})
        return await guard.inspect(candidate,account.get("id",""))

    @app.middleware('http')
    async def local_requests(request:Request,call_next):
        if request.url.hostname not in ('127.0.0.1','localhost'):
            return JSONResponse({'detail':'仅允许本机访问'},status_code=400)
        if request.method not in ('GET','HEAD'):
            if request.headers.get('origin')!=str(request.base_url).rstrip('/') or request.headers.get('x-workbench-request')!='1':
                return JSONResponse({'detail':'修改操作须从本地工作台发起'},status_code=403)
        response=await call_next(request)
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Referrer-Policy']='no-referrer'
        response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        response.headers['Cache-Control']='no-store'
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request,exc):
        return JSONResponse({'detail':[{'loc':list(e['loc']),'msg':e['msg'],'type':e['type']} for e in exc.errors()]},status_code=422)

    @app.exception_handler(ValueError)
    async def bad_value(request,exc):
        return JSONResponse({'detail':str(exc)},status_code=422)

    @app.exception_handler(KeyError)
    async def missing_value(request,exc):
        return JSONResponse({'detail':str(exc).strip("'")},status_code=404)

    def check_collection(options):
        if not options.get('keyword') and not options.get('domains') and not options.get('domain'):
            raise HTTPException(422,'请输入关键词或选择领域')
        kinds=options.get('kinds') or [options['kind']]
        if any(not caps.get(kind) for kind in kinds):
            raise HTTPException(422,'此体裁的正文采集未通过验证，当前不可用')
        combinations=len(kinds)*max(1,len(options.get('domains') or []))
        if options['limit']<combinations:
            raise HTTPException(422,f'当前选择包含 {combinations} 个体裁与领域组合，请将每轮检查上限调到至少 {combinations}')
        if not caps.get('reads') and (options.get('read_min') is not None or options.get('read_max') is not None):
            raise HTTPException(422,'当前页面不提供可验证的阅读量，暂不支持按阅读量采集')
        if options.get('sort') in ('reads','likes') and not any(caps.get('fields',{}).get(kind,caps).get(options['sort']) for kind in kinds):
            raise HTTPException(422,'当前数据源不支持此排序指标')
        if options.get('auto_generate') and not llm.status()['configured']:
            raise HTTPException(422,'请先配置模型，或关闭自动生成草稿')

    @app.get('/')
    async def home():
        return FileResponse(ROOT/'static/index.html')

    def publishing_status():
        result=micro.status()
        day=micro._now().date().isoformat()
        history=[row for row in publications.records(store.setting('publishing_account',{}).get('id','')) if row.get('created_at','').startswith(day)]
        result.update(submitted_total=sum(row['status'] in ('submitted','published','rejected') for row in history),
            published_total=sum(row['status']=='published' for row in history))
        result.update(ai_declaration=store.setting('publishing',{}).get('ai_declaration',False),
            keywords=store.setting('publishing_keywords',DEFAULT_KEYWORDS),
            account=store.setting('publishing_account',{}).get('name','未绑定账号'))
        return result

    @app.get('/api/publishing/settings')
    async def publishing_settings():
        return publishing_status()

    @app.put('/api/publishing/settings')
    async def save_publishing_settings(options:PublishingSettings):
        previous=store.setting('publishing',{})
        store.set_setting('publishing',options.model_dump())
        if previous.get('ai_declaration',False)!=options.ai_declaration:
            store.set_setting('publishing_verification',{})
            micro.pause('声明设置已变更，请验证并恢复')
        return publishing_status()

    @app.put('/api/publishing/keywords')
    async def publishing_keywords(options:PublishingKeywords):
        store.set_setting('publishing_keywords',options.keywords)
        return publishing_status()

    @app.get('/api/publishing/queue')
    async def publishing_queue():
        return {'items':micro.queue.items(),'logs':store.setting('publishing_logs',[])}

    @app.get('/api/publishing/today')
    async def publishing_today():
        return publishing_status()

    @app.post('/api/publishing/bind')
    async def publishing_bind():
        async with micro._tick_lock:
            try:account=await publisher.identify()
            except Exception:raise HTTPException(409,'无法绑定账号，请确认专用浏览器已登录；已绑定账号不能自动更换') from None
            previous=store.setting('publishing_account',{})
            if previous.get('id')==account['id'] and previous.get('name'):account['name']=previous['name']
            store.set_setting('publishing_account',account)
            store.set_setting('publishing_verification',{})
            micro.pause('账号已绑定，请验证并运行')
        return publishing_status()

    @app.post('/api/publishing/pause')
    async def publishing_pause():
        micro.pause('人工暂停')
        return publishing_status()

    @app.post('/api/publishing/resume')
    async def publishing_resume():
        try:await micro.resume()
        except Exception:raise HTTPException(409,'发布验证失败，请在专用浏览器处理登录、账号或声明后重试') from None
        return publishing_status()

    @app.post('/api/publishing/queue/{identity}/confirm')
    async def publishing_confirm(identity:str,options:PublicationConfirmation):
        return await micro.confirm(identity,options.body_sha256)

    @app.post('/api/publishing/queue/{identity}/reject')
    async def publishing_reject(identity:str):
        return await micro.reject(identity)

    @app.post('/api/publishing/queue/{identity}/reconcile')
    async def publishing_reconcile(identity:str):
        try:return await micro.reconcile(identity)
        except NeedsAttention as exc:raise HTTPException(409,str(exc)) from None

    @app.post('/api/drafts/{identity}/queue')
    async def stage_draft(identity:str,options:PublicationStage):
        return await micro.stage(identity,options.topic,options.body_sha256)

    @app.get('/api/status')
    async def status():
        return {'capabilities':caps,'domains':DOMAINS,'model':llm.status(),'paused':engine.paused,
                'active_id':engine.active_id,'queued':engine.queue.qsize(),
                'schedule':store.setting('schedule',{'enabled':False,'minutes':30,'collection':None}),
                'next_run_in':max(0,int(engine.next_run-time.monotonic())) if engine.next_run else None,
                'preferences':store.setting('preferences',{}),
                'publishing':publishing_status(),
                'counts':{'materials':len(store.materials()),'drafts':len(store.drafts())}}

    @app.post('/api/preferences')
    async def preferences(options:Collection):
        store.set_setting('preferences',options.model_dump())
        return {'saved':True}

    @app.post('/api/session/open')
    async def open_session():
        if engine.active_id or micro.status()['running']:
            raise HTTPException(409,'请等待当前任务完成，再打开登录窗口')
        micro.pause('正在人工处理登录，完成后请验证并恢复')
        try:
            return await browser.login()
        except RuntimeError as exc:
            raise HTTPException(422,str(exc)) from None
        except Exception:
            raise HTTPException(422,'浏览器未能打开，请关闭占用专用会话目录的 Chrome 后重试') from None

    @app.get('/api/session')
    async def session_status():
        return await browser.status()

    @app.post('/api/session/clear')
    async def clear_session():
        if engine.active_id or micro.status()['running']:
            raise HTTPException(409,'请等待当前任务完成，再清除会话')
        micro.pause('专用会话已清除，请登录后验证并恢复')
        store.set_setting('publishing_verification',{})
        return await browser.clear()

    @app.post('/api/tasks/resume')
    async def resume():
        await engine.resume()
        return {'paused':engine.paused}

    @app.post('/api/tasks/collect',status_code=202)
    async def collect(options:Collection):
        data=options.model_dump()
        check_collection(data)
        store.set_setting('preferences',data)
        return engine.submit('collect',data)

    @app.post('/api/tasks/import',status_code=202)
    async def import_links(options:ImportRequest):
        if any(not caps.get('micro' if '/w/' in url else 'article') for url in options.urls):
            raise HTTPException(422,'链接对应体裁的正文采集未通过验证')
        return engine.submit('import',options.model_dump())

    @app.get('/api/tasks')
    async def tasks():
        return store.tasks()

    @app.post('/api/tasks/{identity}/stop')
    async def stop(identity:str):
        engine.stop(identity)
        return store.task(identity)

    @app.post('/api/tasks/{identity}/retry')
    async def retry(identity:str):
        engine.retry(identity)
        return store.task(identity)

    @app.put('/api/schedule')
    async def save_schedule(options:Schedule):
        data=options.model_dump()
        if data['enabled']:
            check_collection(data['collection'])
        engine.set_schedule(data)
        return data

    @app.get('/api/materials')
    async def materials(query:str='',kind:str='',hours:int=0,read_min:int|None=None,read_max:int|None=None,sort:str='published_at'):
        rows,unknown=select_items(store.materials(),{'query':query,'kind':kind,'hours':hours,'read_min':read_min,'read_max':read_max,'sort':sort})
        return {'items':[{k:v for k,v in r.items() if k!='body'} for r in rows],'unknown_excluded':unknown}

    @app.get('/api/materials/{identity}')
    async def material(identity:str):
        return store.material(identity)

    @app.post('/api/tasks/generate',status_code=202)
    async def generate(options:GenerateRequest):
        if not llm.status()['configured']:
            raise HTTPException(422,'请先配置模型 .env 并重启工具')
        for identity in options.ids:
            store.material(identity)
        return engine.submit('generate',options.model_dump())

    def model_config_status():
        return {**llm.status(),'base_url':getattr(llm,'base_url','')}

    @app.get('/api/model/config')
    async def model_config():
        return model_config_status()

    @app.put('/api/model/config')
    async def save_model_config(options:ModelConfig):
        if engine.active_id or llm.lock.locked():
            raise HTTPException(409,'请等待当前采集或模型任务结束后修改配置')
        key=options.api_key or getattr(llm,'api_key','')
        if not key:
            raise HTTPException(422,'首次配置需要填写 API Key')
        save_model_values(data_dir,options,key)
        llm.base_url,llm.api_key,llm.model=options.base_url,key,options.model
        return model_config_status()

    @app.post('/api/model/test')
    async def model_test():
        if engine.active_id:
            raise HTTPException(409,'请等待当前任务结束后测试模型')
        await llm.generate([{'title':'连接测试','url':'local:test','body':'本条仅用于测试模型能否返回有效 JSON。'}],Generation(length='300').model_dump(),1)
        return {'ok':True,'model':llm.model}

    @app.get('/api/drafts')
    async def drafts():
        return store.drafts()

    @app.get('/api/drafts/{identity}')
    async def draft(identity:str):
        return store.draft(identity)

    @app.put('/api/drafts/{identity}')
    async def edit_draft(identity:str,options:DraftEdit):
        related=[row for row in micro.queue.items() if row['draft_id']==identity]
        if any(row['status']=='publishing' for row in related):
            raise HTTPException(409,'发布中的正文不可修改，请先核实提交结果')
        result=store.edit_draft(identity,options.title,options.body,options.verify)
        for row in related:
            if row['status'] in ('pending','awaiting_confirmation') and any(result.get(k)!=row.get(k) for k in ('title','body','verify')):
                await micro.reject(row['id'])
        return result

    @app.get('/api/drafts/{identity}/export')
    async def export_draft(identity:str,format:Literal['md','txt']='md'):
        draft=store.draft(identity)
        body=draft['body']
        if body.splitlines() and body.splitlines()[0].strip()==draft['title'].strip():
            body='\n'.join(body.splitlines()[1:]).lstrip('\n')
        lines=[('# ' if format=='md' else '')+draft['title'],'',body,'','待核实事项']
        lines.extend('- '+v for v in draft.get('verify',[]))
        lines.extend(['','资料来源'])
        lines.extend(f"- {s.get('title','')} {s['url']}" for s in draft.get('sources',[]))
        return Response('\n'.join(lines),media_type='text/plain; charset=utf-8',headers={'Content-Disposition':f'attachment; filename="draft-{identity[:12]}.{format}"'})

    app.mount('/static',StaticFiles(directory=ROOT/'static'),name='static')
    return app
