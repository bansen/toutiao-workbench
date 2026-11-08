Warning: truncated output (original token count: 19337)
Total output lines: 1144

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
    async def test_approved_unchanged_evidence_reuses_fact_check_but_changed_evidence_rechecks(self):
        from unittest.mock import AsyncMock
        from workbench.draft_review import approve
        from workbench.ai_tools import REVIEW_POLICY
        source={'url':'https://www.workbuddy.cn/docs/feature','kind':'official','body':'已经核对的完整事实'}
        draft=approve({'title':'标题','body':'正文','verify':[]},[source],self.now,REVIEW_POLICY)
        self.runtime.content.input_issues=lambda draft,sources:[]
        self.runtime.content.check=AsyncMock(return_value={'passed':True,'issues':[]})
        _,result=await self.runtime._qualify_ai(draft,[source],'AI',allow_repair=False)
        self.assertTrue(result['passed']);self.runtime.content.check.assert_not_awaited()
        await self.runtime._qualify_ai(draft,[{**source,'body':'当前事实已改变'}],'AI',allow_repair=False)
        self.assertEqual(self.runtime.content.check.await_count,1)
        self.now+=timedelta(hours=4)
        await self.runtime._qualify_ai(draft,[source],'AI',allow_repair=False)
        self.assertEqual(self.runtime.content.check.await_count,2)

    async def test_automatic_receipt_recheck_resumes_only_after_confirmed_result(self):
        row,_=self.enqueue();await self.runtime.verify()
        self.runtime.queue.update(row['id'],{'status':'publishing'})
        self.runtime.queue.update(row['id'],{'status':'uncertain'})
        self.runtime.plans.update_slot(row['slot_id'],{'status':'uncertain'})
        self.runtime.pause('上次发布回执不明确，禁止自动重发')
        self.now+=timedelta(minutes=2)
        await self.runtime._reconcile_paused()
        self.assertEqual(self.runtime.queue.get(row['id'])['status'],'published')
        self.assertFalse(self.runtime._paused())
        self.assertEqual(self.publisher.calls,0)

    async def test_receipt_recheck_preserves_manual_pause_and_has_bounded_attempts(self):
        from unittest.mock import AsyncMock
        row,_=self.enqueue();await self.runtime.verify()
        self.runtime.queue.update(row['id'],{'status':'publishing'})
        self.runtime.queue.update(row['id'],{'status':'uncertain'})
        self.runtime.plans.update_slot(row['slot_id'],{'status':'uncertain'})
        self.publisher.reconcile=AsyncMock(side_effect=ValueError('尚未匹配'))
        self.runtime.pause('人工暂停');self.now+=timedelta(minutes=2)
        await self.runtime._reconcile_paused();self.publisher.reconcile.assert_not_awaited()
        self.runtime.pause('发布结果待核实，禁止重发')
        await self.runtime._reconcile_paused()
        await self.runtime._reconcile_paused()
        self.assertEqual(self.publisher.reconcile.await_count,1)
        for _ in range(4):
            self.now+=timedelta(minutes=11);await self.runtime._reconcile_paused()
        self.assertEqual(self.publisher.reconcile.await_count,3)
        self.assertTrue(self.runtime._paused());self.assertEqual(self.publisher.calls,0)


    async def test_preparation_round_has_total_deadline_without_pausing_publication(self):
        from unittest.mock import patch
        original=asyncio.wait_for
        cancelled=asyncio.Event()
        async def hang():
            try:await asyncio.Event().wait()
            finally:cancelled.set()
        self.runtime.prepare=hang
        async def bounded(coro,timeout):
            self.assertEqual(timeout,300)
            return await original(coro,.01)
        with patch('workbench.micro_runtime.asyncio.wait_for',new=bounded):
            await self.runtime._prepare_round()
        self.assertTrue(cancelled.is_set())
        self.assertFalse(self.runtime._paused())
        self.assertIn('5分钟总时限',self.store.setting('publishing_logs')[-1]['message'])

    async def test_temporary_saved_draft_dedup_failure_can_recover_next_round(self):
        from unittest.mock import AsyncMock
        from workbench.ai_tools import PROFILE
        self.plan();self.now-=timedelta(minutes=5);await self.runtime.verify();self.runtime.ai_tools=True
        source={'url':'https://www.workbuddy.cn/docs/dedup-retry','kind':'official','body':'具体功能正文',
                'author':'WorkBuddy 官方文档','verified_at':self.now.isoformat()}
        draft=self.store.save_generated('dedup-retry','dedup-retry',1,{'title':'功能','body':'正文','verify':[],
            'sources':[source],'source_snapshots':[source],'options':{'profile':PROFILE,'topic':'AI'}})
        self.browser.official_detail=AsyncMock(return_value=source)
        self.runtime.content.check=AsyncMock(return_value={'passed':True,'issues':[]})
        self.guard.inspect=AsyncMock(side_effect=[{'decision':'review','retryable':True,'reasons':['查重服务暂不可用']},
            {'decision':'clear'}])
        await self.runtime._recover_review_drafts()
        self.assertNotIn(draft['id'],self.store.setting('micro_review_policy_audits',{}))
        self.assertEqual(self.runtime.queue.items(),[])
        await self.runtime._recover_review_drafts()
        self.assertEqual(len(self.runtime.queue.items()),1)

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

    async def test_recovery_never_compares_content_with_other_queued_tasks(self):
        from unittest.mock import AsyncMock
        from workbench.ai_tools import PROFILE
        self.plan();self.now-=timedelta(minutes=5);await self.runtime.verify();self.runtime.ai_tools=True
        plan=self.runtime.plans.load(self.now.date())
        first=next(s for s in plan['slots'] if s.get('content_type')=='tool-guide')
        self.now=datetime.fromisoformat(first['scheduled_at'])-timedelta(minutes=5)
        for index in range(2):
            plan['slots'].append({'id':f"{plan['date']}:recovery-window:{index+1}",
                'scheduled_at':(self.now+timedelta(minutes=30*(index+1))).isoformat(),
                'status':'pending','content_type':'tool-guide'})
        with self.store.connection() as db:
            db.execute('UPDATE micro_daily_plans SET payload=? WHERE day=?',
                       (self.store.dump(plan),plan['date']))
        for i in range(2):
            source={'url':f'https://www.workbuddy.cn/docs/feature-{i}','kind':'official',
                'body':'具有产品特点的官方具体功能正文','author':'WorkBuddy 官方文档','verified_at':self.now.isoformat()}
            self.store.save_generated(str(i),str(i),1,{'title':f'功能{i}','body':f'正文{i}','verify':[],
                'sources':[source],'source_snapshots':[source],'options':{'profile':PROFILE,'topic':'AI'}})
        self.browser.official_detail=AsyncMock(side_effect=lambda url,*args:{'url':url,'kind':'official','body':'实际来源正文'})
        self.runtime.content.check=AsyncMock(return_value={'passed':True,'issues':[]})
        self.runtime.llm.judge_duplicate=AsyncMock(return_value={'decision':'duplicate'})
        await self.runtime._recover_review_drafts()
        self.assertEqual(len(self.runtime.queue.items()),2)
        self.runtime.llm.judge_duplicate.assert_not_awaited()

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
        # The publisher's timeout remains 20ms. The outer test watchdog also
        # includes synchronous SQLite commits, which can exceed 500ms on disk.
        await asyncio.wait_for(self.runtime.tick(),5)
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
        async def discover_with_intents(*args):
            with self.store.connection() as db:
                self.assertGreater(db.execute('SELECT COUNT(*) FROM editorial_intents').fetchone()[0],0)
            return [('AI','official',source)]
        with patch('workbench.micro_runtime.OfficialResearch.discover',new=AsyncMock(side_effect=discover_with_intents)), patch('workbench.micro_runtime.TechNewsResearch.discover',new=AsyncMock(return_value=[])):
            await self.runtime.prepare()
        rows = self.runtime.queue.items()
        self.assertEqual(len(rows),1)
        plan = self.runtime.plans.load(self.now.date())
        slot = next(s for s in plan['slots'] if s['id']==rows[0]['slot_id'])
        self.assertEqual(slot.get('content_type'),'tool-guide')
        self.assertEqual(plan['slots'][0]['status'],'pending')
    async def test_catchup_and_title_revision_inherit_original_context_after_rollback(self):
        from tests.test_editorial_policy import report
        self.plan()
        p=self.runtime.editorial_policy
        p.propose(report(self.now.date().isoformat()),{'headline_angle':'reader_impact'},0)
        plan=self.runtime.plans.loa…9337 tokens truncated…       row,draft=self.enqueue(); self.now-=timedelta(minutes=5); await self.runtime.verify()
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

    async def test_prepare_does_not_exclude_historical_sources(self):
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
        self.assertEqual(len(self.runtime.queue.items()),2)
        self.assertNotIn(old,calls[0][0]);self.assertTrue(all(since==self.now-timedelta(hours=24) for _,since in calls))
        self.assertIn(old,{r['sources'][0]['url'] for r in self.runtime.queue.items()})

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
