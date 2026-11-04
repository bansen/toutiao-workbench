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
    return {'slot_id':str(number)+'-'+hashlib.sha256(body.encode()).hexdigest(),'title':'智能头盔','body':body,'verify':[],'sources':[{'url':f'https://www.toutiao.com/article/{number}/'}]}


class PublicationGuardTests(unittest.IsolatedAsyncioTestCase):

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


    async def test_end_of_window_after_preflight_stops_before_submit(self):
        candidate=draft()
        async def preflight(item):
            self.guard.clock=lambda:datetime(2026,10,30,23,tzinfo=ZoneInfo('Asia/Shanghai'))
            return {'account_id':'account','body_sha256':hashlib.sha256(item['body'].encode()).hexdigest(),
                    'exclusive':{'checked':False,'classes':'byte-checkbox'},
                    'ai':{'checked':True,'classes':'byte-checkbox byte-checkbox-checked'}}
        async def submit(item): self.fail('23点后不得提交')
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

    def test_extended_window_boundaries(self):
        for stamp,allowed in [('06:59:59',False),('07:00:00',True),('22:59:59',True),('23:00:00',False)]:
            self.guard.clock=lambda stamp=stamp:datetime.fromisoformat('2026-10-30T'+stamp+'+08:00')
            if allowed:self.guard.check_time()
            else:
                with self.assertRaises(NeedsAttention):self.guard.check_time()

    async def test_angle_placeholder_is_blocked_before_reservation_or_browser_access(self):
        candidate=draft('鉴权示例：Bearer <token>。')
        async def callback(item):self.fail('不安全尖括号不能进入浏览器提交')
        with self.assertRaises(NeedsAttention):
            await self.guard.send(candidate,'account',hashlib.sha256(candidate['body'].encode()).hexdigest(),callback,callback)
        self.assertEqual(self.ledger.records('account'),[])

    async def test_new_tasks_allow_repeated_content_without_any_model_call(self):
        from unittest.mock import AsyncMock
        self.record()
        candidate={**draft(),'slot_id':'a-new-independent-task'}
        self.guard.judge=AsyncMock(side_effect=AssertionError('不允许调用内容去重模型'))
        result=await self.guard.inspect(candidate,'account')
        self.assertEqual(result['decision'],'clear');self.guard.judge.assert_not_awaited()
        self.assertTrue(self.guard.review_current({**candidate,'publication_approval':result['approval']},'account','unrelated-history-version'))
