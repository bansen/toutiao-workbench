import asyncio
import hashlib
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from workbench.store import Store
from workbench.micro_runtime import MicroRuntime

TZ=ZoneInfo('Asia/Shanghai')

class Browser:
    def __init__(self):
        self.lock=asyncio.Lock(); self.publishing_idle=asyncio.Event(); self.publishing_idle.set(); self.calls=[]
    async def search(self,*args): self.calls.append(args); return []
    async def detail(self,url,kind):
        return {'url':url,'content_id':'123456789012','title':'来源','body':'来源正文','kind':kind,'published_at':'2026-10-30T07:00:00+08:00'}
class Publisher:
    def __init__(self): self.calls=0; self.closed=0; self.preflight_hook=None
    async def probe(self): return {'account_id':'account','declaration_verified':True,'ai_declaration':False}
    async def preflight(self,draft):
        if self.preflight_hook: self.preflight_hook()
        return {}
    async def submit(self,draft):
        if not self.allowed(): raise ValueError('not allowed')
        self.calls+=1
        return {'status':'submitted','content_id':'123'}
    async def close(self): self.closed+=1
    async def reconcile(self,row): return {'status':'published','content_id':'123','url':'https://www.toutiao.com/w/123/'}
class Guard:
    def __init__(self): self.ledger=None
    async def inspect(self,*args): return {'decision':'clear'}
    async def send(self,draft,account,confirmed_sha256,preflight,submit):
        await preflight(draft)
        return {**await submit(draft),'id':'ledger-1'}
class LLM:
    def status(self): return {'configured':True}

class Content:
    def __init__(self,browser): self.browser=browser
    async def check(self,*args):
        assert not self.browser.lock.locked()
        return {'passed':True,'issues':[]}
class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_temporary_review_errors_are_metadata_not_factual_concerns(self):
        draft={'id':'origin','title':'标题','body':'正文','verify':['真正的事实疑点']}
        saved=self.runtime._save_review_draft(draft,['模型超时'],retryable=True)
        self.assertEqual(saved['verify'],['真正的事实疑点'])
        self.assertEqual(saved['review_retryable'],['模型超时'])
        self.assertEqual(saved['recovery_origin'],'origin')

    async def test_repair_timeout_does_not_consume_successful_repair_opportunity(self):
        from unittest.mock import AsyncMock
        draft={'title':'标题','body':'正文','verify':[]}
        self.runtime.content.check=AsyncMock(side_effect=[{'passed':False,'issues':['需要修整']},
            {'passed':False,'issues':['需要修整']},{'passed':True,'issues':[]}])
        self.runtime.content.repair=AsyncMock(side_effect=[TimeoutError(),{**draft,'body':'修整正文'}])
        sources=[{'url':'https://www.workbuddy.cn/docs/feature','kind':'official','body':'资料'}]
        pending,result=await self.runtime._qualify_ai(draft,sources,'AI')
        self.assertTrue(result['retryable'])
        self.assertNotIn('repair_policy',pending)
        fixed,result=await self.runtime._qualify_ai(pending,sources,'AI')
        self.assertTrue(result['passed'])
        self.assertEqual(self.runtime.content.repair.await_count,2)

    async def test_temporary_saved_draft_review_failure_does_not_blacklist_origin(self):
        from unittest.mock import AsyncMock
        from workbench.ai_tools import PROFILE
        self.plan();self.now-=timedelta(minutes=5);await self.runtime.verify();self.runtime.ai_tools=True
        source={'url':'https://www.workbuddy.cn/docs/retry-feature','kind':'official','body':'有来源的具体功能',
                'author':'WorkBuddy 官方文档','verified_at':self.now.isoformat()}
        draft=self.store.save_generated('retry','retry',1,{'title':'功能','body':'正文','verify':[],
            'sources':[source],'source_snapshots':[source],'options':{'profile':PROFILE,'topic':'AI'}})
        self.browser.official_detail=AsyncMock(return_value=source)
        self.runtime.content.check=AsyncMock(side_effect=[TimeoutError(),{'passed':True,'issues':[]}])
        await self.runtime._recover_review_drafts()
        self.assertNotIn(draft['id'],self.store.setting('micro_review_policy_audits',{}))
        await self.runtime._recover_review_drafts()
        self.assertEqual(len(self.runtime.queue.items()),1)

    async def test_temporary_queued_review_failure_can_be_retried_before_slot(self):
        from unittest.mock import AsyncMock
        row,draft=self.enqueue();self.now-=timedelta(minutes=10)
        self.runtime.content.check=AsyncMock(side_effect=[TimeoutError(),{'passed':True,'issues':[]}])
        await self.runtime._certify_pending()
        self.assertNotIn(row['id'],self.store.setting('queue_review_policy',{}))
        await self.runtime._certify_pending()
        self.assertEqual(self.runtime.content.check.await_count,2)
        self.assertIn('review_approval',self.store.draft(draft['id']))

    async def test_recovered_drafts_are_compared_with_same_round_new_queue(self):
        from unittest.mock import AsyncMock
        from workbench.ai_tools import PROFILE
        self.plan();self.now-=timedelta(minutes=5);await self.runtime.verify();self.runtime.ai_tools=True
        plan=self.runtime.plans.load(self.now.date())
        first=next(s for s in plan['slots'] if s.get('content_type')=='tool-guide')
        self.now=datetime.fromisoformat(first['scheduled_at'])-timedelta(minutes=5)
        for i in range(2):
            source={'url':f'https://www.workbuddy.cn/docs/feature-{i}','kind':'official',
                'body':'具有产品特点的官方具体功能正文','author':'WorkBuddy 官方文档','verified_at':self.now.isoformat()}
            self.store.save_generated(str(i),str(i),1,{'title':f'功能{i}','body':f'正文{i}','verify':[],
                'sources':[source],'source_snapshots':[source],'options':{'profile':PROFILE,'topic':'AI'}})
        self.browser.official_detail=AsyncMock(side_effect=lambda url,*args:{'url':url,'kind':'official','body':'实际来源正文'})
        self.runtime.content.check=AsyncMock(return_value={'passed':True,'issues':[]})
        self.runtime.llm.judge_duplicate=AsyncMock(return_value={'decision':'duplicate'})
        await self.runtime._recover_review_drafts()
        self.assertEqual(len(self.runtime.queue.items()),1)
        self.runtime.llm.judge_duplicate.assert_awaited_once()
        self.assertEqual(len(self.runtime.llm.judge_duplicate.call_args.args[1]),1)

    async def test_expired_approval_requests_preparation_even_when_slots_occupied(self):
        from unittest.mock import patch
        from workbench.ai_tools import REVIEW_POLICY
        from workbench.draft_review import approve
        row,draft=self.enqueue();self.runtime.ai_tools=True
        approved=approve(draft,[{'url':'https://example.test','body':'证据'}],self.now-timedelta(hours=4),REVIEW_POLICY)
        with self.store.connection() as db:
            db.execute('UPDATE drafts SET payload=? WHERE id=?',(self.store.dump(approved),draft['id']))
        self.now-=timedelta(minutes=10)
        with patch.object(self.runtime,'_preparation_slots',return_value=[]):
            self.assertTrue(self.runtime._preparation_due(self.now))

    async def test_near_ready_slot_cancels_preparation_before_tick(self):
        from unittest.mock import AsyncMock,patch
        self.enqueue();self.runtime.ai_tools=True
        cancelled=asyncio.Event()
        async def preparing():
            try:await asyncio.Event().wait()
            finally:cancelled.set()
        self.runtime._prepare_task=asyncio.create_task(preparing())
        await asyncio.sleep(0)
        async def tick():
            self.assertTrue(cancelled.is_set())
            self.runtime._closed=True
        self.runtime.tick=tick
        self.runtime._last_retention=self.now
        self.runtime._eligible=lambda now:False
        self.assertTrue(self.runtime._publish_soon(self.now))
        self.assertFalse(self.runtime._preparation_due(self.now))
        with patch('workbench.micro_runtime.asyncio.sleep',new=AsyncMock()):
            await self.runtime._loop()
        self.assertTrue(self.runtime._prepare_task.cancelled())

    async def test_approved_unchanged_sources_do_not_repeat_model_fact_review_at_publish(self):
        from workbench.ai_tools import PROFILE,REVIEW_POLICY
        from workbench.draft_review import approve
        from unittest.mock import AsyncMock
        row,draft=self.enqueue();await self.runtime.verify();self.runtime.ai_tools=True
        sources=await self.runtime._refresh(draft)
        approved=approve({**draft,'options':{'profile':PROFILE}},sources,self.now,REVIEW_POLICY)
        with self.store.connection() as db:
            db.execute('UPDATE drafts SET payload=? WHERE id=?',(self.store.dump(approved),draft['id']))
        self.runtime.content.check=AsyncMock(side_effect=AssertionError('不应重复进行已完成的事实审核'))
        await self.runtime.tick()
        self.assertEqual(self.publisher.calls,1)
        self.runtime.content.check.assert_not_awaited()

    async def test_approved_source_change_requires_new_review_and_can_block_publication(self):
        from workbench.ai_tools import PROFILE,REVIEW_POLICY
        from workbench.draft_review import approve
        from unittest.mock import AsyncMock
        row,draft=self.enqueue();await self.runtime.verify();self.runtime.ai_tools=True
        sources=await self.runtime._refresh(draft)
        approved=approve({**draft,'options':{'profile':PROFILE}},sources,self.now,REVIEW_POLICY)
        with self.store.connection() as db:
            db.execute('UPDATE drafts SET payload=? WHERE id=?',(self.store.dump(approved),draft['id']))
        self.runtime._refresh=AsyncMock(return_value=[{**sources[0],'body':'来源已经撤回原来事实'}])
        self.runtime.content.check=AsyncMock(return_value={'passed':False,'issues':['关键事实被撤回']})
        await self.runtime.tick()
        self.assertEqual(self.publisher.calls,0)
        self.runtime.content.check.assert_awaited_once()

    async def test_hanging_fact_check_times_out_and_releases_scheduler(self):
        row,_=self.enqueue();await self.runtime.verify()
        self.runtime.publish_timeout=.02
        async def hang(*args):await asyncio.Event().wait()
        self.runtime.content.check=hang
        await asyncio.wait_for(self.runtime.tick(),.5)
        self.assertEqual(self.publisher.calls,0)
        self.assertEqual(self.runtime.queue.get(row['id'])['status'],'failed')
        self.assertFalse(self.runtime.status()['running'])
        self.assertTrue(self.browser.publishing_idle.is_set())
        self.assertFalse(self.runtime._tick_lock.locked())
        self.assertIn('超时',str(self.store.setting('publishing_logs')))

    async def test_timeout_after_submit_starts_is_uncertain_and_not_retried(self):
        row,_=self.enqueue();await self.runtime.verify()
        self.runtime.publish_timeout=.02
        async def hang(draft):
            self.publisher.calls+=1
            await asyncio.Event().wait()
        self.publisher.submit=hang
        await asyncio.wait_for(self.runtime.tick(),.5)
        await self.runtime.tick()
        self.assertEqual(self.publisher.calls,1)
        self.assertEqual(self.runtime.queue.get(row['id'])['status'],'uncertain')
        self.assertTrue(self.runtime.status()['paused'])

    async def test_incorrect_type_in_news_queue_is_not_submitted(self):
        from workbench.ai_tools import PROFILE
        self.plan(); await self.runtime.verify(); self.runtime.ai_tools=True
        draft=self.store.save_generated('bad-mix','source',1,{'title':'工具稿','body':'正文','verify':[],
            'options':{'profile':PROFILE},'sources':[{'kind':'official','url':'https://www.workbuddy.cn/docs/feature'}]})
        row=self.runtime.queue.add(self.slot,draft,'AI','event')
        self.runtime.plans.update_slot(self.slot['id'],{'status':'queued','queue_id':row['id']})
        await self.runtime.tick()
        self.assertEqual(self.publisher.calls,0)
        self.assertEqual(self.runtime.queue.get(row['id'])['status'],'skipped')
        self.assertIn('50%',self.runtime.queue.get(row['id'])['reason'])

    async def test_news_slot_is_not_filled_with_tool_guide(self):
        from unittest.mock import AsyncMock, patch
        from workbench.ai_tools import PROFILE
        self.plan(); self.now -= timedelta(minutes=5)
        await self.runtime.verify(); self.runtime.ai_tools = True
        source = {'url':'https://www.workbuddy.cn/docs/test-feature','kind':'official','body':'官方功能正文',
                  'author':'WorkBuddy 官方文档','content_id':'official:feature','verified_at':self.now.isoformat()}
        self.browser.official_detail = AsyncMock(return_value=source)
        self.runtime.content.generate = AsyncMock(return_value={'title':'具体功能','body':'有依据的具体功能正文',
            'verify':[],'sources':[source],'options':{'profile':PROFILE,'topic':'AI'}})
        with patch('workbench.micro_runtime.OfficialResearch.discover',new=AsyncMock(return_value=[('AI','official',source)])), patch('workbench.micro_runtime.TechNewsResearch.discover',new=AsyncMock(return_value=[])):
            await self.runtime.prepare()
        rows = self.runtime.queue.items()
        self.assertEqual(len(rows),1)
        plan = self.runtime.plans.load(self.now.date())
        slot = next(s for s in plan['slots'] if s['id']==rows[0]['slot_id'])
        self.assertEqual(slot.get('content_type'),'tool-guide')
        self.assertEqual(plan['slots'][0]['status'],'pending')
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store=Store(Path(self.tmp.name)/'db'); self.store.set_setting('publishing_account',{'id':'account'})
        self.now=datetime(2026,10,30,10,0,tzinfo=TZ)
        self.browser=Browser(); self.publisher=Publisher(); self.guard=Guard()
        self.runtime=MicroRuntime(self.store,self.browser,LLM(),self.guard,self.publisher,clock=lambda:self.now)
        self.runtime.content=Content(self.browser)
    def plan(self,when=None):
        plan=self.runtime.plans.get_or_create(self.now-timedelta(hours=3))
        # Use the real persisted schedule and move test clock to its next slot.
        self.slot=next(s for s in plan['slots'] if s['status']=='pending')
        self.now=datetime.fromisoformat(self.slot['scheduled_at'])
        return self.slot
    def enqueue(self):
        slot=self.plan()
        draft=self.store.save_generated('task','source',1,{'title':'标题','body':'正文','verify':[], 'sources':[{'url':'https://www.toutiao.com/article/123456789012/','kind':'article'}], 'source_snapshots':[{'body':'来源正文'}]})
        row=self.runtime.queue.add(slot,draft,'民生','event')
        self.runtime.plans.update_slot(slot['id'],{'status':'queued','queue_id':row['id']})
        return row,draft
    async def test_default_enabled_but_requires_real_verification(self):
        self.assertTrue(self.runtime.status()['enabled']); self.assertFalse(self.runtime.status()['ready'])
        await self.runtime.verify(); self.assertTrue(self.runtime.status()['ready'])
        self.store.set_setting('publishing_account',{'id':'other'})
        self.assertFalse(self.runtime.status()['ready'])
    async def test_title_history_uses_sent_and_approved_queue_titles_not_failed_drafts(self):
        row,draft=self.enqueue()
        self.store.save_generated('bad','bad',1,{'title':'目录模板失败稿','body':'内容','verify':['待核实'],'options':{'profile':'ai-tools-v1'}})
        self.runtime._records=lambda:[
            {'id':'sent','title':'真实发布标题','status':'published','created_at':(self.now-timedelta(minutes=30)).isoformat()},
            {'id':'failed','title':'提交前失败标题','status':'failed_before_submit','created_at':self.now.isoformat()}]
        titles=self.runtime._recent_titles()
        self.assertIn(draft['title'],titles)
        self.assertIn('真实发布标题',titles)
        self.assertNotIn('目录模板失败稿',titles)
        self.assertNotIn('提交前失败标题',titles)
        self.assertNotIn(draft['title'],self.runtime._recent_titles(exclude_draft_id=draft['id']))
        self.runtime.queue.update(row['id'],{'status':'cancelled'})
        self.assertNotIn(draft['title'],self.runtime._recent_titles())
    async def test_prepublication_title_check_refreshes_history_and_excludes_its_own_title(self):
        from unittest.mock import AsyncMock
        row,draft=self.enqueue();await self.runtime.verify()
        self.runtime._records=lambda:[{'id':'other','title':'后来真正发出的标题','status':'published',
            'created_at':(self.now-timedelta(hours=1)).isoformat()}]
        check=AsyncMock(return_value={'passed':True,'issues':[]})
        self.runtime.content.check=check
        await self.runtime.tick()
        checked=check.call_args.args[0]
        self.assertEqual(checked['recent_titles'],['后来真正发出的标题'])
        self.assertNotIn(row['title'],checked['recent_titles'])
        self.assertEqual(self.publisher.calls,1)
    async def test_disabled_never_probes_or_submits(self):
        row,_=self.enqueue(); await self.runtime.verify()
        self.store.set_setting('publishing',{'enabled':False})
        await self.runtime.tick(); self.assertEqual(self.publisher.calls,0)
    async def test_due_slot_submits_once_and_releases_priority(self):
        row,_=self.enqueue(); await self.runtime.verify()
        await self.runtime.tick(); await self.runtime.tick()
        self.assertEqual(self.publisher.calls,1)
        self.assertEqual(self.runtime.queue.get(row['id'])['status'],'submitted')
        self.assertTrue(self.browser.publishing_idle.is_set())
    async def test_prepublication_check_preserves_specific_issues_without_submitting(self):
        row,_=self.enqueue(); await self.runtime.verify()
        async def fail(*args):
            return {'passed':False,'issues':['来源中的日期发生变化']}
        self.runtime.content.check=fail
        await self.runtime.tick()
        failed=self.runtime.queue.get(row['id'])
        self.assertEqual(failed['status'],'failed')
        self.assertEqual(failed['prepublication_check']['issues'],['来源中的日期发生变化'])
        self.assertIn('来源中的日期发生变化',failed['reason'])
        slot=next(s for s in self.runtime.plans.load(self.now.date())['slots'] if s['id']==row['slot_id'])
        self.assertEqual(slot['reason'],failed['reason'])
        self.assertIn('来源中的日期发生变化',self.store.setting('publishing_logs',[])[-1]['message'])
        self.assertEqual(self.publisher.calls,0)
    async def test_pause_after_preflight_blocks_final_click(self):
        row,_=self.enqueue(); await self.runtime.verify()
        self.publisher.preflight_hook=lambda:self.runtime.pause('manual')
        await self.runtime.tick()
        self.assertEqual(self.publisher.calls,0)
        self.assertTrue(self.runtime.status()['paused'])
    async def test_edited_draft_cancels_frozen_queue(self):
        row,draft=self.enqueue(); await self.runtime.verify()
        self.store.edit_draft(draft['id'],'标题','改文',[])
        await self.runtime.tick()
        self.assertEqual(self.runtime.queue.get(row['id'])['status'],'cancelled'); self.assertEqual(self.publisher.calls,0)
    async def test_missed_slot_is_skipped_without_catchup(self):
        row,_=self.enqueue(); await self.runtime.verify(); self.now+=timedelta(seconds=61)
        await self.runtime.tick()
        self.assertEqual(self.runtime.queue.get(row['id'])['status'],'skipped'); self.assertEqual(self.publisher.calls,0)
    async def test_due_without_qualified_data_skips(self):
        self.plan(); await self.runtime.verify(); await self.runtime.tick()
        self.assertEqual(self.runtime.plans.load(self.now.date())['slots'][0]['status'],'skipped')
    async def test_ai_tools_preparation_uses_official_docs_not_hot_news_or_publication_age(self):
        from unittest.mock import AsyncMock, patch
        from workbench.ai_tools import PROFILE
        slot=self.plan(); self.now-=timedelta(minutes=5); await self.runtime.verify()
        self.runtime.ai_tools=True
        source={'url':'https://developers.openai.com/codex/skills/', 'content_id':'official:test',
                'kind':'official','title':'Skills','author':'Codex 官方文档','body':'当前官方功能正文'*30,
                'published_at':None,'verified_at':self.now.isoformat()}
        self.browser.official_detail=AsyncMock(return_value=source)
        self.runtime.content.generate=AsyncMock(return_value={'title':'Codex技能','body':'技能正文',
            'verify':[],'sources':[source],'options':{'profile':PROFILE,'topic':'AI'}})
        with patch('workbench.micro_runtime.OfficialResearch.discover',new=AsyncMock(return_value=[('AI','official',source)])), patch('workbench.micro_runtime.TechNewsResearch.discover',new=AsyncMock(return_value=[])):
            await self.runtime.prepare()
        self.assertEqual(self.browser.calls,[])
        self.assertEqual(len(self.runtime.queue.items()),1)
        self.assertEqual(self.runtime.queue.items()[0]['topic'],'AI')
        self.runtime.content.generate.assert_awaited_once()
        self.assertIn('recent_titles',self.runtime.content.generate.call_args.kwargs)
    async def test_ai_profile_can_queue_technology_news_with_detail_source_checks(self):
        from unittest.mock import AsyncMock, patch
        from workbench.ai_tools import PROFILE
        self.plan();self.now-=timedelta(minutes=5);await self.runtime.verify()
        self.runtime.ai_tools=True
        source={'url':'https://www.toutiao.com/article/7701979050666902079/','content_id':'7701979050666902079',
                'kind':'article','title':'机器人新进展','author':'科技日报','body':'甲公司发布园区搬运机器人。',
                'published_at':self.now.isoformat(),'collected_at':self.now.isoformat()}
        self.browser.detail=AsyncMock(return_value=source)
        self.runtime.content.generate=AsyncMock(return_value={'title':'机器人新进展','body':'机器人新闻正文','verify':[],
            'sources':[source],'options':{'profile':PROFILE,'topic':'科技','content_type':'tech-news'}})
        with patch('workbench.micro_runtime.OfficialResearch.discover',new=AsyncMock(return_value=[])) as guides, patch('workbench.micro_runtime.TechNewsResearch.discover',new=AsyncMock(return_value=[('科技','article',source)])) as news:
            await self.runtime.prepare()
        self.assertEqual(len(self.runtime.queue.items()),1)
        self.assertEqual(self.runtime.queue.items()[0]['topic'],'科技')
        self.assertEqual(guides.call_args.args[-1],20)
        self.assertEqual(news.call_args.args[-1],10)
    async def test_ai_prepares_two_hour_buffer_and_retries_missing_slots_after_five_minutes(self):
        self.plan();self.now-=timedelta(minutes=5);await self.runtime.verify();self.runtime.ai_tools=True
        slots=self.runtime._preparation_slots(self.now)
        self.assertTrue(slots)
        self.assertTrue(any(datetime.fromisoformat(s['scheduled_at'])>self.now+timedelta(hours=1) for s in slots))
        self.runtime._last_prepare=self.now-timedelta(minutes=5)
        self.assertTrue(self.runtime._preparation_due(self.now))
        self.runtime._last_prepare=self.now-timedelta(minutes=4)
        self.assertFalse(self.runtime._preparation_due(self.now))

    async def test_ai_recovers_review_draft_once_before_discovery_and_queues_only_after_checks(self):
        from unittest.mock import AsyncMock,patch
        from workbench.ai_tools import PROFILE
        self.plan();self.now-=timedelta(minutes=5);await self.runtime.verify();self.runtime.ai_tools=True
        source={'url':'https://www.workbuddy.cn/docs/experts','kind':'official','body':'官方专家中心操作说明',
                'author':'WorkBuddy 官方文档','content_id':'official:expert','verified_at':self.now.isoformat()}
        draft=self.store.save_generated('review','source',1,{'title':'专家中心','body':'有来源的具体用途','verify':['标签排版待修整'],
            'sources':[source],'source_snapshots':[source],'options':{'profile':PROFILE,'topic':'AI'}})
        self.browser.official_detail=AsyncMock(return_value=source)
        self.runtime.content.repair=AsyncMock(side_effect=lambda old,*args:{**old,'verify':[],'editorial_review':['标签排版待修整']})
        self.runtime.content.check=AsyncMock(return_value={'passed':True,'issues':[]})
        with patch('workbench.micro_runtime.OfficialResearch.discover',new=AsyncMock(return_value=[])),patch('workbench.micro_runtime.TechNewsResearch.discover',new=AsyncMock(return_value=[])):
            await self.runtime.prepare();await self.runtime.prepare()
        self.runtime.content.repair.assert_awaited_once()
        self.assertEqual(len(self.runtime.queue.items()),1)
        self.assertEqual(self.runtime.queue.items()[0]['status'],'pending')
        self.assertEqual(self.publisher.calls,0)
    async def test_ai_initial_verify_gets_one_repair_before_guard_and_failed_qa_does_not_take_slot(self):
        from unittest.mock import AsyncMock,patch
        from workbench.ai_tools import PROFILE
        self.plan();self.now-=timedelta(minutes=5);await self.runtime.verify();self.runtime.ai_tools=True
        source={'url':'https://www.workbuddy.cn/docs/feature','kind':'official','body':'官方具体功能正文',
                'author':'WorkBuddy 官方文档','content_id':'official:feature','verified_at':self.now.isoformat()}
        self.browser.official_detail=AsyncMock(return_value=source)
        draft={'title':'具体功能','body':'有依据的用法','verify':['正文没有采用的价格未披露'],'sources':[source],
               'options':{'profile':PROFILE,'topic':'AI'}}
        self.runtime.content.generate=AsyncMock(return_value=draft)
        self.runtime.content.repair=AsyncMock(side_effect=lambda old,*args:{**old,'verify':[]})
        self.runtime.content.check=AsyncMock(return_value={'passed':True,'issues':[]})
        async def inspect(candidate,*args):
            self.assertEqual(candidate['verify'],[])
            return {'decision':'clear'}
        self.guard.inspect=inspect
        with patch('workbench.micro_runtime.OfficialResearch.discover',new=AsyncMock(return_value=[('AI','official',source)])),patch('workbench.micro_runtime.TechNewsResearch.discover',new=AsyncMock(return_value=[])):
            await self.runtime.prepare()
        self.runtime.content.repair.assert_awaited_once()
        self.assertEqual(self.runtime.queue.items()[0]['status'],'pending')

    async def test_ai_failed_quality_review_preserves_draft_without_occupying_future_slot(self):
        from unittest.mock import AsyncMock,patch
        from workbench.ai_tools import PROFILE
        self.plan();self.now-=timedelta(minutes=5);await self.runtime.verify();self.runtime.ai_tools=True
        source={'url':'https://www.workbuddy.cn/docs/unverified','kind':'official','body':'官方资料正文',
                'author':'WorkBuddy 官方文档','content_id':'official:unverified','verified_at':self.now.isoformat()}
        self.browser.official_detail=AsyncMock(return_value=source)
        self.runtime.content.generate=AsyncMock(return_value={'title':'功能待查','body':'存在核心疑点','verify':[],
            'sources':[source],'options':{'profile':PROFILE,'topic':'AI'}})
        self.runtime.content.repair=AsyncMock(side_effect=lambda old,*args:{**old,'verify':['核心事实缺少证据']})
        self.runtime.content.check=AsyncMock(return_value={'passed':False,'issues':['核心事实缺少证据']})
        with patch('workbench.micro_runtime.OfficialResearch.discover',new=AsyncMock(return_value=[('AI','official',source)])),patch('workbench.micro_runtime.TechNewsResearch.discover',new=AsyncMock(return_value=[])):
            await self.runtime.prepare()
        self.assertEqual(self.runtime.queue.items(),[])
        self.assertTrue(self.store.drafts()[0]['verify'])
        self.assertTrue(self.runtime._preparation_slots(self.now))
        self.runtime.content.repair.assert_awaited_once()
    async def test_review_recovery_does_not_rewrite_history_uncertainty(self):
        from unittest.mock import AsyncMock
        from workbench.ai_tools import PROFILE
        self.plan();self.now-=timedelta(minutes=5);await self.runtime.verify();self.runtime.ai_tools=True
        self.store.save_generated('uncertain','source',1,{'title':'事件待查','body':'稿件',
            'verify':['事件是否重复尚不明确，须人工核实'],'sources':[{'url':'https://www.workbuddy.cn/docs/event','kind':'official'}],
            'options':{'profile':PROFILE,'topic':'AI'}})
        self.browser.official_detail=AsyncMock()
        self.runtime.content.repair=AsyncMock()
        await self.runtime._recover_review_drafts()
        self.runtime.content.repair.assert_not_awaited()
        self.browser.official_detail.assert_not_awaited()
    async def test_restart_publishing_becomes_uncertain_and_pauses(self):
        row,_=self.enqueue(); self.runtime.queue.update(row['id'],{'status':'publishing'})
        self.runtime.plans.update_slot(row['slot_id'],{'status':'publishing'})
        await self.runtime.start(); await self.runtime.close()
        self.assertEqual(self.runtime.queue.get(row['id'])['status'],'uncertain'); self.assertTrue(self.runtime.status()['paused'])
    async def test_reconcile_never_submits(self):
        row,_=self.enqueue(); self.runtime.queue.update(row['id'],{'status':'publishing'}); self.runtime.queue.update(row['id'],{'status':'uncertain','reason':'发布结果待核实'})
        self.runtime.plans.update_slot(row['slot_id'],{'status':'publishing'}); self.runtime.plans.update_slot(row['slot_id'],{'status':'uncertain'})
        await self.runtime.reconcile(row['id'])
        self.assertEqual(self.publisher.calls,0); self.assertEqual(self.runtime.queue.get(row['id'])['status'],'published')
        self.assertEqual(self.runtime.queue.get(row['id'])['reason'],'')
    async def test_prepare_only_future_hour_and_browser_steps_lock(self):
        self.plan(); self.now-=timedelta(minutes=5); await self.runtime.verify()
        await self.runtime.prepare()
        self.assertEqual(len(self.browser.calls),6)
        self.assertEqual(sum(c[2] for c in self.browser.calls),30)
    async def test_new_day_gets_distinct_immutable_plan(self):
        self.plan(); old=self.runtime.plans.load(self.now.date()); self.now+=timedelta(days=1)
        await self.runtime.tick(); self.assertEqual(self.runtime.plans.load(old['date'])['target'],old['target'])
        self.assertIsNotNone(self.runtime.plans.load(self.now.date()))
    async def test_timeout_is_uncertain_and_never_retried(self):
        row,_=self.enqueue(); await self.runtime.verify()
        async def timeout(draft):
            self.publisher.calls+=1
            raise TimeoutError('secret browser token')
        self.publisher.submit=timeout
        await self.runtime.tick(); await self.runtime.tick()
        self.assertEqual(self.publisher.calls,1)
        self.assertEqual(self.runtime.queue.get(row['id'])['status'],'uncertain')
        self.assertNotIn('secret',str(self.store.setting('publishing_logs')))
    async def test_final_click_rechecks_draft_edit_during_preflight(self):
        row,draft=self.enqueue(); await self.runtime.verify()
        self.publisher.preflight_hook=lambda:self.store.edit_draft(draft['id'],'标题','新正文',[])
        await self.runtime.tick()
        self.assertEqual(self.publisher.calls,0)
    async def test_final_click_rechecks_binding_even_after_new_verified_probe(self):
        row,_=self.enqueue(); await self.runtime.verify()
        def switch_account():
            self.store.set_setting('publishing_account',{'id':'other'})
            self.store.set_setting('publishing_verification',{'account_id':'other','declaration_verified':True,'ai_declaration':False})
        self.publisher.preflight_hook=switch_account
        await self.runtime.tick()
        self.assertEqual(self.publisher.calls,0)
    async def test_minimum_spacing_and_daily_cap_use_all_possible_ledger_records(self):
        from workbench.publication_ledger import PublicationLedger
        self.guard.ledger=PublicationLedger(self.store)
        row,_=self.enqueue(); await self.runtime.verify()
        self.guard.ledger.record_verified({'body':'其他事件','sources':[{'url':'https://www.toutiao.com/article/999999999999/'}]},'account',{'status':'uncertain','content_id':'999'})
        record=self.guard.ledger.records('account')[0]
        record['created_at']=self.now.isoformat()
        with self.store.connection() as db:
            db.execute('UPDATE publication_history SET payload=? WHERE id=?',(self.store.dump(record),record['id']))
        await self.runtime.tick()
        self.assertEqual(self.publisher.calls,0)
        self.assertEqual(self.runtime.queue.get(row['id'])['status'],'skipped')
    async def test_daily_platform_count_blocks_at_target(self):
        row,_=self.enqueue(); await self.runtime.verify()
        plan=self.runtime.plans.load(self.now.date())
        self.store.set_setting('publishing_daily_count',{'date':self.now.date().isoformat(),'count':plan['target']})
        await self.runtime.tick(); self.assertEqual(self.publisher.calls,0)
    async def test_failed_probe_invalidates_old_verification_and_pauses(self):
        await self.runtime.verify()
        async def fail(): raise ValueError('secret')
        self.publisher.probe=fail
        with self.assertRaises(ValueError): await self.runtime.verify()
        self.assertFalse(self.runtime.status()['ready']); self.assertTrue(self.runtime.status()['paused'])
    async def test_preparation_persists_original_sources_and_never_reuses_origin(self):
        self.plan(); self.now-=timedelta(minutes=5); await self.runtime.verify()
        async def search(word,kind,limit,exclude_urls=None,since=None):
            return [{'url':'https://www.toutiao.com/article/123456789012/','published_at':self.now.isoformat(),'comments':99,'likes':7}]
        self.browser.search=search
        async def generate(sources,topic):
            self.assertFalse(self.browser.lock.locked())
            return {'title':'标题','body':'正文','sources':[{'url':sources[0]['url']}],'verify':[]}
        self.runtime.content.generate=generate
        await self.runtime.prepare(); await self.runtime.prepare()
        self.assertEqual(len(self.runtime.queue.items()),1)
        draft=self.store.draft(self.runtime.queue.items()[0]['draft_id'])
        self.assertEqual(draft['source_snapshots'][0]['body'],'来源正文')
        self.assertEqual(draft['source_snapshots'][0]['comments'],99)
    async def test_day_rollover_expires_old_queued_slots(self):
        row,_=self.enqueue(); await self.runtime.verify(); self.now+=timedelta(days=1)
        await self.runtime.tick()
        self.assertEqual(self.runtime.queue.get(row['id'])['status'],'skipped')
    async def test_recovered_uncertain_links_ledger_identity(self):
        from workbench.publication_ledger import PublicationLedger
        self.guard.ledger=PublicationLedger(self.store)
        row,draft=self.enqueue()
        record=self.guard.ledger.record_verified(draft,'account',{'status':'uncertain','content_id':'888'})
        self.runtime.queue.update(row['id'],{'status':'publishing'}); self.runtime.plans.update_slot(row['slot_id'],{'status':'publishing'})
        await self.runtime.start(); await self.runtime.close()
        self.assertEqual(self.runtime.queue.get(row['id']).get('publication_id'),record['id'])
    async def test_ai_setting_change_requires_new_actual_verification(self):
        await self.runtime.verify()
        self.store.set_setting('publishing',{'enabled':True,'ai_declaration':True})
        self.assertFalse(self.runtime.status()['ready'])
    async def test_unexpected_exclusive_receipt_pauses_and_records_platform_count(self):
        row,_=self.enqueue(); await self.runtime.verify()
        async def submit(draft):
            return {'status':'submitted','unexpected_exclusive_tag':True,'visible_today_count':4}
        self.publisher.submit=submit
        await self.runtime.tick()
        self.assertTrue(self.runtime.status()['paused'])
        self.assertEqual(self.store.setting('publishing_daily_count')['count'],4)
    async def test_confirm_requires_unchanged_future_draft_and_rechecks_sources(self):
        slot=self.plan(); self.now-=timedelta(minutes=5); await self.runtime.verify()
        draft=self.store.save_generated('t','s',1,{'title':'标题','body':'正文','sources':[{'url':'https://www.toutiao.com/article/123456789012/'}],'verify':[],'source_snapshots':[{'body':'原始'}]})
        row=self.runtime.queue.add(slot,draft,'民生','event',status='awaiting_confirmation')
        self.runtime.plans.update_slot(slot['id'],{'status':'awaiting_confirmation','queue_id':row['id']})
        with self.assertRaises(ValueError): await self.runtime.confirm(row['id'],'wrong')
        confirmed=await self.runtime.confirm(row['id'],row['body_sha256'])
        self.assertEqual(confirmed['status'],'pending')
        self.assertEqual(self.runtime.plans.load(self.now.date())['slots'][0]['status'],'queued')
    async def test_confirm_rejects_edited_snapshot(self):
        row,draft=self.enqueue(); self.now-=timedelta(minutes=5); await self.runtime.verify()
        self.store.edit_draft(draft['id'],'标题','改文',[])
        with self.assertRaises(ValueError): await self.runtime.confirm(row['id'],row['body_sha256'])
    async def test_reject_cannot_release_possible_submission(self):
        row,_=self.enqueue(); await self.runtime.reject(row['id'])
        self.assertEqual(self.runtime.queue.get(row['id'])['status'],'cancelled')
        with self.assertRaises(ValueError): await self.runtime.reject(row['id'])
    async def test_unexpected_public_ai_tag_pauses_without_rewriting_receipt(self):
        row,_=self.enqueue(); await self.runtime.verify()
        async def submit(draft): return {'status':'published','unexpected_ai_tag':True}
        self.publisher.submit=submit
        await self.runtime.tick()
        self.assertTrue(self.runtime.status()['paused'])
        self.assertEqual(self.runtime.queue.get(row['id'])['status'],'published')
    async def test_missing_model_configuration_is_not_ready(self):
        await self.runtime.verify()
        self.runtime.llm.status=lambda:{'configured':False}
        self.assertFalse(self.runtime.status()['ready'])
        self.assertIn('模型',self.runtime.status()['reason'])
    async def test_outside_window_is_idle_and_reports_service_activity(self):
        await self.runtime.verify(); self.now=self.now.replace(hour=23)
        self.assertEqual(self.runtime.status()['state'],'idle')
        self.assertIn('08:00',self.runtime.status()['reason'])
        self.assertFalse(self.runtime.status()['service_active'])
    async def test_background_browser_waiter_rechecks_publication_priority(self):
        calls=[]
        async def method(): calls.append('called')
        await self.browser.lock.acquire()
        task=asyncio.create_task(self.runtime._browser_step(method))
        await asyncio.sleep(0)
        self.browser.publishing_idle.clear(); self.browser.lock.release()
        await asyncio.sleep(0); await asyncio.sleep(0)
        try: self.assertEqual(calls,[])
        finally:
            self.browser.publishing_idle.set(); await task
    async def test_verify_waits_for_active_publication_before_probe_or_close(self):
        row,_=self.enqueue(); await self.runtime.verify()
        entered=asyncio.Event(); release=asyncio.Event(); probes=[]
        async def preflight(draft):
            entered.set(); await release.wait(); return {}
        async def probe():
            probes.append('probe'); return {'account_id':'account','declaration_verified':True,'ai_declaration':False}
        self.publisher.preflight=preflight; self.publisher.probe=probe
        tick=asyncio.create_task(self.runtime.tick()); await entered.wait()
        closes=self.publisher.closed
        verify=asyncio.create_task(self.runtime.verify()); await asyncio.sleep(0); await asyncio.sleep(0)
        try:
            self.assertEqual(probes,[])
            self.assertEqual(self.publisher.closed,closes)
        finally:
            release.set(); await asyncio.gather(tick,verify)
        self.assertEqual(self.publisher.calls,1)
        self.assertEqual(probes,['probe'])
    async def test_reconcile_waits_for_another_publisher_operation(self):
        row,_=self.enqueue()
        self.runtime.queue.update(row['id'],{'status':'publishing'}); self.runtime.queue.update(row['id'],{'status':'uncertain'})
        self.runtime.plans.update_slot(row['slot_id'],{'status':'publishing'}); self.runtime.plans.update_slot(row['slot_id'],{'status':'uncertain'})
        calls=[]
        async def reconcile(value): calls.append('reconcile'); return {'status':'submitted','content_id':'123'}
        self.publisher.reconcile=reconcile
        await self.runtime._tick_lock.acquire()
        task=asyncio.create_task(self.runtime.reconcile(row['id'])); await asyncio.sleep(0); await asyncio.sleep(0)
        try:
            self.assertEqual(calls,[]); self.assertEqual(self.publisher.closed,0)
        finally:
            self.runtime._tick_lock.release(); await task
        self.assertEqual(calls,['reconcile'])
    async def test_close_waits_for_probe_before_releasing_its_page(self):
        entered=asyncio.Event(); release=asyncio.Event()
        async def probe():
            entered.set(); await release.wait()
            return {'account_id':'account','declaration_verified':True,'ai_declaration':False}
        self.publisher.probe=probe
        verify=asyncio.create_task(self.runtime.verify()); await entered.wait()
        closing=asyncio.create_task(self.runtime.close()); await asyncio.sleep(0); await asyncio.sleep(0)
        try: self.assertEqual(self.publisher.closed,0)
        finally:
            release.set(); await asyncio.gather(verify,closing)
    async def test_cancelled_close_finishes_page_cleanup_after_active_probe(self):
        entered=asyncio.Event(); release=asyncio.Event()
        async def probe():
            entered.set(); await release.wait()
            return {'account_id':'account','declaration_verified':True,'ai_declaration':False}
        self.publisher.probe=probe
        verify=asyncio.create_task(self.runtime.verify()); await entered.wait()
        closing=asyncio.create_task(self.runtime.close()); await asyncio.sleep(0)
        closing.cancel(); release.set(); await verify
        with self.assertRaises(asyncio.CancelledError): await closing
        self.assertGreaterEqual(self.publisher.closed,2)
        self.assertTrue(self.browser.publishing_idle.is_set())
    async def test_stage_edited_draft_into_next_free_slot_after_fresh_checks(self):
        row,draft=self.enqueue(); self.now-=timedelta(minutes=5); await self.runtime.verify()
        await self.runtime.reject(row['id'])
        edited=self.store.edit_draft(draft['id'],'新标题','修改后的正文',[])
        sha=hashlib.sha256(edited['body'].encode()).hexdigest()
        staged=await self.runtime.stage(draft['id'],'民生',sha)
        self.assertEqual(staged['body'],'修改后的正文')
        self.assertEqual(staged['body_sha256'],sha)
        self.assertEqual(staged['status'],'pending')
        self.assertNotEqual(staged['slot_id'],row['slot_id'])
    async def test_stage_refuses_incorrect_sha_and_failed_fresh_content_check(self):
        row,draft=self.enqueue(); self.now-=timedelta(minutes=5); await self.runtime.verify(); await self.runtime.reject(row['id'])
        with self.assertRaises(ValueError): await self.runtime.stage(draft['id'],'民生','wrong')
        async def fail(*args): return {'passed':False,'issues':['来源冲突']}
        self.runtime.content.check=fail
        with self.assertRaises(ValueError): await self.runtime.stage(draft['id'],'民生',row['body_sha256'])
        self.assertEqual(len(self.runtime.queue.items()),1)
    async def test_restart_repairs_plan_after_queue_result_was_persisted(self):
        row,_=self.enqueue()
        self.runtime.queue.update(row['id'],{'status':'publishing'})
        self.runtime.queue.update(row['id'],{'status':'submitted','publication_id':'ledger-id','receipt':{'status':'submitted','content_id':'777'}})
        self.runtime.plans.update_slot(row['slot_id'],{'status':'publishing'})
        await self.runtime.start(); await self.runtime.close()
        slot=next(s for s in self.runtime.plans.load(self.now.date())['slots'] if s['id']==row['slot_id'])
        self.assertEqual(slot['status'],'submitted'); self.assertEqual(slot['publication_id'],'ledger-id')
    async def test_resume_does_not_clear_pause_received_during_probe(self):
        self.runtime.pause('initial pause')
        entered=asyncio.Event(); release=asyncio.Event()
        async def probe():
            entered.set(); await release.wait()
            return {'account_id':'account','declaration_verified':True,'ai_declaration':False}
        self.publisher.probe=probe
        resuming=asyncio.create_task(self.runtime.resume()); await entered.wait()
        self.runtime.pause('new user pause'); release.set()
        try: await resuming
        except Exception: pass
        self.assertTrue(self.runtime.status()['paused'])
        self.assertEqual(self.runtime.status()['reason'],'new user pause')
        self.assertFalse(self.runtime.status()['ready'])
    async def test_ai_change_during_probe_cannot_write_stale_verification(self):
        entered=asyncio.Event(); release=asyncio.Event()
        async def probe():
            entered.set(); await release.wait()
            return {'account_id':'account','declaration_verified':True,'ai_declaration':False}
        self.publisher.probe=probe
        verifying=asyncio.create_task(self.runtime.verify()); await entered.wait()
        self.store.set_setting('publishing',{'enabled':True,'ai_declaration':True})
        self.runtime.pause('AI settings changed'); self.store.set_setting('publishing_verification',{})
        release.set()
        with self.assertRaises(Exception): await verifying
        self.assertEqual(self.store.setting('publishing_verification'),{})
        self.assertEqual(self.runtime.status()['reason'],'AI settings changed')
    async def test_failed_probe_preserves_newer_user_pause_reason(self):
        entered=asyncio.Event(); release=asyncio.Event()
        async def probe(): entered.set(); await release.wait(); raise ValueError('probe unavailable')
        self.publisher.probe=probe
        verifying=asyncio.create_task(self.runtime.verify()); await entered.wait()
        self.runtime.pause('new explicit pause'); release.set()
        with self.assertRaises(ValueError): await verifying
        self.assertEqual(self.runtime.status()['reason'],'new explicit pause')
    async def test_reconcile_public_receipt_anomaly_pauses_and_updates_daily_count(self):
        row,_=self.enqueue(); self.runtime.queue.update(row['id'],{'status':'publishing'}); self.runtime.queue.update(row['id'],{'status':'submitted'})
        self.runtime.plans.update_slot(row['slot_id'],{'status':'publishing'}); self.runtime.plans.update_slot(row['slot_id'],{'status':'submitted'})
        async def reconcile(value):
            return {'status':'published','content_id':'123','unexpected_exclusive_tag':True,'visible_today_count':9}
        self.publisher.reconcile=reconcile
        result=await self.runtime.reconcile(row['id'])
        self.assertEqual(result['status'],'published'); self.assertTrue(self.runtime.status()['paused'])
        self.assertEqual(self.store.setting('publishing_daily_count')['count'],9)
    async def test_reconcile_unexpected_ai_label_pauses_preserving_published(self):
        row,_=self.enqueue(); self.runtime.queue.update(row['id'],{'status':'publishing'}); self.runtime.queue.update(row['id'],{'status':'uncertain'})
        self.runtime.plans.update_slot(row['slot_id'],{'status':'publishing'}); self.runtime.plans.update_slot(row['slot_id'],{'status':'uncertain'})
        async def reconcile(value): return {'status':'published','content_id':'123','unexpected_ai_tag':True}
        self.publisher.reconcile=reconcile
        result=await self.runtime.reconcile(row['id'])
        self.assertEqual(result['status'],'published'); self.assertTrue(self.runtime.status()['paused'])
        self.assertIn('AI',self.runtime.status()['reason'])
    async def test_empty_search_combination_does_not_abort_remaining_five(self):
        self.plan(); self.now-=timedelta(minutes=5); await self.runtime.verify()
        calls=[]
        async def search(*args):
            calls.append(args)
            if len(calls)==1: raise ValueError('此组合未取得结果')
            return []
        self.browser.search=search
        await self.runtime.prepare()
        self.assertEqual(len(calls),6)
        logs=str(self.store.setting('publishing_logs'))
        self.assertIn('开始本轮',logs); self.assertIn('发现候选',logs); self.assertIn('本轮完成',logs)
    async def test_normal_queue_and_submission_progress_are_logged_without_body(self):
        row,_=self.enqueue(); await self.runtime.verify(); await self.runtime.tick()
        logs=str(self.store.setting('publishing_logs'))
        self.assertIn('提交结果',logs); self.assertIn('submitted',logs)
        self.assertNotIn('来源正文',logs)
    async def test_missed_slot_logs_skip_reason(self):
        row,_=self.enqueue(); await self.runtime.verify(); self.now+=timedelta(seconds=61)
        await self.runtime.tick()
        self.assertIn('跳过排期',str(self.store.setting('publishing_logs')))
    async def test_guard_review_keeps_paid_generated_draft_with_sources_without_queue(self):
        self.plan(); self.now-=timedelta(minutes=5); await self.runtime.verify()
        async def search(word,kind,limit,exclude_urls=None,since=None):
            return [{'url':'https://www.toutiao.com/article/123456789012/','published_at':self.now.isoformat()}]
        async def generate(sources,topic):
            return {'title':'可人工处理的稿件','body':'已经生成的正文','sources':[{'url':sources[0]['url']}],'verify':['原待核实项']}
        async def review(*args): return {'decision':'review','reasons':['历史回执尚未核实','原待核实项']}
        self.browser.search=search; self.runtime.content.generate=generate; self.guard.inspect=review
        await self.runtime.prepare()
        self.assertEqual(self.runtime.queue.items(),[])
        drafts=self.store.drafts(); self.assertEqual(len(drafts),1)
        self.assertEqual(drafts[0]['body'],'已经生成的正文')
        self.assertEqual(drafts[0]['verify'],['原待核实项','历史回执尚未核实'])
        self.assertEqual(drafts[0]['source_snapshots'][0]['body'],'来源正文')
        self.assertTrue(all(s['status']=='pending' for s in self.runtime.plans.load(self.now.date())['slots']))
        logs=str(self.store.setting('publishing_logs'))
        self.assertIn('待人工处理',logs); self.assertIn('历史回执尚未核实',logs); self.assertIn('来源已处理',logs)
    async def test_guard_duplicate_logs_reason_without_saving_review_or_queue(self):
        self.plan(); self.now-=timedelta(minutes=5); await self.runtime.verify()
        async def search(word,kind,limit,exclude_urls=None,since=None): return [{'url':'https://www.toutiao.com/article/123456789012/','published_at':self.now.isoformat()}]
        async def generate(sources,topic): return {'title':'同事件','body':'已生成','sources':[{'url':sources[0]['url']}],'verify':[]}
        async def duplicate(*args): return {'decision':'duplicate','reasons':['同一事件已有发布记录']}
        self.browser.search=search; self.runtime.content.generate=generate; self.guard.inspect=duplicate
        await self.runtime.prepare()
        self.assertEqual(self.store.drafts(),[]); self.assertEqual(self.runtime.queue.items(),[])
        self.assertIn('同一事件已有发布记录',str(self.store.setting('publishing_logs')))

    async def test_start_compacts_old_resolved_queue_but_preserves_draft(self):
        row, draft = self.enqueue()
        self.runtime.queue.update(row['id'], {'status': 'skipped'})
        old = self.runtime.queue.get(row['id'])
        old['created_at'] = (self.now - timedelta(days=4)).isoformat()
        with self.store.connection() as db:
            db.execute('UPDATE micro_publish_queue SET payload=? WHERE id=?', (self.store.dump(old), row['id']))
        await self.runtime.start()
        await self.runtime.close()
        self.assertTrue(self.runtime.queue.get(row['id'])['history_compacted'])
        self.assertEqual(self.store.draft(draft['id'])['body'], draft['body'])

    async def test_staging_does_not_send_old_queue_body_to_model(self):
        row, draft = self.enqueue()
        self.runtime.queue.update(row['id'], {'status': 'publishing'})
        self.runtime.queue.update(row['id'], {'status': 'published'})
        old = self.runtime.queue.get(row['id'])
        old['created_at'] = (self.now - timedelta(days=4)).isoformat()
        with self.store.connection() as db:
            db.execute('UPDATE micro_publish_queue SET payload=? WHERE id=?', (self.store.dump(old), row['id']))
        await self.runtime.verify()
        candidate = self.store.save_generated('new', 'new', 1, {
            'title': '新稿', 'body': '完全不同的事件', 'verify': [],
            'sources': [{'url': 'https://www.toutiao.com/article/123456789013/'}],
            'source_snapshots': [{'body': '来源'}]})
        calls = []
        async def judge(candidate, history):
            calls.append(history)
            return {'decision': 'uncertain'}
        self.runtime.llm.judge_duplicate = judge
        import hashlib
        queued = await self.runtime.stage(candidate['id'], 'AI', hashlib.sha256(candidate['body'].encode()).hexdigest())
        self.assertEqual(queued['status'], 'pending')
        self.assertEqual(calls, [])

    async def test_preflight_failure_is_failed_not_uncertain_and_reason_is_logged(self):
        from workbench.browser import NeedsAttention
        row,_=self.enqueue(); await self.runtime.verify()
        async def blocked(draft):raise NeedsAttention('微头条编辑器已有内容，已暂停以免覆盖')
        self.publisher.preflight=blocked
        await self.runtime.tick()
        record=self.runtime.queue.get(row['id'])
        self.assertEqual(record['status'],'failed')
        self.assertIn('编辑器已有内容',record['reason'])
        self.assertEqual(self.runtime.plans.load(self.now.date())['slots'][0]['status'],'failed')
        self.assertIn('编辑器已有内容',str(self.store.setting('publishing_logs')))
        self.assertEqual(self.publisher.calls,0)

    async def test_unknown_exception_before_submit_never_logs_secret(self):
        row,_=self.enqueue(); await self.runtime.verify()
        async def blocked(draft):raise ValueError('secret browser token')
        self.publisher.preflight=blocked
        await self.runtime.tick()
        self.assertEqual(self.runtime.queue.get(row['id'])['status'],'failed')
        self.assertNotIn('secret',str(self.store.setting('publishing_logs')))

    async def test_real_guard_preflight_failure_keeps_failed_attempt_identity(self):
        from workbench.browser import NeedsAttention
        from workbench.publication_ledger import PublicationLedger
        from workbench.publication_guard import PublicationGuard
        row,_=self.enqueue(); await self.runtime.verify()
        ledger=PublicationLedger(self.store)
        async def judge(*args):return {'decision':'distinct'}
        self.runtime.guard=PublicationGuard(ledger,judge,clock=lambda:self.now,expected_ai=lambda:False)
        async def blocked(draft):raise NeedsAttention('微头条编辑器已有其他内容，已暂停以免覆盖')
        self.publisher.preflight=blocked
        await self.runtime.tick()
        record=self.runtime.queue.get(row['id'])
        self.assertEqual(record['status'],'failed')
        self.assertEqual(ledger.get(record['publication_id'])['status'],'failed_before_submit')
        self.assertEqual(self.publisher.calls,0)

    async def test_restart_preserves_terminal_failed_slot(self):
        row,_=self.enqueue()
        self.runtime.queue.update(row['id'],{'status':'failed','reason':'未提交'})
        self.runtime.plans.update_slot(row['slot_id'],{'status':'failed','reason':'未提交'})
        await self.runtime.start();await self.runtime.close()
        slot=next(s for s in self.runtime.plans.load(self.now.date())['slots'] if s['id']==row['slot_id'])
        self.assertEqual(slot['status'],'failed')

    async def test_skipping_unqualified_slot_preserves_quality_reason(self):
        row,_=self.enqueue()
        self.runtime.queue.update(row['id'],{'reason':'稿件不属于民生，且会议日期缺少支持'})
        self.runtime._skip(row['slot_id'],self.runtime.queue.get(row['id']),'当前排期没有通过检查的稿件')
        self.assertIn('会议日期缺少支持',self.runtime.queue.get(row['id'])['reason'])
        self.assertIn('会议日期缺少支持',str(self.store.setting('publishing_logs')))

    async def test_staging_reports_quality_reason_before_duplicate_model_call(self):
        from unittest.mock import AsyncMock
        row,draft=self.enqueue();self.now-=timedelta(minutes=5);await self.runtime.verify()
        self.runtime.content.check=AsyncMock(return_value={'passed':False,'issues':['会议日期缺少来源支持']})
        self.runtime.guard.inspect=AsyncMock(return_value={'decision':'clear'})
        with self.assertRaisesRegex(ValueError,'会议日期缺少来源支持'):
            await self.runtime.stage(draft['id'],'AI',row['body_sha256'])
        self.runtime.guard.inspect.assert_not_awaited()

    async def test_prepare_repairs_once_rechecks_and_uses_actual_topic(self):
        from unittest.mock import AsyncMock
        self.plan();self.now-=timedelta(minutes=5);await self.runtime.verify()
        async def search(*args):return [{'url':'https://www.toutiao.com/article/123456789012/','published_at':self.now.isoformat()}]
        self.browser.search=search
        async def generate(sources,topic):return {'title':'AI会议','body':'未经支持的成效','verify':[], 'sources':[{'url':sources[0]['url']}], 'options':{'topic':topic}}
        async def repair(draft,sources,topic,issues):return {**draft,'body':'来源报道会议讨论AI应用','options':{'topic':'AI'},'editorial_review':issues}
        self.runtime.content.generate=generate
        self.runtime.content.repair=AsyncMock(side_effect=repair)
        self.runtime.content.check=AsyncMock(side_effect=[{'passed':False,'issues':['删除未支持成效']},{'passed':True,'issues':[]}])
        await self.runtime.prepare()
        self.runtime.content.repair.assert_awaited_once()
        self.assertEqual(self.runtime.content.check.await_count,2)
        row=self.runtime.queue.items()[0]
        self.assertEqual(row['topic'],'AI')
        self.assertEqual(row['status'],'pending')
        self.assertNotIn('未经支持',row['body'])
        self.assertEqual(self.store.draft(row['draft_id'])['editorial_review'],['删除未支持成效'])

    async def test_unknown_list_time_is_verified_on_detail_page(self):
        self.plan();self.now-=timedelta(minutes=5);await self.runtime.verify()
        async def search(*args):return [{'url':'https://www.toutiao.com/article/123456789012/','published_at':None}]
        self.browser.search=search
        async def generate(sources,topic):return {'title':'机器人展会','body':'机器人亮相展会','sources':[{'url':sources[0]['url']}],'verify':[]}
        self.runtime.content.generate=generate
        await self.runtime.prepare()
        self.assertEqual(len(self.runtime.queue.items()),1)
        self.assertEqual(self.runtime.queue.items()[0]['status'],'pending')

    async def test_missing_detail_time_still_blocks_generation(self):
        from unittest.mock import AsyncMock
        self.plan();self.now-=timedelta(minutes=5);await self.runtime.verify()
        async def search(*args):return [{'url':'https://www.toutiao.com/article/123456789012/','published_at':None}]
        self.browser.search=search
        self.browser.detail=AsyncMock(return_value={'url':'https://www.toutiao.com/article/123456789012/','body':'正文','published_at':None})
        self.runtime.content.generate=AsyncMock()
        await self.runtime.prepare()
        self.runtime.content.generate.assert_not_awaited()
        self.assertEqual(self.runtime.queue.items(),[])

    async def test_prepare_search_advances_past_processed_sources(self):
        self.plan();self.now-=timedelta(minutes=5);await self.runtime.verify()
        old='https://www.toutiao.com/article/123456789011/'
        new='https://www.toutiao.com/article/123456789012/'
        self.store.set_setting('micro_seen_sources',[old]);calls=[]
        async def search(word,kind,limit,exclude_urls=None,since=None):
            calls.append((exclude_urls,since))
            url=new if old in (exclude_urls or []) else old
            return [] if url in (exclude_urls or []) else [{'url':url,'published_at':self.now.isoformat()}]
        self.browser.search=search
        async def generate(sources,topic):return {'title':'新事件','body':'有来源的新事件','sources':[{'url':sources[0]['url']}],'verify':[]}
        self.runtime.content.generate=generate
        await self.runtime.prepare()
        self.assertEqual(len(self.runtime.queue.items()),1)
        self.assertTrue(all(old in excluded and since==self.now-timedelta(hours=24) for excluded,since in calls))
        self.assertEqual(self.runtime.queue.items()[0]['sources'][0]['url'],new)

    async def test_slow_generation_uses_next_future_slot_not_expired_slot(self):
        first=self.plan();self.now-=timedelta(minutes=5);await self.runtime.verify()
        async def search(*args):return [{'url':'https://www.toutiao.com/article/123456789012/','published_at':self.now.isoformat()}]
        self.browser.search=search
        async def generate(sources,topic):
            self.now=datetime.fromisoformat(first['scheduled_at'])+timedelta(minutes=2)
            await self.runtime.tick()
            return {'title':'新事件','body':'有来源的新事件','sources':[{'url':sources[0]['url']}],'verify':[]}
        self.runtime.content.generate=generate
        await self.runtime.prepare()
        rows=self.runtime.queue.items()
        self.assertEqual(len(rows),1)
        self.assertGreater(datetime.fromisoformat(rows[0]['scheduled_at']),self.now)
        self.assertNotEqual(rows[0]['slot_id'],first['id'])
