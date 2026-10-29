import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient
from workbench.app import create_app
from workbench.llm import LLM
from test_engine import FakeBrowser


class ModelConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)
        self.model=LLM('','','')
        self.app=create_app(self.path,browser=FakeBrowser(),llm=self.model)
        self.ctx=TestClient(self.app,base_url='http://127.0.0.1:8765')
        self.client=self.ctx.__enter__()
        self.headers={'Origin':'http://127.0.0.1:8765','X-Workbench-Request':'1'}
    def tearDown(self):
        self.ctx.__exit__(None,None,None)
        self.tmp.cleanup()
    def test_configuration_saved_privately_and_live_without_key_echo(self):
        secret='unit-test-secret-${literal}'
        result=self.client.put('/api/model/config',headers=self.headers,json={'base_url':'https://model.example/v1','api_key':secret,'model':'test'} )
        self.assertEqual(result.status_code,200)
        self.assertTrue(result.json()['configured'])
        self.assertNotIn(secret,result.text)
        self.assertEqual(self.model.api_key,secret)
        self.assertEqual((self.path/'model.env').stat().st_mode & 0o777,0o600)
        reopened=create_app(self.path,browser=FakeBrowser())
        self.assertEqual(reopened.state.engine.llm.api_key,secret)
        self.assertNotIn(secret,self.client.get('/api/model/config').text)
        result=self.client.put('/api/model/config',headers=self.headers,json={'base_url':'https://model.example/v1','api_key':'','model':'changed'})
        self.assertEqual(result.status_code,200)
        self.assertEqual(self.model.api_key,secret)
    def test_origin_active_job_and_model_lock_block_configuration(self):
        values={'base_url':'https://model.example/v1','api_key':'key','model':'test'}
        self.assertEqual(self.client.put('/api/model/config',json=values).status_code,403)
        self.app.state.engine.active_id='active'
        self.assertEqual(self.client.put('/api/model/config',json=values,headers=self.headers).status_code,409)
    def test_invalid_fields_never_echo_secret_in_validation_response(self):
        secret='test-secret-\n-injection'
        response=self.client.put('/api/model/config',headers=self.headers,json={'base_url':'https://model.example/v1','api_key':secret,'model':'test'})
        self.assertEqual(response.status_code,422)
        self.assertNotIn('test-secret',response.text)
        self.assertFalse((self.path/'model.env').exists())
    def test_credentials_in_base_url_are_rejected_without_echo(self):
        response=self.client.put('/api/model/config',headers=self.headers,json={'base_url':'https://user:base-secret@model.example/v1','api_key':'key','model':'test'})
        self.assertEqual(response.status_code,422)
        self.assertNotIn('base-secret',response.text)

class LegacyEnvironmentTests(unittest.TestCase):
    def test_project_env_interpolation_is_preserved(self):
        from workbench.model_config import load_model_values
        with tempfile.TemporaryDirectory() as tmp:
            project=Path(tmp)
            (project/'.env').write_text('PROVIDER=https://provider.example\nLLM_BASE_URL=${PROVIDER}/v1\nLLM_API_KEY=fixture\nLLM_MODEL=test\n')
            from unittest.mock import patch
            with patch.dict('os.environ',{},clear=True):
                self.assertEqual(load_model_values(project,project/'data')[0],'https://provider.example/v1')

    def test_project_env_data_directory_is_preserved(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            project=Path(tmp)
            (project/'docs').mkdir()
            (project/'static').mkdir()
            (project/'docs/capabilities.json').write_text('{}')
            expected=project/'custom-data'
            (project/'.env').write_text('WORKBENCH_DATA_DIR='+str(expected)+'\n')
            with patch('workbench.app.ROOT',project),patch.dict('os.environ',{},clear=True):
                app=create_app(browser=FakeBrowser(),llm=LLM('','',''))
            self.assertEqual(app.state.engine.store.path,expected/'workbench.db')
