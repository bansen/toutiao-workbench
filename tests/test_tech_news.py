import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

from workbench.store import Store
from workbench.tech_news import TechNewsResearch, mix_candidates


class TechNewsTests(unittest.IsolatedAsyncioTestCase):
    async def test_social_life_search_preserves_category_and_topic(self):
        from workbench.ai_tools import NEWS_KEYWORDS,NEWS_CATEGORIES
        now=datetime.now(timezone.utc)
        keywords=[k for k in NEWS_KEYWORDS if NEWS_CATEGORIES[k]=='social']
        self.assertTrue(keywords)
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'db');store.set_setting('tech_news_cursor',NEWS_KEYWORDS.index(keywords[0]))
            search=AsyncMock(return_value=[{'url':'https://www.toutiao.com/article/7701979050666902001/','published_at':now.isoformat()}])
            result=await TechNewsResearch(store,search).discover(set(),now,limit=1)
            self.assertEqual(result[0][0],'社会生活')
            self.assertEqual(result[0][2]['news_category'],'social')
    async def test_search_is_bounded_excludes_seen_and_preserves_unknown_times_for_detail(self):
        now=datetime.now(timezone.utc)
        seen='https://www.toutiao.com/article/7701979050666902000/'
        fresh='https://www.toutiao.com/article/7701979050666902001/'
        unknown='https://www.toutiao.com/article/7701979050666902002/'
        calls=[]
        async def search(word,kind,limit,exclude,since):
            calls.append((kind,limit,list(exclude),since))
            if len(calls)==1:
                return [{'url':seen,'published_at':now.isoformat()},{'url':fresh,'published_at':now.isoformat()}]
            return [{'url':fresh,'published_at':now.isoformat()},{'url':unknown,'published_at':None}]
        with tempfile.TemporaryDirectory() as tmp:
            result=await TechNewsResearch(Store(Path(tmp)/'db'),search).discover({seen},now,limit=2)
        self.assertEqual([v[2]['url'] for v in result],[fresh,unknown])
        self.assertEqual([v[0] for v in result], ['热点','商业'])
        self.assertEqual([v[2]['news_category'] for v in result], ['hot','business'])
        self.assertTrue(all(v[1]=='article' for v in result))
        self.assertLessEqual(len(calls),5)
        self.assertTrue(all(v[1]<=2 and v[3]==now-timedelta(hours=24) for v in calls))
        self.assertIn(fresh,calls[1][2])

    async def test_past_and_future_news_cards_are_rejected(self):
        now=datetime.now(timezone.utc)
        search=AsyncMock(return_value=[{'url':'https://www.toutiao.com/article/7701979050666902001/','published_at':(now-timedelta(days=2)).isoformat()},
                                      {'url':'https://www.toutiao.com/article/7701979050666902002/','published_at':(now+timedelta(hours=1)).isoformat()}])
        with tempfile.TemporaryDirectory() as tmp:
            result=await TechNewsResearch(Store(Path(tmp)/'db'),search).discover(set(),now)
        self.assertEqual(result,[])
        self.assertLessEqual(search.await_count,5)

    async def test_mix_preserves_guides_and_interleaves_news_without_dropping_sources(self):
        self.assertEqual(mix_candidates(['g1','g2','g3'],['n1','n2']),['n1','g1','n2','g2','g3'])
        self.assertEqual(mix_candidates([],['n1']),['n1'])
        self.assertEqual(mix_candidates(['g1'],[]),['g1'])
