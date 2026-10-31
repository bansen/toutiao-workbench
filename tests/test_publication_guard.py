import hashlib
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from workbench.store import Store
from workbench.publication_ledger import PublicationLedger
from workbench.publication_guard import PublicationGuard
from workbench.browser import NeedsAttention


def draft(body='武汉展会展示语音控制转向灯的智能骑行头盔。',number=7701979050666902079):
    return {'title':'智能头盔','body':body,'verify':[],'sources':[{'url':f'https://www.toutiao.com/article/{number}/'}]}


class PublicationGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_unchanged_history_uses_bound_dedup_approval_but_new_history_invalidates_it(self):
        from unittest.mock import AsyncMock
        self.record()
        self.guard.judge=AsyncMock(return_value={'decision':'distinct','matched_id':None,'reason':'不同事件'})
        candidate=draft('全新的工作流事件',7701979050666902081)
        result=await self.guard.inspect(candidate,'account')
        approved={**candidate,'publication_approval':result['approval']}
        self.assertEqual((await self.guard.inspect(approved,'account'))['decision'],'clear')
        self.assertEqual(self.guard.judge.await_count,1)
        self.ledger.record_verified(draft('第三个事件',7701979050666902082),'account',
            {'content_id':'1880433446879242','status':'published'})
        await self.guard.inspect(approved,'account')
        self.assertEqual(self.guard.judge.await_count,2)

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.ledger=PublicationLedger(Store(Path(self.tmp.name)/'test.db'))
        self.calls=[]
        async def judge(candidate,history):
            self.calls.append((candidate,history))
            return {'decision':'duplicate','matched_id':history[0]['id'],'reason':'同一展会演示'}
        self.guard=PublicationGuard(self.ledger,judge,clock=lambda:datetime(2026,10,30,10,tzinfo=ZoneInfo('Asia/Shanghai')))
    def tearDown(self): self.tmp.cleanup()
    def record(self,item=None):
        return self.ledger.record_verified(item or draft(),'account',{'content_id':'1880433446879241','url':'https://www.toutiao.com/w/1880433446879241/','status':'published'})
    async def test_identical_source_is_blocked_without_model(self):
        self.record()
        result=await self.guard.inspect(draft('完全换了说法，还是这篇素材。'),'account')
        self.assertEqual(result['decision'],'duplicate')
        self.assertEqual(self.calls,[])
    async def test_cross_source_paraphrase_same_event_is_blocked(self):
        self.record()
        result=await self.guard.inspect(draft('声控头戴装备在湖北某科技大会亮相。',7701979050666902080),'account')
        self.assertEqual(result['decision'],'duplicate')
        self.assertEqual(len(self.calls),1)
    async def test_uncertain_or_unavailable_model_never_clears(self):
        self.record()
        async def uncertain(candidate,history): return {'decision':'uncertain','reason':'无法确认是否有新进展'}
        self.guard.judge=uncertain
        self.assertEqual((await self.guard.inspect(draft('另一说法',7701979050666902080),'account'))['decision'],'review')
        async def failed(candidate,history): raise RuntimeError('private-error')
        self.guard.judge=failed
        result=await self.guard.inspect(draft('另一说法',7701979050666902080),'account')
        self.assertEqual(result['decision'],'review')
        self.assertTrue(result['retryable'])
        self.assertNotIn('private-error',str(result))
    async def test_all_history_is_checked_in_batches(self):
        for i in range(27):
            self.ledger.record_verified(draft('第'+str(i)+'个完全不同的独立事件记录。',7701979050666902079+i),'account',{'content_id':str(1880433446879241+i),'status':'published'})
        async def distinct(candidate,history):
            self.calls.append(history)
            return {'decision':'distinct','matched_id':None,'reason':'不同事件'}
        self.guard.judge=distinct
        result=await self.guard.inspect(draft('这次准备报道完全不同的事情。',7701979050666902110),'account')
        self.assertEqual(result['decision'],'clear')
        self.assertEqual(sum(map(len,self.calls)),27)
        self.assertGreater(len(self.calls),1)
    async def test_send_freezes_snapshot_and_uncertain_prevents_retry(self):
        candidate=draft()
        async def preflight(item):
            return {'account_id':'account','body_sha256':hashlib.sha256(item['body'].encode()).hexdigest(),
                    'exclusive':{'checked':False,'classes':'byte-checkbox'},'ai':{'checked':True,'classes':'byte-checkbox byte-checkbox-checked'}}
        async def send(item):
            self.calls.append(item)
            raise TimeoutError('结果未知')
        with self.assertRaises(TimeoutError):
            await self.guard.send(candidate,'account',hashlib.sha256(candidate['body'].encode()).hexdigest(),preflight,send)
        self.assertEqual(self.ledger.records('account')[0]['status'],'uncertain')
        with self.assertRaises(NeedsAttention):
            await self.guard.send(candidate,'account',hashlib.sha256(candidate['body'].encode()).hexdigest(),preflight,send)
        self.assertEqual(len(self.calls),1)
    async def test_exclusive_or_wrong_account_stops_before_submit(self):
        candidate=draft()
        async def wrong(item):
            return {'account_id':'another','body_sha256':hashlib.sha256(item['body'].encode()).hexdigest()}
        async def send(item): self.fail('不得提交')
        with self.assertRaises(NeedsAttention):
            await self.guard.send(candidate,'account',hashlib.sha256(candidate['body'].encode()).hexdigest(),wrong,send)
        self.assertEqual(self.ledger.records('account'),[])
    async def test_changed_confirmed_text_stops_before_reservation(self):
        async def callback(item): self.fail('不得执行')
        with self.assertRaises(NeedsAttention): await self.guard.send(draft(),'account','not-approved',callback,callback)
        self.assertEqual(self.ledger.records('account'),[])

    async def test_external_link_stops_before_browser_or_reservation_but_sources_stay_valid(self):
        candidate=draft('工具说明，请访问 dify.ai 查看入口。')
        async def callback(item):self.fail('正文含外链时不应打开编辑器或提交')
        result=await self.guard.inspect(candidate,'account')
        self.assertEqual(result['decision'],'review')
        with self.assertRaises(NeedsAttention):
            await self.guard.send(candidate,'account',hashlib.sha256(candidate['body'].encode()).hexdigest(),callback,callback)
        self.assertEqual(self.ledger.records('account'),[])
        self.assertEqual((await self.guard.inspect(draft('打开应用 → 技能页面，选择对应功能。'),'account'))['decision'],'clear')

    async def test_selected_first_release_never_submits(self):
        candidate=draft()
        async def preflight(item):
            return {'account_id':'account','body_sha256':hashlib.sha256(item['body'].encode()).hexdigest(),
                    'exclusive':{'checked':True,'classes':'byte-checkbox byte-checkbox-checked'},
                    'ai':{'checked':True,'classes':'byte-checkbox byte-checkbox-checked'}}
        async def submit(item): self.fail('首发选中时不得提交')
        with self.assertRaises(NeedsAttention):
            await self.guard.send(candidate,'account',hashlib.sha256(candidate['body'].encode()).hexdigest(),preflight,submit)
        self.assertEqual(self.ledger.records('account'),[])

    async def test_history_change_during_judgment_requires_recheck(self):
        self.record()
        async def raced(candidate,history):
            self.ledger.record_verified(draft('刚刚完成的另一事件记录',7701979050666902081),'account',{'status':'published','content_id':'1880433446879242'})
            return {'decision':'distinct'}
        self.guard.judge=raced
        result=await self.guard.inspect(draft('另一篇稿件',7701979050666902080),'account')
        self.assertEqual(result['decision'],'review')

    async def test_end_of_window_after_preflight_stops_before_submit(self):
        candidate=draft()
        async def preflight(item):
            self.guard.clock=lambda:datetime(2026,10,30,22,tzinfo=ZoneInfo('Asia/Shanghai'))
            return {'account_id':'account','body_sha256':hashlib.sha256(item['body'].encode()).hexdigest(),
                    'exclusive':{'checked':False,'classes':'byte-checkbox'},
                    'ai':{'checked':True,'classes':'byte-checkbox byte-checkbox-checked'}}
        async def submit(item): self.fail('22点后不得提交')
        with self.assertRaises(NeedsAttention):
            await self.guard.send(candidate,'account',hashlib.sha256(candidate['body'].encode()).hexdigest(),preflight,submit)
        self.assertEqual(self.ledger.records('account'),[])

    async def test_failed_history_import_blocks_send_boundary_too(self):
        self.guard.ready=lambda account:False
        candidate=draft()
        async def callback(item): self.fail('历史未核实不得执行浏览器操作')
        with self.assertRaises(NeedsAttention):
            await self.guard.send(candidate,'account',hashlib.sha256(candidate['body'].encode()).hexdigest(),callback,callback)
        self.assertEqual(self.ledger.records('account'),[])
