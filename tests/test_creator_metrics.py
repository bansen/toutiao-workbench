import unittest
from datetime import datetime
from workbench.creator_analytics import normalize_row,detail_metrics
from workbench.micro_schedule import TIMEZONE

class MetricsTests(unittest.TestCase):
    def test_known_zero_and_missing(self):
        row=normalize_row({'body':'标题\n正文','time':'10-31 20:30','status':'已发布','stats':['展现 1.2万','阅读 0','点赞 --','评论 2']},datetime(2026,11,1,5,tzinfo=TIMEZONE))
        self.assertEqual(row['metrics']['impressions'],12000)
        self.assertEqual(row['metrics']['reads'],0)
        self.assertIsNone(row['metrics']['likes'])
        self.assertEqual(row['published_at'],'2026-10-31T20:30:00+08:00')
        self.assertEqual(row['scope'],'unverified')
    def test_drawer_exact_labels_not_fan_subcounts(self):
        m=detail_metrics([{'label':'展现量','value':'1,234'},{'label':'粉丝展现量','value':'20'},{'label':'分享','value':'0'},{'label':'转发','value':'7'},{'label':'涨粉','value':'待更新'},{'label':'创作收益','value':'0.12'}])
        self.assertEqual(m['impressions'],1234)
        self.assertEqual(m['shares'],0)
        self.assertIsNone(m['follows'])
        self.assertEqual(m['revenue'],.12)

class MetadataTests(unittest.TestCase):
    def test_experiment_binding_uses_account_scoped_receipt_not_rendered_body(self):
        import tempfile
        from pathlib import Path
        from workbench.store import Store
        from workbench.creator_analytics import CreatorAnalytics
        from workbench.publication_ledger import PublicationLedger
        with tempfile.TemporaryDirectory() as directory:
            store=Store(Path(directory)/'data.db')
            draft=store.save_generated('t','s',1,{'title':'标题','body':'标题\n\n正文','sources':[{'url':'https://www.toutiao.com/w/1880000000000000/'}],
                'editorial_context':{'experiment_id':'experiment','arm':'trial','content_kind':'tool-guide','intent_id':'original'}})
            ledger=PublicationLedger(store)
            ledger.record_verified({**draft,'draft_id':draft['id']},'account',{'status':'published','content_id':'1880000000000001'})
            collector=CreatorAnalytics(None,lambda:'account',directory,lambda:False,store=store)
            row={'platform_id':'1880000000000001','body':'标题正文','content_kind':'unknown','editorial_context':None}
            collector.attach_metadata(row)
            self.assertEqual(row['editorial_context'],draft['editorial_context'])
            self.assertEqual(row['content_kind'],'tool-guide')
            other=CreatorAnalytics(None,lambda:'other-account',directory,lambda:False,store=store)
            untouched={'platform_id':'1880000000000001','body':'标题正文','content_kind':'unknown','editorial_context':None}
            other.attach_metadata(untouched)
            self.assertIsNone(untouched['editorial_context'])
            manual={'platform_id':'1880000000000009','body':draft['body'],'content_kind':'unknown','editorial_context':None}
            collector.attach_metadata(manual)
            self.assertIsNone(manual['editorial_context'])

class CoverageTests(unittest.TestCase):
    def test_only_verified_chronological_prefix_can_end_at_retention_boundary(self):
        from workbench.creator_analytics import date_coverage
        now=datetime(2026,11,2,5,tzinfo=TIMEZONE)
        start=datetime(2026,10,30,5,tzinfo=TIMEZONE)
        ordered,previous,done=date_coverage(['11-01 21:00','10-31 09:00'],None,start,True,now)
        self.assertTrue(ordered);self.assertFalse(done)
        ordered,previous,done=date_coverage(['10-30 04:00','10-29 19:00'],previous,start,ordered,now)
        self.assertTrue(done)
        for times,prior in [(['未知'],None),([],None),(['10-29 09:00','10-30 04:00'],None),(['10-30 04:00'],datetime(2026,10,29,5,tzinfo=TIMEZONE))]:
            self.assertFalse(date_coverage(times,prior,start,True,now)[2])
