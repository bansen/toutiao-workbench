"""Only rendered page DOM is read; no private endpoints or signatures."""
import asyncio
import re
import shutil
from pathlib import Path
from datetime import datetime
from urllib.parse import urlencode, urljoin, urlsplit

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

from .parsing import canonical_url, now_iso, parse_count, parse_time

CARDS_JS = """els => els.map(e => ({
  text: e.innerText,
  links: [...e.querySelectorAll('a[href]')].map(a => ({text:a.innerText,href:a.href})),
  source: e.querySelector('.cs-source-content')?.innerText || ''
}))"""


def parse_card(card,kind,search_url):
    link=None
    title=''
    author_text=''
    for anchor in card.get('links',[]):
        try:
            candidate=canonical_url(anchor['href'])
        except ValueError:
            if not author_text and anchor.get('text','').strip():
                author_text=anchor['text']
            continue
        if anchor.get('text','').strip():
            link=candidate
            title=anchor['text'].strip()
            break
    if not link:
        raise ValueError('卡片没有可识别的内容链接')
    meta=card.get('source') or author_text
    lines=meta.splitlines()
    def metric(label):
        # Only explicit standalone metric labels; body mentions are not counts.
        match=re.search(r'(?m)^\s*(\d+(?:\.\d+)?万?)\s*'+label+r'\s*$',card['text'])
        return parse_count(match[1]) if match else None
    return {'url':link,'title':title,'kind':kind,'author':lines[0] if lines else '',
            'published_at':parse_time(meta),'published_raw':meta,'comments':metric('评论'),
            'likes':metric('点赞'),'search_url':search_url,'list_text':card['text']}


class NeedsAttention(Exception):
    pass


class Browser:
    def __init__(self, profile, headless=True):
        self.profile = Path(profile)
        self.headless = headless
        self.context = None
        self.driver = None
        self.lock = asyncio.Lock()

    async def start(self, visible=False):
        if self.context and self.context.browser and not self.context.browser.is_connected():
            await self.close()
        if self.context:
            if not visible or not self.headless:
                return
            await self.close()
        self.profile.mkdir(parents=True, exist_ok=True)
        self.profile.chmod(0o700)
        self.driver = await async_playwright().start()
        self.headless = self.headless and not visible
        try:
            self.context = await self.driver.chromium.launch_persistent_context(
                str(self.profile), channel='chrome', headless=self.headless,
                viewport={'width':1360,'height':900}, locale='zh-CN', timezone_id='Asia/Hong_Kong')
        except Exception:
            await self.driver.stop()
            self.driver = None
            raise RuntimeError('无法启动 Chrome；请安装 Google Chrome，或关闭占用本工具专用配置目录的浏览器') from None

    async def close(self):
        if self.context:
            await self.context.close()
            self.context = None
        if self.driver:
            await self.driver.stop()
            self.driver = None

    async def login(self):
        async with self.lock:
            await self.start(visible=True)
            page = await self.context.new_page()
            await page.goto('https://www.toutiao.com/', wait_until='domcontentloaded')
            return {'status':'待人工确认','message':'请在独立 Chrome 窗口完成登录，再回到工具检查会话或恢复任务。'}

    async def status(self):
        if not self.context:
            return {'status':'未启动','message':'采集可先读取公开页面；需要登录时打开专用窗口。'}
        try:
            pages = self.context.pages
            if not pages:
                return {'status':'待人工确认'}
            text = await pages[-1].locator('body').inner_text(timeout=3000)
            blocked = self.blocked(text)
            return {'status':'需人工处理' if blocked else '浏览器已启动', 'message':blocked or '请在浏览器中确认登录；公开页面可用不表示账号已登录。'}
        except Exception:
            return {'status':'浏览器已关闭','message':'请重新打开登录窗口。'}

    async def clear(self):
        async with self.lock:
            await self.close()
            if self.profile.exists():
                shutil.rmtree(self.profile)
            return {'status':'会话已清除'}

    @staticmethod
    def blocked(text):
        for marker in ('请完成安全验证','访问过于频繁','访问受限','拖动滑块','请先登录后查看','扫码登录后查看','登录后继续阅读'):
            if marker in text:
                return marker
        return None

    async def check_blocking_ui(self,page):
        # Exclude source text: an article discussing captchas is not itself a
        # captcha. Visible surrounding UI/overlays are checked independently.
        ui_text=await page.locator('body').evaluate('''body => {
            const walker=document.createTreeWalker(body,NodeFilter.SHOW_TEXT);
            const parts=[];let node;
            while(node=walker.nextNode()) {
                const parent=node.parentElement;
                if(!parent || parent.closest('article,.result-content,script,style,noscript'))continue;
                const css=getComputedStyle(parent);
                if(css.visibility==='hidden' || css.display==='none' || !parent.getClientRects().length)continue;
                parts.push(node.textContent);
            }
            return parts.join('');
        }''')
        reason=self.blocked(ui_text)
        if reason:
            raise NeedsAttention(reason)
        dialogs=page.locator('[role="dialog"]:visible, dialog[open]:visible')
        for index in range(await dialogs.count()):
            if '登录' in await dialogs.nth(index).inner_text():
                raise NeedsAttention('页面出现登录弹层，请在专用浏览器处理后恢复')

    async def navigate(self, page, url, selector):
        response = await page.goto(url, wait_until='domcontentloaded', timeout=30000)
        if response and response.status in (401,403,429):
            raise NeedsAttention(f'页面返回 HTTP {response.status}，请在专用浏览器处理后恢复')
        try:
            await page.wait_for_function("""selector => !!document.querySelector(selector) ||
                document.body?.innerText.trim() === 'error'""", arg=selector, timeout=15000)
            if (await page.locator('body').inner_text()).strip() == 'error':
                raise ValueError('头条详情页显示 error，正文不可用')
            await self.check_blocking_ui(page)
        except PlaywrightTimeout:
            text = await page.locator('body').inner_text()
            if self.blocked(text):
                raise NeedsAttention(self.blocked(text))
            raise ValueError('页面未出现可识别的正文或搜索结果；可能暂无结果、页面改版或内容不可用') from None

    async def search(self, keyword, kind='article', limit=30, exclude_urls=None, since=None):
        await self.start()
        page = await self.context.new_page()
        results, seen = [], set()
        for value in exclude_urls or []:
            try:seen.add(canonical_url(value))
            except (ValueError,TypeError,AttributeError):continue
        visited=set()
        try:
            url = 'https://so.toutiao.com/search/?'+urlencode({'keyword':keyword,'pd':'information' if kind=='article' else 'weitoutiao','dvpf':'pc'})
            for number in range(4):
                if url in visited:break
                visited.add(url)
                try:
                    await self.navigate(page,url,'.result-content a[href*="/search/jump"], .result-content a[href*="/article/"], .result-content a[href*="/w/"]')
                except ValueError:
                    if results:
                        break
                    raise
                cards = await page.locator('.result-content').evaluate_all(CARDS_JS)
                for card in cards:
                    try:
                        item = parse_card(card,kind,url)
                    except ValueError:
                        continue
                    link=item['url']
                    if link in seen:
                        continue
                    seen.add(link)
                    if since is not None and item.get('published_at') and datetime.fromisoformat(item['published_at'])<since:
                        continue
                    results.append(item)
                    if len(results)>=limit:
                        return results
                next_link = page.locator('a').filter(has_text=re.compile(r'^下一页$'))
                if not cards or not await next_link.count():
                    break
                href = await next_link.first.get_attribute('href')
                if not href:
                    break
                target=urljoin(url,href).replace('http://','https://',1)
                parsed=urlsplit(target)
                if parsed.scheme!='https' or parsed.hostname not in {'so.toutiao.com','sou.toutiao.com'} or parsed.username or parsed.path.rstrip('/')!='/search':
                    break
                url=target
            return results
        finally:
            await page.close()

    async def detail(self, url, kind='article'):
        await self.start()
        url = canonical_url(url)
        page = await self.context.new_page()
        try:
            await self.navigate(page,url,'article')
            body = await page.locator('article').first.inner_text()
            if not body.strip():
                raise ValueError('正文为空，未使用搜索摘要代替正文')
            title = await page.locator('.article-content > h1').first.inner_text() if await page.locator('.article-content > h1').count() else (await page.locator('h1').first.inner_text() if await page.locator('h1').count() else body[:60])
            meta = await page.locator('.article-meta').inner_text() if await page.locator('.article-meta').count() else ''
            author=meta.split('·',1)[-1].strip() if '·' in meta else ''
            if await page.locator('.wtt-meta').count():
                kind='micro'
                meta=await page.locator('.wtt-meta .time').inner_text() if await page.locator('.wtt-meta .time').count() else ''
                author=await page.locator('.wtt-meta .name').inner_text() if await page.locator('.wtt-meta .name').count() else ''
            final_url = canonical_url(page.url)
            timestamp = now_iso()
            return {'url':final_url,'content_id':re.search(r'\d{10,25}',final_url)[0], 'kind':kind,
                    'title':title.strip(),'body':body.strip(),'author':author,
                    'published_at':parse_time(meta),'published_raw':meta,'reads':None,'comments':None,'likes':None,
                    'body_source':final_url,'metrics_source':final_url,'body_collected_at':timestamp,'metrics_collected_at':timestamp,'collected_at':timestamp}
        finally:
            await page.close()
