"""Creator UI adapter. Only rendered DOM; never a private publishing API."""
import asyncio
import re
from datetime import datetime

from .browser import NeedsAttention
from .parsing import canonical_url,parse_time
from .publication_guard import digest
from .publishing_checks import check_publish_declarations
from .micro_schedule import TIMEZONE

EDITOR='.ProseMirror[contenteditable="true"]'
CREATE='https://mp.toutiao.com/profile_v4/weitoutiao/publish'
MANAGE='https://mp.toutiao.com/profile_v4/manage/content/all?enter_from=left_menu'

# The site retains this cache even when an empty editor emits an input event.
# Clear only a cache whose rendered text matches the snapshot we own.
CLEAR_OWNED_DRAFT_JS = r"""owned => {
    const key = 'wtt-mpeditordraft';
    const normal = text => text.replace(/\s+/g, ' ').trim();
    try {
        const cache = JSON.parse(localStorage.getItem(key));
        if (!cache || typeof cache.content !== 'string') return false;
        const doc = new DOMParser().parseFromString(cache.content, 'text/html');
        const paragraphs = [...doc.querySelectorAll('p')];
        const text = paragraphs.length ? paragraphs.map(p => p.textContent).join('\n') : doc.body.textContent;
        if (normal(text) !== normal(owned)) return false;
        localStorage.removeItem(key);
        return true;
    } catch { return false; }
}"""


def compact(text): return re.sub(r'\s+','',text)


def rendered_text(text):
    # Chromium inserts an extra visual newline for empty editable blocks.
    # Keep word boundaries; never turn 'now here' into 'nowhere'.
    return re.sub(r'\s+',' ',text).strip()


def platform_status(text):
    if any(v in text for v in ('未通过','审核不通过','发布失败')):return 'rejected'
    if '已发布' in text:return 'published'
    if any(v in text for v in ('审核中','审核通过','发布中','已提交')):return 'submitted'
    return 'uncertain'


class ToutiaoPublisher:
    def __init__(self,browser,account_id,expected_ai=None):
        self.browser=browser
        self.account_id=account_id # callable: never silently adopt a different logged-in account
        self.expected_ai=expected_ai or (lambda:False)
        self.allowed=lambda:False
        self.page=None
        self.owns_lock=False
        self.snapshot=None
        self.editor_body=None

    async def close(self):
        try:
            if self.page and not self.page.is_closed():
                try:
                    # Only clear text this adapter prepared, never a user's edits.
                    if self.editor_body is not None:
                        editor=self.page.locator(EDITOR)
                        if await editor.count()==1 and rendered_text(await editor.inner_text())==rendered_text(self.editor_body):
                            await editor.fill('')
                            await self.page.evaluate(CLEAR_OWNED_DRAFT_JS, self.editor_body)
                except Exception:
                    pass # Cleanup must not replace the original failure or receipt.
                finally:
                    await self.page.close()
        finally:
            self.page=None
            self.snapshot=None
            self.editor_body=None
            if self.owns_lock:
                self.owns_lock=False
                self.browser.lock.release()

    async def _open(self,url,selector,check_account=True):
        if self.page:raise NeedsAttention('发布窗口已被占用，请先核查上一次提交')
        await self.browser.lock.acquire()
        self.owns_lock=True
        try:
            await self.browser.start()
            self.page=await self.browser.context.new_page()
            response=await self.page.goto(url,wait_until='domcontentloaded',timeout=45000)
            if response and response.status in (401,403,429):raise NeedsAttention('创作者后台限制访问或登录失效')
            await self.page.locator(selector).first.wait_for(timeout=20000)
            if check_account:await self.check_account()
        except BaseException:
            await self.close()
            raise

    async def identify(self):
        try:
            await self._open(CREATE,EDITOR,check_account=False)
            await self.browser.check_blocking_ui(self.page)
            links=self.page.locator('a[href*="/c/user/"]')
            if await links.count()!=1:raise NeedsAttention('无法唯一识别登录账号')
            match=re.search(r'/c/user/(\d+)/',await links.get_attribute('href') or '')
            if not match:raise NeedsAttention('当前账号主页链接不可识别')
            if self.account_id() and match[1]!=self.account_id():raise NeedsAttention('当前登录账号与绑定账号不一致，不自动更换绑定')
            return {'id':match[1],'name':(await links.inner_text()).strip() or '头条账号 '+match[1][-6:]}
        finally:await self.close()

    async def check_account(self):
        await self.browser.check_blocking_ui(self.page)
        links=self.page.locator('a[href*="/c/user/"]')
        if await links.count()!=1:raise NeedsAttention('无法唯一识别当前头条账号，请人工登录核实')
        match=re.search(r'/c/user/(\d+)/',await links.get_attribute('href') or '')
        if not match or match[1]!=self.account_id():raise NeedsAttention('当前头条账号与绑定账号不一致，已暂停')
        text=await self.page.locator('body').inner_text()
        # Administrative UI only; source article text is never used here.
        for marker in ('今日发文次数已达上限','发文数量已达上限','发布频繁','暂时无法发布','账号被封禁','发文功能受限'):
            if marker in text:raise NeedsAttention('平台发文限制：'+marker)
        return match[1]

    def label(self,name):
        return self.page.locator('label').filter(has=self.page.get_by_text(name,exact=True))

    async def declarations(self,apply=False):
        result={}
        for name,key,desired in [('头条首发','exclusive',False),('引用AI','ai',self.expected_ai())]:
            label=self.label(name)
            if await label.count()!=1 or await label.locator('input[type="checkbox"]').count()!=1:
                raise NeedsAttention('发布声明控件不可识别：'+name)
            control=label.locator('input[type="checkbox"]')
            # The platform can auto-select first release after text is filled;
            # styles can also lag. Cancel once, then require three stable samples.
            stable=0
            clicked=False
            for _ in range(10):
                await asyncio.sleep(.2)
                result[key]={'checked':await control.is_checked(),'classes':await label.get_attribute('class')}
                matches=(result[key]['checked']==desired and
                         ('byte-checkbox-checked' in (result[key]['classes'] or '').split())==desired)
                if apply and result[key]['checked']!=desired and not clicked:
                    await label.get_by_text(name,exact=True).click()
                    clicked=True
                    stable=0
                    continue
                stable=stable+1 if matches else 0
                if stable>=3:break
            if stable<3:raise NeedsAttention('声明状态未稳定：'+name)
        check_publish_declarations(**result,expected_ai=self.expected_ai())
        return result

    async def probe(self):
        try:
            await self._open(CREATE,EDITOR)
            await self.declarations(apply=True)
            return {'account_id':self.account_id(),'declaration_verified':True,'ai_declaration':self.expected_ai()}
        finally:await self.close()

    async def preflight(self,draft):
        await self._open(CREATE,EDITOR)
        editor=self.page.locator(EDITOR)
        previous=(await editor.inner_text()).strip()
        if previous and previous!='有什么新鲜事想告诉大家？' and rendered_text(previous)!=rendered_text(draft['body']):
            raise NeedsAttention('微头条编辑器已有其他内容，已暂停以免覆盖')
        self.editor_body=draft['body']
        await editor.fill(draft['body'])
        actual=(await editor.inner_text()).replace('\r\n','\n').strip()
        if rendered_text(actual)!=rendered_text(draft['body']):raise NeedsAttention('编辑器正文与固定快照不一致，未提交')
        declarations=await self.declarations(apply=True)
        self.snapshot={'body_sha256':digest(draft['body']),'editor_body_sha256':digest(actual),'account_id':await self.check_account(),**declarations}
        return self.snapshot

    async def submit(self,draft):
        if not self.page or not self.snapshot:raise NeedsAttention('未完成发布前检查，禁止提交')
        if not self.allowed():raise NeedsAttention('发布已暂停、关闭或错过时段，未点击发布')
        await self.check_account()
        await self.declarations(apply=True)
        actual=(await self.page.locator(EDITOR).inner_text()).replace('\r\n','\n').strip()
        if rendered_text(actual)!=rendered_text(draft['body']) or digest(actual)!=self.snapshot['editor_body_sha256']:
            raise NeedsAttention('待发布正文发生变化，未提交')
        # Last synchronous decision immediately precedes the one allowed click.
        if not self.allowed():raise NeedsAttention('发布条件发生变化，未点击发布')
        await self.page.get_by_role('button',name='发布',exact=True).click(timeout=10000)
        try:
            await self.page.wait_for_function("location.pathname.includes('/manage/content/') || document.body.innerText.includes('发布成功')",timeout=15000)
        except Exception:
            pass # Query records; never click a second time.
        await self.page.goto(MANAGE,wait_until='domcontentloaded',timeout=45000)
        await self.page.locator('.post-item').first.wait_for(timeout=20000)
        await self.check_account()
        receipt=await self._lookup(draft)
        receipt['declarations']=self.snapshot
        return receipt

    async def _lookup(self,draft):
        matches=[]
        today_count=0
        today=datetime.now(TIMEZONE).date()
        for row in await self.page.locator('.post-item').all():
            times=row.locator('.time')
            when=parse_time(await times.first.inner_text()) if await times.count() else None
            if when and datetime.fromisoformat(when).date()==today:today_count+=1
            text=row.locator('.wtt-text')
            if not await text.count():continue
            content=text.locator('.overflow-handler-container')
            raw=await content.first.inner_text() if await content.count() else re.sub(r'\s*展开\s*$','',await text.inner_text())
            if compact(raw)==compact(draft['body']):matches.append(row)
        if len(matches)!=1:raise NeedsAttention('未在当前作品记录中找到唯一匹配正文，结果待核实，禁止重发')
        row=matches[0]
        tags=await row.locator('.tags').inner_text()
        state=platform_status(tags)
        base={'status':state,'platform_status':tags,'body_verified':False,
              'unexpected_exclusive_tag':'首发' in tags,'visible_today_count':today_count,
              'checked_at':datetime.now(TIMEZONE).isoformat()}
        try:
            async with self.page.expect_popup(timeout=10000) as popup_info:
                await row.locator('.wtt-text').click()
            popup=await popup_info.value
        except Exception:
            if state in ('submitted','rejected'):return base
            raise
        try:
            try:
                await popup.wait_for_url(re.compile(r'https://www\.toutiao\.com/w/\d+'),wait_until='domcontentloaded',timeout=20000)
                url=canonical_url(popup.url)
            except Exception:
                if state in ('submitted','rejected'):return base
                raise
            if state in ('submitted','rejected'):
                return {**base,'url':url,'content_id':re.search(r'/w/(\d+)',url)[1]}
            await popup.locator('article').first.wait_for(timeout=15000)
            body=await popup.locator('article').first.inner_text()
            if rendered_text(body)!=rendered_text(draft['body']):raise NeedsAttention('公开正文与发布快照不一致，须人工核查')
            public_text=await popup.locator('body').inner_text()
            ai_public='内容由AI生成' in public_text or '内容由 AI 生成' in public_text
            if self.expected_ai() and not ai_public:raise NeedsAttention('公开页未核实AI声明，须人工核查')
            return {'status':platform_status(tags),'platform_status':tags,'url':url,
                    'content_id':re.search(r'/w/(\d+)',url)[1],'body_verified':True,
                    'ai_public':ai_public,'unexpected_ai_tag':ai_public!=self.expected_ai(),'unexpected_exclusive_tag':'首发' in tags,
                    'visible_today_count':today_count,'checked_at':datetime.now(TIMEZONE).isoformat()}
        finally:await popup.close()

    async def reconcile(self,row):
        try:
            await self._open(MANAGE,'.post-item')
            return await self._lookup(row)
        finally:await self.close()
