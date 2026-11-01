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
