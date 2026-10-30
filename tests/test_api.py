import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from workbench.app import create_app
from test_engine import FakeBrowser,FakeLLM


class APITests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.app=create_app(Path(self.tmp.name),browser=FakeBrowser(),llm=FakeLLM())
        self.ctx=TestClient(self.app,base_url='http://127.0.0.1:8765')
        self.client=self.ctx.__enter__()
        self.headers={'Origin':'http://127.0.0.1:8765','X-Workbench-Request':'1'}
    def tearDown(self):
        self.ctx.__exit__(None,None,None);self.tmp.cleanup()

    def test_foreign_origin_missing_origin_and_host_rebinding_blocked(self):
        for headers in [{},{'Origin':'https://evil.example','X-Workbench-Request':'1'}]:
            self.assertEqual(self.client.post('/api/tasks/collect',json={'keyword':'AI'},headers=headers).status_code,403)
        self.assertEqual(self.client.get('/api/status',headers={'Host':'evil.example'}).status_code,400)

    def test_invalid_links_and_unknown_type_rejected_before_jobs(self):
        response=self.client.post('/api/tasks/collect',json={'keyword':'AI','kind':'video'},headers=self.headers)
        self.assertEqual(response.status_code,422)
        response=self.client.post('/api/tasks/import',json={'urls':['http://localhost/private']},headers=self.headers)
        self.assertEqual(response.status_code,422)

    def test_preferences_can_save_without_keyword_but_collection_requires_it(self):
        response=self.client.post('/api/preferences',json={'keyword':''},headers=self.headers)
        self.assertEqual(response.status_code,200)
        response=self.client.post('/api/tasks/collect',json={'keyword':''},headers=self.headers)
        self.assertEqual(response.status_code,422)

    def test_status_does_not_return_api_key(self):
        result=self.client.get('/api/status').json()
        self.assertEqual(result['model']['key_set'],True)
        self.assertNotIn('api_key',result['model'])

    def test_draft_edit_export_and_restart_persistence(self):
        store=self.app.state.engine.store
        draft=store.save_generated('task','src',1,{'title':'原稿','body':'正文','verify':[],'sources':[],'model':'fake','options':{}})
        r=self.client.put('/api/drafts/'+draft['id'],json={'title':'修改稿','body':'修改正文','verify':['核对']},headers=self.headers)
        self.assertEqual(r.status_code,200)
        r=self.client.get('/api/drafts/'+draft['id']+'/export?format=md')
        self.assertIn('修改正文',r.text)
        self.assertIn('核对',r.text)
        self.assertIn('attachment',r.headers['content-disposition'])

    def test_check_requires_local_origin_and_bound_account(self):
        store=self.app.state.engine.store
        draft=store.save_generated('task','src',1,{'title':'稿件','body':'测试正文','verify':[], 'sources':[{'url':'https://www.toutiao.com/article/7701979050666902079/'}]})
        url='/api/drafts/'+draft['id']+'/dedup-check'
        self.assertEqual(self.client.post(url).status_code,403)
        self.assertEqual(self.client.post(url,headers=self.headers).json()['decision'],'review')
        store.set_setting('publishing_account',{'id':'account','name':'测试'})
        self.app.state.publications.record_verified(draft,'account',{'status':'published','content_id':'1880433446879241'})
        self.assertEqual(self.client.post(url,headers=self.headers).json()['decision'],'duplicate')
        self.assertEqual(len(self.client.get('/api/publications').json()['items']),1)


    def test_verified_private_receipt_import_is_idempotent_and_mismatch_blocks(self):
        import hashlib,json
        body='真实测试正文'
        sha=hashlib.sha256(body.encode()).hexdigest()
        preview={'account_id':'verified-account','account':'已核实账号','body':body,'body_sha256':sha,
                 'sources':[{'url':'https://www.toutiao.com/article/7701979050666902079/'}]}
        receipt={'account_id':'verified-account','status':'published','body_verified':True,'body_sha256':sha,
                 'content_id':'1880433446879241','url':'https://www.toutiao.com/w/1880433446879241/'}
        directory=Path(self.tmp.name)
        (directory/'publishing-test-preview.json').write_text(json.dumps(preview))
        (directory/'publishing-test-attempt.json').write_text(json.dumps(receipt))
        for _ in range(2):
            imported=create_app(directory,browser=FakeBrowser(),llm=FakeLLM())
            with TestClient(imported,base_url='http://127.0.0.1:8765') as client:
                self.assertEqual(len(client.get('/api/publications').json()['items']),1)
                self.assertFalse(imported.state.engine.store.setting('publication_import_error',False))
        preview['body']='被修改的快照'
        (directory/'publishing-test-preview.json').write_text(json.dumps(preview))
        imported=create_app(directory,browser=FakeBrowser(),llm=FakeLLM())
        with TestClient(imported,base_url='http://127.0.0.1:8765'):
            self.assertTrue(imported.state.engine.store.setting('publication_import_error',False))



    def test_auto_publishing_defaults_enabled_but_never_reports_running(self):
        result=self.client.get('/api/publishing/settings')
        self.assertEqual(result.status_code,200)
        config=result.json()
        self.assertIs(config['enabled'],True)
        self.assertIs(config['ready'],False)
        self.assertIs(config['running'],False)
        self.assertEqual(config['state'],'unverified')
        self.assertTrue(config['reason'])
        self.assertEqual(self.client.get('/api/status').json()['publishing'],config)

    def test_auto_publishing_toggle_persists_false_on_restart(self):
        response=self.client.put('/api/publishing/settings',json={'enabled':False},headers=self.headers)
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()['state'],'disabled')
        reopened=create_app(Path(self.tmp.name),browser=FakeBrowser(),llm=FakeLLM())
        with TestClient(reopened,base_url='http://127.0.0.1:8765') as client:
            self.assertIs(client.get('/api/publishing/settings').json()['enabled'],False)
        response=self.client.put('/api/publishing/settings',json={'enabled':True},headers=self.headers)
        self.assertIs(response.json()['enabled'],True)
        self.assertIs(response.json()['running'],False)

    def test_auto_publishing_settings_reject_foreign_origin_and_forged_readiness(self):
        path='/api/publishing/settings'
        for headers in ({},{'Origin':'https://evil.example','X-Workbench-Request':'1'}):
            self.assertEqual(self.client.put(path,json={'enabled':False},headers=headers).status_code,403)
        for data in ({'enabled':'false'},{'enabled':True,'ready':True},{'enabled':True,'running':True}):
            self.assertEqual(self.client.put(path,json=data,headers=self.headers).status_code,422)



    def test_runtime_pause_and_resume_have_live_status(self):
        class Publisher:
            async def probe(self):return {'account_id':'account','declaration_verified':True,'ai_declaration':False}
            async def close(self):pass
        runtime=self.app.state.micro_runtime
        runtime.publisher=Publisher()
        runtime.store.set_setting('publishing_account',{'id':'account'})
        result=self.client.post('/api/publishing/resume',headers=self.headers)
        self.assertEqual(result.status_code,200)
        self.assertTrue(result.json()['ready'])
        self.assertTrue(result.json()['service_active'])
        result=self.client.post('/api/publishing/pause',headers=self.headers)
        self.assertTrue(result.json()['paused'])
        self.assertEqual(result.json()['state'],'paused')

    def test_keyword_config_is_limited_to_three_directions(self):
        path='/api/publishing/keywords'
        self.assertEqual(self.client.put(path,json={'keywords':{'娱乐':['娱乐']}},headers=self.headers).status_code,422)
        good={'民生':['公共服务'],'AI':['人工智能'],'科技':['机器人']}
        self.assertEqual(self.client.put(path,json={'keywords':good},headers=self.headers).json()['keywords'],good)

    def test_edit_cancels_queued_snapshot_but_cannot_modify_publishing_body(self):
        runtime=self.app.state.micro_runtime;store=runtime.store
        draft=store.save_generated('task','src',1,{'title':'排期稿','body':'正文快照','verify':[], 'sources':[{'url':'https://www.toutiao.com/article/7701979050666902079/'}]})
        plan=runtime.plans.load(runtime._now().date())
        slot=next(s for s in plan['slots'] if s['status']=='pending')
        row=runtime.queue.add(slot,draft,'科技','event')
        runtime.plans.update_slot(slot['id'],{'status':'queued'})
        result=self.client.put('/api/drafts/'+draft['id'],json={'title':'排期稿','body':'新正文','verify':[]},headers=self.headers)
        self.assertEqual(result.status_code,200)
        self.assertEqual(runtime.queue.get(row['id'])['status'],'cancelled')
        next_slot=next(s for s in plan['slots'] if s['status']=='pending' and s['id']!=slot['id'])
        other=runtime.queue.add(next_slot,store.draft(draft['id']),'科技','event2')
        runtime.queue.update(other['id'],{'status':'publishing'})
        result=self.client.put('/api/drafts/'+draft['id'],json={'title':'排期稿','body':'不得修改','verify':[]},headers=self.headers)
        self.assertEqual(result.status_code,409)
        self.assertEqual(store.draft(draft['id'])['body'],'新正文')



    def test_bind_uses_browser_identified_account_instead_of_request_fields(self):
        class Publisher:
            async def identify(self):return {'id':'browser-account','name':'已登录账号'}
            async def close(self):pass
        app=create_app(Path(self.tmp.name)/'binding',browser=FakeBrowser(),llm=FakeLLM(),publisher=Publisher())
        with TestClient(app,base_url='http://127.0.0.1:8765') as client:
            response=client.post('/api/publishing/bind',json={'id':'forged'},headers=self.headers)
            self.assertEqual(response.status_code,200)
            self.assertEqual(app.state.engine.store.setting('publishing_account')['id'],'browser-account')
            self.assertEqual(response.json()['account'],'已登录账号')
            self.assertFalse(response.json()['ready'])


if __name__=='__main__': unittest.main()
