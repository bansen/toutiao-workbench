import tempfile,unittest
from pathlib import Path
from fastapi.testclient import TestClient
from workbench.app import create_app
from test_engine import FakeBrowser,FakeLLM

class ReviewAPITests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.app=create_app(Path(self.directory.name),browser=FakeBrowser(),llm=FakeLLM())
        self.context=TestClient(self.app,base_url='http://127.0.0.1:8765');self.client=self.context.__enter__()
        self.headers={'Origin':'http://127.0.0.1:8765','X-Workbench-Request':'1'}
    def tearDown(self):self.context.__exit__(None,None,None);self.directory.cleanup()
    def test_settings_cannot_enable_without_real_validation_and_origin(self):
        self.assertEqual(self.client.put('/api/reviews/settings',json={'enabled':True}).status_code,403)
        settings=self.client.get('/api/reviews/settings').json()
        self.assertFalse(settings['enabled']);self.assertFalse(settings['validated'])
        response=self.client.put('/api/reviews/settings',json={'enabled':True},headers=self.headers)
        self.assertEqual(response.status_code,422)
        self.assertFalse(self.client.get('/api/reviews/settings').json()['enabled'])
    def test_noop_check_does_not_collect_or_require_bound_account(self):
        response=self.client.post('/api/reviews/check',json={},headers=self.headers)
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()['reason'],'disabled')
        self.assertFalse(response.json()['due'])
        with self.app.state.review_service.reviews.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM daily_reviews').fetchone()[0],0)
    def test_policy_cas_and_raw_details_are_not_exposed(self):
        state=self.client.get('/api/editorial-policy').json()
        self.assertEqual(state['version'],0)
        response=self.client.post('/api/editorial-policy/rollback',json={'expected_version':1},headers=self.headers)
        self.assertEqual(response.status_code,422)
        self.assertEqual(self.client.get('/api/editorial-policy').json()['version'],0)
        self.assertEqual(self.client.get('/api/reviews/not-existing/anonymous').status_code,404)
        self.assertEqual(self.client.get('/api/reviews/not-existing/raw').status_code,404)
