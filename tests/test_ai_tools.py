import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock

from workbench.ai_tools import AiToolContent, canonical_official_url, PROFILE, OfficialResearch, activate_profile, format_issues
from workbench.micro_content import MicroContent
from workbench.publication_ledger import _canonical_source
from workbench.store import Store
from workbench.micro_runtime import MicroRuntime
from workbench.browser import Browser
from workbench.post_layout import format_post
from pathlib import Path
import tempfile

BODY='WorkBuddy 的技能入口这样用\n\n1. 技能市场：在专家·技能·连接器中选择技能页签，查看推荐技能。\n2. 上传技能：从添加技能进入上传技能，导入本地技能包。\n3. 技能开关：关闭后新会话不再调用，已加载的会话可能仍可使用。\n\n#WorkBuddy #AI办公 #Skills'
SOURCE={'url':'https://www.workbuddy.cn/docs/workbuddy/From-Beginner-to-Expert-Guide/Function-Description/Skills-Market','title':'技能','author':'WorkBuddy 官方文档','body':'技能市场。添加技能支持上传技能，开关关闭后新会话不再调用。','kind':'official','verified_at':datetime.now(timezone.utc).isoformat()}

BODY=format_post(BODY)

class AiToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_business_news_keeps_its_category_and_supported_source(self):
        now=datetime.now(timezone.utc).isoformat()
        source={'url':'https://www.toutiao.com/article/7701979050666902079/','title':'公司合作公告',
                'author':'经济日报','body':'甲公司与乙公司宣布成立联合研发中心，计划开发仓储设备。',
                'kind':'article','published_at':now,'collected_at':now,'news_category':'business'}
        body='两家公司把合作落在联合研发中心\n\n据经济日报报道，甲公司与乙公司宣布成立联合研发中心，计划开发仓储设备。\n\n#商业新闻 #企业合作 #产业动态'
        service=AiToolContent(None);service.request=AsyncMock(return_value={'title':body.splitlines()[0],'body':body,'verify':[]})
        draft=await service.generate([source],'商业')
        self.assertEqual(draft['options']['topic'],'商业')
        self.assertEqual(draft['options']['news_category'],'business')
        self.assertEqual(draft['sources'][0]['news_category'],'business')
        service.request=AsyncMock(return_value={'passed':True,'issues':[]})
        self.assertTrue((await service.check(draft,[source],'商业'))['passed'])

    async def test_dense_model_output_is_formatted_before_validation(self):
        dense=BODY.replace('\n\n','\n')
        service=AiToolContent(None);service.request=AsyncMock(return_value={'title':BODY.splitlines()[0],'body':dense,'verify':[]})
        draft=await service.generate([SOURCE],'AI')
        self.assertEqual(draft['body'],BODY)
        self.assertEqual(draft['verify'],[])
    async def test_tech_news_uses_event_paragraphs_without_forced_function_list(self):
        now=datetime.now(timezone.utc).isoformat()
        source={'url':'https://www.toutiao.com/article/7701979050666902079/','title':'机器人新品发布',
                'author':'科技日报','body':'甲公司今天发布一款仓储机器人，支持在园区搬运物料，尚未公布售价。',
                'kind':'article','published_at':now,'collected_at':now}
        body='仓储机器人又有新进展\n\n据科技日报报道，甲公司发布了一款仓储机器人，面向园区物料搬运。售价尚未公布。\n\n#机器人 #科技新闻 #智能设备'
        service=AiToolContent(None);service.request=AsyncMock(return_value={'title':body.splitlines()[0],'body':body,'verify':[]})
        draft=await service.generate([source],'科技',recent_titles=['旧标题'])
        self.assertEqual(draft['verify'],[])
        self.assertEqual(draft['options']['content_type'],'tech-news')
        self.assertEqual(draft['options']['topic'],'科技')
        self.assertEqual(draft['sources'][0]['published_at'],now)
        service.request=AsyncMock(return_value={'passed':True,'issues':[]})
        self.assertTrue((await service.check(draft,[source],'科技'))['passed'])
        self.assertNotIn('当前官方正文支撑',str(service.request.call_args))
        repaired=AiToolContent(None);repaired.request=AsyncMock(return_value={'title':draft['title'],'body':body,'verify':[]})
        revised=await repaired.repair(draft,[source],'科技',['核实发布日期'])
        self.assertEqual(revised['options']['content_type'],'tech-news')
        self.assertEqual(revised['verify'],[])

    async def test_tech_news_excludes_unknown_dates_old_news_and_untrusted_authors(self):
        now=datetime.now(timezone.utc).isoformat()
        body='科技事件进展\n\n据科技日报报道，甲公司发布园区机器人。\n\n#机器人 #科技新闻 #智能设备'
        draft={'title':body.splitlines()[0],'body':body,'verify':[]}
        source={'url':'https://www.toutiao.com/article/7701979050666902079/','kind':'article','author':'科技日报',
                'body':'甲公司发布园区机器人。','published_at':now,'collected_at':now}
        service=AiToolContent(None);service.request=AsyncMock(return_value={'passed':True,'issues':[]})
        for bad in [{**source,'published_at':None},{**source,'published_at':'2025-01-01T00:00:00+00:00'},
                    {**source,'author':'某某营销号'},{**source,'collected_at':'2025-01-01T00:00:00+00:00'}]:
            self.assertFalse((await service.check(draft,[bad],'科技'))['passed'])
        service.request.assert_not_awaited()

    async def test_optional_operation_steps_do_not_inflate_title_feature_count(self):
        body=BODY.replace('这样用','3个功能').replace('\n\n#WorkBuddy','\n\n操作方法：\n1. 打开技能页签。\n2. 选择需要的技能。\n\n#WorkBuddy')
        body=format_post(body)
        self.assertEqual(format_issues({'title':body.splitlines()[0],'body':body}),[])
    async def test_profile_migration_cancels_old_pending_drafts_without_changing_schedule(self):
        from workbench.micro_queue import MicroQueue
        from workbench.micro_plan_store import MicroPlanStore
        from workbench.micro_schedule import TIMEZONE
        from datetime import timedelta
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'db');plans=MicroPlanStore(store);queue=MicroQueue(store)
            now=datetime(2030,1,1,8,tzinfo=TIMEZONE)
            plan=plans.get_or_create(now);slot=next(s for s in plan['slots'] if s['status']=='pending')
            draft=store.save_generated('task','source',1,{'title':'旧民生稿','body':'旧稿正文','sources':[SOURCE],'verify':[]})
            row=queue.add(slot,draft,'民生','event');plans.update_slot(slot['id'],{'status':'queued','queue_id':row['id']})
            activate_profile(store)
            self.assertEqual(queue.get(row['id'])['status'],'cancelled')
            updated=plans.load(now.date());same=next(s for s in updated['slots'] if s['id']==slot['id'])
            self.assertEqual(same['scheduled_at'],slot['scheduled_at'])
            self.assertEqual(same['status'],'skipped')
            self.assertEqual(store.setting('editorial_profile'),PROFILE)
            activate_profile(store)
            self.assertEqual(plans.load(now.date()),updated)

    async def test_official_discovery_is_bounded_and_moves_past_processed_documents(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'db')
            async def read(url,product):
                return {**SOURCE,'url':url,'body':'官方功能和限制说明'*30,'links':[{'url':'https://www.workbuddy.cn/docs/new-feature'}]}
            reader=AsyncMock(side_effect=read)
            result=await OfficialResearch(store,reader).discover({SOURCE['url']},limit=2)
            self.assertEqual(reader.await_count,2)
            self.assertIn('Automation-Guide',result[0][2]['url'])
            self.assertLessEqual(store.setting('ai_tools_last_research')['checked'],2)
            store.set_setting('ai_tools_cursor',0)
            reader.reset_mock()
            result=await OfficialResearch(store,reader).discover(set(),limit=1)
            self.assertEqual(len(result),1)
            self.assertEqual(reader.await_count,1)

    async def test_official_browser_method_rejects_foreign_url_before_starting_chrome(self):
        browser=Browser('/tmp/unused-official-test-profile')
        browser.start=AsyncMock()
        with self.assertRaises(ValueError):await browser.official_detail('https://example.com/docs','WorkBuddy')
        browser.start.assert_not_awaited()

    async def test_active_profile_refreshes_official_docs_instead_of_toutiao_detail(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'db');store.set_setting('editorial_profile',PROFILE)
            browser=Browser(Path(tmp)/'browser')
            browser.official_detail=AsyncMock(return_value=SOURCE)
            browser.detail=AsyncMock()
            class Publisher:pass
            runtime=MicroRuntime(store,browser,None,None,Publisher())
            self.assertIsInstance(runtime.content,AiToolContent)
            result=await runtime._refresh({'sources':[SOURCE]})
            self.assertEqual(result,[SOURCE])
            browser.official_detail.assert_awaited_once()
            browser.detail.assert_not_awaited()

    async def test_composition_contains_title_and_tags_but_keeps_sources_separate(self):
        service=AiToolContent(None)
        service.request=AsyncMock(return_value={'title':BODY.splitlines()[0],'body':BODY,'verify':[]})
        draft=await service.generate([SOURCE],'AI',recent_titles=['旧标题'])
        self.assertEqual(draft['body'],BODY)
        self.assertNotIn('来源：',draft['body'])
        self.assertEqual(draft['sources'][0]['url'],SOURCE['url'])
        self.assertEqual(draft['options']['profile'],PROFILE)
        self.assertIn('旧标题',str(service.request.call_args))

    async def test_official_url_boundary_rejects_untrusted_hosts_and_credentials(self):
        self.assertEqual(_canonical_source(SOURCE['url']),SOURCE['url'])
        for url in ['http://www.workbuddy.cn/docs','https://www.workbuddy.cn.evil.test/docs','https://user@www.workbuddy.cn/docs','https://127.0.0.1/docs','https://example.com/docs']:
            with self.assertRaises(ValueError):canonical_official_url(url)

    async def test_format_and_stale_sources_fail_before_model(self):
        service=AiToolContent(None);service.request=AsyncMock(return_value={'passed':True,'issues':[]})
        for body in ['没有编号和标签',BODY+'\n来源：官方',BODY.replace('这样用','8个功能')]:
            result=await service.check({'body':body,'title':body.splitlines()[0],'verify':[]},[SOURCE],'AI')
            self.assertFalse(result['passed'])
        old={**SOURCE,'verified_at':'2025-01-01T00:00:00+00:00'}
        self.assertFalse((await service.check({'body':BODY,'title':BODY.splitlines()[0],'verify':[]},[old],'AI'))['passed'])
        service.request.assert_not_awaited()

    async def test_editorial_check_requires_product_specificity_and_current_evidence(self):
        service=AiToolContent(None);service.request=AsyncMock(return_value={'passed':True,'issues':[]})
        draft={'body':BODY,'title':BODY.splitlines()[0],'verify':[],'source_snapshots':[SOURCE]}
        self.assertTrue((await service.check(draft,[SOURCE],'AI'))['passed'])
        prompt=str(service.request.call_args)
        for word in ['产品名称','套餐','实测','标题','来源']:
            self.assertIn(word,prompt)

    async def test_quality_repair_rechecks_same_profile_and_keeps_title(self):
        service=AiToolContent(None);service.request=AsyncMock(return_value={'title':BODY.splitlines()[0],'body':BODY,'verify':[]})
        draft=await service.generate([SOURCE],'AI')
        repaired=await service.repair(draft,[SOURCE],'AI',['删除无证据的价格'])
        self.assertEqual(repaired['body'].splitlines()[0],repaired['title'])
        self.assertEqual(repaired['options']['profile'],PROFILE)
        self.assertEqual(repaired['editorial_review'],['删除无证据的价格'])
