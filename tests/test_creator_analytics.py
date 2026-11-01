import asyncio,tempfile,unittest
from pathlib import Path
from unittest.mock import AsyncMock
from workbench.creator_analytics import CreatorAnalytics
from workbench.browser import NeedsAttention

class AnalyticsProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_unbound_account_never_opens_browser(self):
        with tempfile.TemporaryDirectory() as directory:
            browser=type('Browser',(),{'start':AsyncMock(),'lock':asyncio.Lock()})()
            collector=CreatorAnalytics(browser,lambda:'',Path(directory),lambda:False)
            with self.assertRaises(NeedsAttention):await collector.probe()
            browser.start.assert_not_awaited()
    async def test_due_publishing_yields_without_opening_browser(self):
        with tempfile.TemporaryDirectory() as directory:
            browser=type('Browser',(),{'start':AsyncMock(),'lock':asyncio.Lock()})()
            collector=CreatorAnalytics(browser,lambda:'account',Path(directory),lambda:True)
            result=await collector.probe()
            self.assertEqual(result['state'],'yielded')
            browser.start.assert_not_awaited()
