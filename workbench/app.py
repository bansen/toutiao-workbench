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

from .browser import Browser
from .engine import Engine
from .llm import LLM
from .model_config import ModelConfig,load_model_values,save_model_values
from .parsing import select_items
from .schemas import Collection,DOMAINS,DraftEdit,GenerateRequest,Generation,ImportRequest,Schedule
from .store import Store

ROOT=Path(__file__).resolve().parent.parent


def create_app(data_dir=None,browser=None,llm=None):
    load_dotenv(ROOT/'.env')
    data_dir=Path(data_dir or os.environ.get('WORKBENCH_DATA_DIR',str(ROOT/'data')))
    data_dir.mkdir(parents=True,exist_ok=True)
    data_dir.chmod(0o700)
    store=Store(data_dir/'workbench.db')
    caps=json.loads((ROOT/'docs/capabilities.json').read_text())
    browser=browser or Browser(data_dir/'browser')
    llm=llm or LLM(*load_model_values(ROOT,data_dir))
    engine=Engine(store,browser,llm,caps)

    @asynccontextmanager
    async def lifespan(app):
        await engine.start()
        yield
        await engine.close()

    app=FastAPI(title='头条内容工作台',lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
    app.state.engine=engine

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

    @app.get('/api/status')
    async def status():
        return {'capabilities':caps,'domains':DOMAINS,'model':llm.status(),'paused':engine.paused,
                'active_id':engine.active_id,'queued':engine.queue.qsize(),
                'schedule':store.setting('schedule',{'enabled':False,'minutes':30,'collection':None}),
                'next_run_in':max(0,int(engine.next_run-time.monotonic())) if engine.next_run else None,
                'preferences':store.setting('preferences',{}),
                'counts':{'materials':len(store.materials()),'drafts':len(store.drafts())}}

    @app.post('/api/preferences')
    async def preferences(options:Collection):
        store.set_setting('preferences',options.model_dump())
        return {'saved':True}

    @app.post('/api/session/open')
    async def open_session():
        if engine.active_id:
            raise HTTPException(409,'请先停止当前任务，再打开登录窗口')
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
        if engine.active_id:
            raise HTTPException(409,'请先停止当前任务，再清除会话')
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
        return store.edit_draft(identity,options.title,options.body,options.verify)

    @app.get('/api/drafts/{identity}/export')
    async def export_draft(identity:str,format:Literal['md','txt']='md'):
        draft=store.draft(identity)
        lines=[('# ' if format=='md' else '')+draft['title'],'',draft['body'],'','待核实事项']
        lines.extend('- '+v for v in draft.get('verify',[]))
        lines.extend(['','资料来源'])
        lines.extend(f"- {s.get('title','')} {s['url']}" for s in draft.get('sources',[]))
        return Response('\n'.join(lines),media_type='text/plain; charset=utf-8',headers={'Content-Disposition':f'attachment; filename="draft-{identity[:12]}.{format}"'})

    app.mount('/static',StaticFiles(directory=ROOT/'static'),name='static')
    return app
