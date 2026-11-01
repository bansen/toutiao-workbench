import asyncio
import json
import tempfile
import unittest
from datetime import datetime,timedelta
from pathlib import Path
from unittest.mock import AsyncMock,Mock,patch
from zoneinfo import ZoneInfo
from workbench.store import Store
from workbench.review_store import ReviewStore
from workbench.editorial_policy import EditorialPolicy
from workbench.review_service import ReviewService
from tests.test_review_metrics import row

ZONE=ZoneInfo('Asia/Shanghai')

class ReviewServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(Path(self.tmp.name)/'state.db')
        self.reviews=ReviewStore(self.store,Path(self.tmp.name)/'reviews')
        self.policy=EditorialPolicy(self.store)
        self.now=datetime(2026,11,1,4,59,tzinfo=ZONE)
        self.account='private-account'
        self.collector=Mock();self.collector.collect=AsyncMock(return_value={'rows':[row(verification={'list_drawer_match':True,'time_match':True,'public_id_verified':True})], 'complete':True,'capabilities':{'reads':True},'warnings':[], 'account_checked':True,'pagination':{'pages':1,'complete':True},'verified_samples':1})
        self.service=ReviewService(self.reviews,self.policy,self.collector,lambda:self.account,clock=lambda:self.now)

    async def ready(self,validation=False):
        self.now=datetime(2026,11,1,5,tzinfo=ZONE)
        record=await self.service.start_run('2026-10-31',validation)
        await self.service.wait(record['id'])
        return self.service.status(record['id'])

    async def test_background_claim_deduplicates_and_anonymous_never_contains_source_text(self):
        ready=await self.ready()
        again=await self.service.start_run('2026-10-31')
        self.assertEqual(ready['id'],again['id']);self.collector.collect.assert_awaited_once()
        public=json.dumps(self.service.anonymous(ready['id']),ensure_ascii=False)
        for value in ('secret-post','秘密标题','秘密正文','private.invalid','private-account'):
            self.assertNotIn(value,public)
        self.account='other'
        with self.assertRaises(KeyError):self.service.anonymous(ready['id'])

    async def test_validation_finishes_without_mutating_policy_and_unlocks_configuration(self):
        with self.assertRaises(ValueError):self.service.configure(True)
        ready=await self.ready(True)
        result=self.service.finish(ready['id'],['验收通过'],[],{'target_length':450},0)
        self.assertEqual(result['state'],'completed');self.assertEqual(self.policy.status()['version'],0)
        self.assertTrue(self.service.configure(True)['enabled'])

    async def test_finish_uses_only_stored_report_and_is_idempotent(self):
        ready=await self.ready()
        with patch.object(self.policy,'evaluate',wraps=self.policy.evaluate) as evaluate:
            result=self.service.finish(ready['id'],['样本不足'],[])
            self.assertEqual(result['state'],'completed')
            self.assertEqual(evaluate.call_args.args[0],self.reviews.analysis(ready['id']))
            again=self.service.finish(ready['id'],['不同内容'],[])
            self.assertEqual(again,result);evaluate.assert_called_once()

    async def test_application_crash_is_never_reapplied(self):
        ready=await self.ready()
        with patch.object(self.reviews,'finish',side_effect=RuntimeError('crash private text')):
            result=self.service.finish(ready['id'],[],[])
        self.assertEqual(result['state'],'attention')
        with patch.object(self.policy,'evaluate') as evaluate:
            self.service.finish(ready['id'],[],[])
            self.reviews.recover()
            reopened=ReviewService(self.reviews,self.policy,self.collector,lambda:self.account,clock=lambda:self.now)
            reopened.finish(ready['id'],[],[])
            evaluate.assert_not_called()

    async def test_due_starts_at_next_five_only_latest_day_and_repeated_hourly_is_noop(self):
        self.assertEqual(self.service.settings()['first_due'],'2026-11-01T05:00:00+08:00')
        self.assertFalse(self.service.check_due()['due'])
        ready=await self.ready(True);self.service.finish(ready['id'],[],[]);self.service.configure(True)
        self.now=datetime(2026,11,1,4,59,tzinfo=ZONE)
        self.assertFalse(self.service.check_due()['due'])
        self.now=datetime(2026,11,3,6,tzinfo=ZONE)
        self.collector.collect.return_value={'rows':[row(published_at='2026-11-02T18:00:00+08:00',observed_at='2026-11-03T05:00:00+08:00',verification={'list_drawer_match':True,'time_match':True,'public_id_verified':True})],'complete':True,'capabilities':{},'warnings':[], 'account_checked':True,'pagination':{'pages':1,'complete':True},'verified_samples':1}
        self.assertEqual(self.service.check_due()['date'],'2026-11-02')
        result=await self.service.run_due();await self.service.wait(result['id'])
        self.service.finish(result['id'],[],[])
        self.now+=timedelta(hours=1)
        self.assertFalse(self.service.check_due()['due'])
        self.assertEqual((await self.service.run_due())['state'],'noop')
        self.assertEqual(self.collector.collect.call_args.args[0],'2026-11-02')
        with self.assertRaises(ValueError):await self.service.start_run('2026-11-01')

    async def test_failures_are_redacted_retryable_but_attention_is_not_retried(self):
        self.now=datetime(2026,11,1,5,tzinfo=ZONE)
        self.collector.collect.side_effect=RuntimeError('secret-post private.invalid')
        record=await self.service.start_run('2026-10-31');await self.service.wait(record['id'])
        self.assertEqual(self.service.status(record['id'])['state'],'failed')
        self.assertNotIn('private.invalid',json.dumps(self.service.status(record['id'])))
        self.collector.collect.side_effect=None
        again=await self.service.start_run('2026-10-31');await self.service.wait(again['id'])
        self.assertEqual(again['id'],record['id'])

    async def test_cas_and_untrusted_text_rejected_before_application_claim(self):
        ready=await self.ready()
        for args in ((['x'*501],[],None,0),([],[],{'prompt':'evil'},0),([],[],{'target_length':450},9)):
            with self.assertRaises(ValueError):self.service.finish(ready['id'],*args)
            self.assertEqual(self.service.status(ready['id'])['state'],'ready')

    async def test_validation_rejects_empty_or_unverified_collector_and_saves_local_capabilities(self):
        from copy import deepcopy
        baseline=deepcopy(self.collector.collect.return_value)
        for field,value in (('rows',[]),('account_checked',None),('verified_samples',0),('pagination',{'complete':False})):
            self.now=datetime(2026,11,1,5,tzinfo=ZONE)
            self.account='test-account-'+field
            self.collector.collect.return_value=deepcopy(baseline)
            self.collector.collect.return_value[field]=value
            record=await self.service.start_run('2026-10-31',True)
            await self.service.wait(record['id']);saved=self.service.status(record['id'])
            self.assertEqual(saved['state'],'attention')
            self.assertFalse(saved['capability_validation']['passed'])
            with self.assertRaises(ValueError):self.service.configure(True)
            self.assertTrue((Path(self.tmp.name)/'reviews/reports'/(record['id']+'-capabilities.json')).exists())

    async def test_needs_attention_is_not_automatically_recollected(self):
        from workbench.browser import NeedsAttention
        self.now=datetime(2026,11,1,5,tzinfo=ZONE)
        self.collector.collect.side_effect=NeedsAttention('private.invalid/captcha')
        record=await self.service.start_run('2026-10-31',True);await self.service.wait(record['id'])
        self.assertEqual(self.service.status(record['id'])['state'],'attention')
        await self.service.start_run('2026-10-31',True)
        self.collector.collect.assert_awaited_once()

    async def test_account_switch_during_collection_does_not_archive_new_account_data(self):
        self.now=datetime(2026,11,1,5,tzinfo=ZONE);account=self.account
        result=self.collector.collect.return_value
        async def collect(day):
            self.account='different-account'
            return result
        self.collector.collect.side_effect=collect
        record=await self.service.start_run('2026-10-31',True)
        await asyncio.gather(*list(self.service._tasks.values()))
        self.assertEqual(self.reviews.get(account,'2026-10-31',True)['state'],'attention')
        self.assertEqual(self.reviews.raw_rows(record['id']),[])

    async def test_stale_ready_report_cannot_apply_policy_on_another_due_day(self):
        record=await self.ready();self.now+=timedelta(days=2)
        with self.assertRaises(ValueError):self.service.finish(record['id'],[],[])
        self.assertEqual(self.policy.status()['version'],0)

    async def test_disabled_service_prunes_immediately_hourly_and_close_stops_task(self):
        self.assertFalse(self.service.settings()['enabled'])
        old=self.now-timedelta(days=61)
        expired=self.reviews.claim(self.account,'2026-09-01',now=old)
        self.reviews.save_collection(expired['id'],[],True,{},[],now=old)
        raw=self.reviews.directory/'raw'/(expired['id']+'.json')
        self.assertTrue(raw.exists())
        first_wait=asyncio.Event();second_wait=asyncio.Event();advance=asyncio.Event();stop=asyncio.Event();sleeps=0
        async def hourly(seconds):
            nonlocal sleeps
            self.assertEqual(seconds,3600);sleeps+=1
            if sleeps==1:
                first_wait.set();await advance.wait()
            else:
                second_wait.set();await stop.wait()
        with patch.object(self.reviews,'prune',wraps=self.reviews.prune) as review_prune, \
             patch.object(self.policy,'prune') as policy_prune, \
             patch('workbench.review_service.asyncio.sleep',side_effect=hourly):
            await self.service.start()
            review_prune.assert_called_once();policy_prune.assert_called_once()
            self.assertFalse(raw.exists())
            self.assertIsNone(self.reviews.get(self.account,'2026-09-01'))
            task=self.service._retention_task
            await self.service.start()
            self.assertIs(self.service._retention_task,task)
            await first_wait.wait();advance.set();await second_wait.wait()
            self.assertEqual(review_prune.call_count,2);self.assertEqual(policy_prune.call_count,2)
            await self.service.close()
            self.assertTrue(task.done())
            self.assertIsNone(self.service._retention_task)

    async def test_collection_warning_blocks_eleven_valid_rows_despite_ten_verified(self):
        self.now=datetime(2026,11,1,5,tzinfo=ZONE)
        self.collector.collect.return_value={
            'rows':[row(platform_id='valid-'+str(n),verification={'list_drawer_match':True,'time_match':True,'public_id_verified':True}) for n in range(11)],
            'complete':True,'capabilities':{'impressions':True,'reads':True},
            'warnings':['部分作品ID缺失，未计入11条有效行'],'account_checked':True,
            'pagination':{'pages':2,'complete':True},'verified_samples':10}
        record=await self.service.start_run('2026-10-31');await self.service.wait(record['id'])
        saved=self.service.status(record['id'])
        self.assertEqual(saved['state'],'attention')
        self.assertIn('collection_warnings',saved['capability_validation']['failure_codes'])
        with patch.object(self.policy,'propose') as propose,patch.object(self.policy,'evaluate') as evaluate:
            with self.assertRaises(ValueError):self.service.finish(record['id'],[],[],{'target_length':450},0)
            propose.assert_not_called();evaluate.assert_not_called()

    async def test_delayed_first_enable_anchors_next_five_and_restart_preserves_activation(self):
        ready=await self.ready(True);self.service.finish(ready['id'],[],[])
        self.now=datetime(2026,11,3,18,30,tzinfo=ZONE)
        activated=self.service.configure(True)
        self.assertEqual(activated['activated_at'],'2026-11-03T18:30:00+08:00')
        self.assertEqual(activated['first_due'],'2026-11-04T05:00:00+08:00')
        self.assertFalse(self.service.check_due()['due'])
        reopened=ReviewService(self.reviews,self.policy,self.collector,lambda:self.account,clock=lambda:self.now)
        self.assertEqual(reopened.settings()['first_due'],activated['first_due'])
        self.assertFalse(reopened.check_due()['due'])
        self.now=datetime(2026,11,4,5,tzinfo=ZONE)
        self.assertTrue(reopened.check_due()['due'])
        self.assertEqual(reopened.check_due()['date'],'2026-11-03')
        reopened.configure(False)
        self.now=datetime(2026,11,6,10,tzinfo=ZONE)
        resumed=reopened.configure(True)
        self.assertEqual(resumed['activated_at'],activated['activated_at'])
        self.assertEqual(resumed['first_due'],activated['first_due'])
        self.assertEqual(reopened.check_due()['date'],'2026-11-05')
        self.assertTrue(reopened.check_due()['due'])
