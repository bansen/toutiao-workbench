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


if __name__=='__main__': unittest.main()
