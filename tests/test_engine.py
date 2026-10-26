import asyncio
import tempfile
import unittest
from pathlib import Path

from workbench.engine import Engine
from workbench.store import Store
from workbench.browser import NeedsAttention


class FakeBrowser:
    def __init__(self):
        self.lock=asyncio.Lock()
        self.attention=False
    async def search(self,keyword,kind,limit):
        if self.attention: raise NeedsAttention('请完成安全验证')
        return [{'url':f'https://www.toutiao.com/article/{7699775205312315954+i}/','kind':'article','title':f'资料{i}'} for i in range(min(limit,5))]
    async def detail(self,url,kind='article'):
        return {'url':url,'content_id':url.rstrip('/').split('/')[-1],'kind':kind,'title':'来源','body':'正文','published_at':'2026-10-25T00:00:00+08:00'}
    async def close(self): pass


class FakeLLM:
    model='fake'
    def __init__(self): self.calls=[];self.fail_version=None
    def status(self): return {'configured':True,'model':'fake','key_set':True}
    async def generate(self,sources,options,version):
        self.calls.append(version)
        if version==self.fail_version: raise ValueError('模型返回空内容')
        return {'title':'稿件','body':'成稿','verify':[],'sources':sources,'options':options,'model':'fake'}


class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(Path(self.tmp.name)/'test.db')
        self.browser=FakeBrowser();self.llm=FakeLLM()
        self.engine=Engine(self.store,self.browser,self.llm,{'article':True,'micro':False})
    async def asyncTearDown(self):
        await self.engine.close();self.tmp.cleanup()

    async def test_repeated_collect_deduplicates_and_auto_generation_caps_three(self):
        opts={'keyword':'AI','kind':'article','limit':5,'auto_generate':True,'generation':{'mode':'single','versions':1}}
        task=self.store.create_task('collect',opts)
        await self.engine.execute(task['id'])
        self.assertEqual(self.store.task(task['id'])['accepted'],5)
        self.assertEqual(len(self.store.drafts()),3)
        again=self.store.create_task('collect',opts)
        await self.engine.execute(again['id'])
        self.assertEqual(len(self.store.materials()),5)
        self.assertEqual(len(self.store.drafts()),3)

    async def test_partial_generation_retry_skips_successful_versions(self):
        row,_=self.store.upsert_material(await self.browser.detail('https://www.toutiao.com/article/7699775205312315954/'))
        task=self.store.create_task('generate',{'ids':[row['id']],'mode':'single','versions':2})
        self.llm.fail_version=2
        await self.engine.execute(task['id'])
        self.assertEqual(self.store.task(task['id'])['status'],'partial')
        self.llm.fail_version=None
        await self.engine.execute(task['id'])
        self.assertEqual(self.llm.calls,[1,2,2])
        self.assertEqual(len(self.store.drafts()),2)

    async def test_attention_pauses_scheduler_until_explicit_resume(self):
        self.browser.attention=True
        task=self.store.create_task('collect',{'keyword':'AI','kind':'article','limit':5})
        await self.engine.execute(task['id'])
        self.assertEqual(self.store.task(task['id'])['status'],'attention')
        self.assertTrue(self.engine.paused)
        self.browser.attention=False
        await self.engine.resume()
        self.assertFalse(self.engine.paused)
        self.assertEqual(self.store.task(task['id'])['status'],'waiting')

    async def test_schedule_waits_next_interval_and_skips_overlap(self):
        self.engine.set_schedule({'enabled':True,'minutes':30,'collection':{'keyword':'AI','kind':'article','limit':5}})
        deadline=self.engine.next_run
        await self.engine.tick(deadline-1)
        self.assertEqual(len(self.store.tasks()),0)
        self.engine.active_id='other'
        await self.engine.tick(deadline+5000)
        self.assertEqual(len(self.store.tasks()),0)
        self.assertGreater(self.engine.next_run,deadline+5000)

    async def test_resume_finds_attention_job_outside_visible_history(self):
        task=self.store.create_task('collect',{'keyword':'AI','kind':'article','limit':1})
        self.store.update_task(task['id'],status='attention')
        for _ in range(100):
            new=self.store.create_task('generate',{})
            self.store.update_task(new['id'],status='success')
        self.engine.paused=True
        await self.engine.resume()
        self.assertEqual(self.store.task(task['id'])['status'],'waiting')
        self.assertEqual(await self.engine.queue.get(),task['id'])


if __name__=='__main__': unittest.main()
