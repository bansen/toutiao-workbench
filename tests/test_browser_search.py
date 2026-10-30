import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock,Mock

from workbench.browser import Browser


def card(number):
    return {'text':'候选正文','links':[{'href':f'https://www.toutiao.com/article/{number}/','text':'标题'}],'source':'来源\n今天 10:00'}


class BrowserSearchTests(unittest.IsolatedAsyncioTestCase):
    def setup_browser(self,pages):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        browser=Browser(Path(temporary.name));browser.start=AsyncMock()
        page=Mock();page.close=AsyncMock();current={'index':-1}
        browser.context=Mock();browser.context.new_page=AsyncMock(return_value=page)
        async def navigate(page,url,selector):
            current['index']+=1
            self.visited.append(url)
        self.visited=[];browser.navigate=AsyncMock(side_effect=navigate)
        cards=Mock();cards.evaluate_all=AsyncMock(side_effect=lambda script:pages[current['index']][0])
        next_link=Mock();next_link.filter.return_value=next_link;next_link.first=next_link
        next_link.count=AsyncMock(side_effect=lambda:1 if pages[current['index']][1] else 0)
        next_link.get_attribute=AsyncMock(side_effect=lambda attr:pages[current['index']][1])
        page.locator.side_effect=lambda selector:cards if selector=='.result-content' else next_link
        return browser,page

    async def test_processed_results_do_not_consume_new_candidate_budget(self):
        browser,page=self.setup_browser([([card(7701979050666902079),card(7701979050666902080)],None)])
        result=await browser.search('AI','article',1,['https://www.toutiao.com/article/7701979050666902079/?tracking=1'])
        self.assertEqual([r['content_id'] if 'content_id' in r else r['url'] for r in result],['https://www.toutiao.com/article/7701979050666902080/'])
        page.close.assert_awaited_once()

    async def test_all_processed_first_page_continues_relative_pagination(self):
        browser,_=self.setup_browser([([card(7701979050666902079)],'/search/?keyword=AI&page_num=1'),([card(7701979050666902080)],None)])
        result=await browser.search('AI','article',1,['https://www.toutiao.com/article/7701979050666902079/'])
        self.assertEqual(len(result),1)
        self.assertEqual(len(self.visited),2)
        self.assertEqual(self.visited[1],'https://so.toutiao.com/search/?keyword=AI&page_num=1')

    async def test_search_pagination_does_not_leave_platform(self):
        browser,_=self.setup_browser([([card(7701979050666902079)],'https://evil.example/search/')])
        self.assertEqual(await browser.search('AI','article',1,['https://www.toutiao.com/article/7701979050666902079/']),[])
        self.assertEqual(len(self.visited),1)

    async def test_known_old_results_do_not_consume_recent_candidate_budget(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        old=card(7701979050666902079);old['source']='来源\n2026-10-29 08:00'
        new=card(7701979050666902080);new['source']='来源\n2026-10-30 10:00'
        browser,_=self.setup_browser([([old,new],None)])
        since=datetime(2026,10,29,17,tzinfo=ZoneInfo('Asia/Shanghai'))
        result=await browser.search('AI','article',1,[],since)
        self.assertEqual(result[0]['url'],'https://www.toutiao.com/article/7701979050666902080/')
