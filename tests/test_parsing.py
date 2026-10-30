import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from workbench.parsing import canonical_url, parse_count, parse_time, select_items


class ParsingTests(unittest.TestCase):
    def test_nested_search_redirect_and_tracking_deduplicate(self):
        url = 'https://sou.toutiao.com/search/jump?url=https%3A%2F%2Fwww.toutiao.com%2Fa7699775205312315954%2F%3Fsource%3Dsearch'
        self.assertEqual(canonical_url(url), 'https://www.toutiao.com/article/7699775205312315954/')
        self.assertEqual(canonical_url('https://www.toutiao.com/article/7699775205312315954/?wid=123#comment'), canonical_url(url))

    def test_only_toutiao_content_links_are_accepted(self):
        for url in ['http://localhost/article/123/', 'https://www.toutiao.com.evil.org/article/123/', 'file:///tmp/a', 'https://www.toutiao.com@evil.org/article/123/', 'https://www.toutiao.com/search/?keyword=x']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                canonical_url(url)

    def test_legacy_micro_link_maps_to_current_detail_route(self):
        self.assertEqual(canonical_url('https://www.toutiao.com/a1879948840281099/?source=search_tab'),'https://www.toutiao.com/w/1879948840281099/')

    def test_units_and_missing_counts(self):
        self.assertEqual(parse_count('1.2万'), 12000)
        self.assertEqual(parse_count('1,234'), 1234)
        self.assertEqual(parse_count('0'), 0)
        self.assertIsNone(parse_count('评论'))
        self.assertIsNone(parse_count('未知'))

    def test_relative_time_uses_hong_kong_day_boundary(self):
        now = datetime(2026, 10, 25, 0, 30, tzinfo=ZoneInfo('Asia/Hong_Kong'))
        self.assertEqual(parse_time('前天16:20', now), '2026-10-23T16:20:00+08:00')
        self.assertEqual(parse_time('50分钟前', now), (now-timedelta(minutes=50)).isoformat())
        self.assertIsNone(parse_time('时间未知', now))

    def test_filter_boundary_unknown_and_sort(self):
        items = [{'id':1,'reads':10000}, {'id':2,'reads':49999}, {'id':3,'reads':50000}, {'id':4,'reads':None}]
        rows, unknown = select_items(items, {'read_min':10000,'read_max':50000,'sort':'reads'})
        self.assertEqual([x['id'] for x in rows], [2,1])
        self.assertEqual(unknown, 1)
        rows, _ = select_items(items, {'sort':'reads'})
        self.assertEqual(rows[-1]['id'],4)

    def test_creator_month_day_timestamps_count_manual_posts(self):
        now=datetime(2026,10,30,17,0,tzinfo=ZoneInfo('Asia/Hong_Kong'))
        self.assertEqual(parse_time('10-30 16:56',now),'2026-10-30T16:56:00+08:00')
        self.assertEqual(parse_time('10-29 12:54',now),'2026-10-29T12:54:00+08:00')
        january=datetime(2026,1,1,12,tzinfo=ZoneInfo('Asia/Hong_Kong'))
        self.assertEqual(parse_time('12-31 23:30',january),'2025-12-31T23:30:00+08:00')
        self.assertIsNone(parse_time('13-40 25:61',now))


if __name__ == '__main__':
    unittest.main()
