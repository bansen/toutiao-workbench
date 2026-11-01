"""Read-only creator statistics; shares the publishing browser and its lock."""
import asyncio,json,uuid,re
from .parsing import parse_count,parse_time
from .toutiao_publisher import platform_status
from datetime import datetime
from pathlib import Path
from .browser import NeedsAttention
from .micro_schedule import TIMEZONE
from .toutiao_publisher import ToutiaoPublisher,MANAGE

LABELS={'展现':'impressions','展现量':'impressions','阅读':'reads','阅读量':'reads','点赞':'likes','评论':'comments','收藏':'favorites','分享':'shares','涨粉':'follows','创作收益':'revenue'}

def detail_metrics(cells):
    values={key:None for key in set(LABELS.values())}
    for cell in cells:
        key=LABELS.get(cell.get('label','').strip())
        if not key:continue
        text=(cell.get('value') or '').strip()
        if key=='revenue':
            values[key]=float(text) if re.fullmatch(r'\d+(?:\.\d+)?',text) else None
        else:values[key]=parse_count(text)
    return values

def normalize_row(raw,now):
    values=detail_metrics([{'label':m[1],'value':m[2]} for text in raw.get('stats',[]) if (m:=re.fullmatch(r'(展现|阅读|点赞|评论)\s*(.*)',text.strip()))])
    body=raw.get('body','').removesuffix('展开...').removesuffix('展开').strip()
    return {'platform_id':None,'title':body.splitlines()[0] if body else '', 'body':body,
        'published_at':parse_time(raw.get('time',''),now),'observed_at':now.isoformat(),
        'status':platform_status(raw.get('status','')),'metrics':values,'scope':'unverified',
        'source_url':None,'content_kind':'unknown','subtopic':None,'editorial_context':None,
        'field_sources':{'title':'作品管理 .wtt-text','published_at':'作品管理 .time','status':'作品管理 .tags','metrics':'作品管理 .stat'},
        'statistical_scope':'单篇作品界面快照；未核实累计口径'}

class CreatorAnalytics:
    def __init__(self,browser,account_id,data_dir,publish_busy,store=None):
        self.browser,self.account_id=browser,account_id
        self.directory=Path(data_dir)/'reviews'
        self.publish_busy=publish_busy
        self.store=store

    def attach_metadata(self,record):
        """Bind experiments only through an account-scoped publication receipt.

        Creator DOM can collapse line breaks. Text similarity is neither stable
        identity nor proof that a manual post belongs to an experiment.
        """
        if not self.store or not record.get('platform_id'):return
        with self.store.connection() as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='publication_history'").fetchone():return
            published=[json.loads(r[0]) for r in db.execute('SELECT payload FROM publication_history WHERE account_id=?',(self.account_id(),))]
        matches=[r for r in published if str((r.get('receipt') or {}).get('content_id',''))==record['platform_id']]
        if len(matches)!=1:return
        snapshot=matches[0];draft=snapshot
        if snapshot.get('draft_id'):
            try:draft=self.store.draft(snapshot['draft_id'])
            except KeyError:return
        context=draft.get('editorial_context')
        record['editorial_context']=context
        record['content_kind']=(context or {}).get('content_kind') or ('tool-guide' if draft.get('sources') and all(s.get('kind')=='official' for s in draft['sources']) else 'news')
        record['subtopic']=draft.get('topic')
        record.setdefault('field_sources',{})['editorial_context']='当前账号发布回执 → 原始草稿'

    async def probe(self,surface='manage',detail=False):
        if not self.account_id():raise NeedsAttention('请先绑定头条账号')
        if self.publish_busy():return {'state':'yielded','reason':'发布优先，稍后重试'}
        actor=ToutiaoPublisher(self.browser,self.account_id)
        try:
            async def read():
                url=MANAGE if surface=='manage' else 'https://mp.toutiao.com/profile_v4/analysis/works-overall/all'
                await actor._open(url,'.post-item' if surface=='manage' else 'body',check_account=False)
                if (await actor._identity())['id']!=self.account_id():raise NeedsAttention('后台账号与绑定账号不一致')
                if detail and surface=='overview':
                    await actor.page.locator('#menuTab .byte-tabs-header-title').get_by_text('单篇',exact=True).click()
                    await actor.page.wait_for_timeout(1500)
                rows=await actor.page.locator('.post-item').evaluate_all('(els)=>els.slice(0,10).map(e=>({text:e.innerText,html:e.outerHTML}))')
                links=await actor.page.locator('a[href]').evaluate_all('(els)=>els.filter(e=>e.getClientRects().length).map(e=>({text:e.innerText,href:e.href}))')
                (self.directory/'raw').mkdir(parents=True,exist_ok=True,mode=0o700)
                (self.directory/'screenshots').mkdir(parents=True,exist_ok=True,mode=0o700)
                identity=uuid.uuid4().hex
                path=self.directory/'raw'/('probe-'+identity+'.json')
                path.touch(mode=0o600)
                payload={'at':datetime.now(TIMEZONE).isoformat(),'url':actor.page.url,'rows':rows,'links':links,
                         'body':await actor.page.locator('body').inner_text(),'html':await actor.page.locator('body').evaluate("e=>{const c=e.cloneNode(true);c.querySelectorAll('script,style,noscript,input[type=hidden]').forEach(n=>n.remove());return c.innerHTML}")}
                if detail and surface=='manage':
                    await actor.page.locator('.post-item').first.get_by_text('查看数据',exact=True).click()
                    await actor.page.locator('.byte-drawer').wait_for(timeout=10000)
                    await actor.page.wait_for_timeout(1000)
                    tips=[]
                    for icon in await actor.page.locator('.byte-drawer svg.question-circle-icon').all():
                        try:
                            await icon.hover(timeout=1000)
                            await actor.page.wait_for_timeout(300)
                            tips.append(await actor.page.locator('body').inner_text())
                        except Exception:pass
                    payload['detail']={'url':actor.page.url,'tips':tips,'body':await actor.page.locator('.byte-drawer').inner_text(),
                        'html':await actor.page.locator('.byte-drawer').inner_html()}
                path.write_text(json.dumps(payload,ensure_ascii=False))
                screenshot=self.directory/'screenshots'/('probe-'+identity+'.png')
                await actor.page.screenshot(path=str(screenshot),full_page=True)
                screenshot.chmod(0o600)
                return {'state':'captured','samples':len(rows),'private_path':str(path),'screenshot':str(screenshot)}
            return await asyncio.wait_for(read(),30)
        finally:
            try:await asyncio.wait_for(actor.close(),7)
            except asyncio.TimeoutError:pass

    async def collect(self,target_date):
        """Walk the complete management pagination, including manual micro-posts.

        One rendered row is the largest locked unit. No source is executed and no
        state-changing creator control (modify/delete/publish) is touched.
        """
        from datetime import date,timedelta
        from .browser import close_page
        if not self.account_id():raise NeedsAttention('请先绑定头条账号')
        target=date.fromisoformat(target_date)
        now=datetime.now(TIMEZONE)
        cutoff=now-timedelta(hours=72)
        actor=ToutiaoPublisher(self.browser,self.account_id)
        rows=[];seen=set();pages=set();complete=False;warnings=[]
        capture_id=uuid.uuid4().hex;sample_captures=0;stage='打开作品管理'
        checkpoint=self.directory/'raw'/f'collection-{capture_id}.json'
        checkpoint.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        checkpoint.touch(mode=0o600)
        def result():
            return {'rows':rows,'complete':complete and not warnings,'capabilities':capabilities,'warnings':list(dict.fromkeys(warnings)),
                'account_checked':True,'pagination':{'pages':len(pages),'complete':complete},
                'verified_samples':sum(all(r.get('verification',{}).values()) for r in [r for r in rows if r['published_at'][:10]==target_date][:10])}
        capabilities={k:None for k in set(LABELS.values())}
        async def acquire():
            if actor.owns_lock:return
            while True:
                while self.publish_busy():await asyncio.sleep(1)
                await self.browser.lock.acquire()
                if self.publish_busy():self.browser.lock.release();await asyncio.sleep(.2);continue
                actor.owns_lock=True;return
        def release():
            if actor.owns_lock:actor.owns_lock=False;self.browser.lock.release()
        try:
            while self.publish_busy():await asyncio.sleep(1)
            await asyncio.wait_for(actor._open(MANAGE,'.post-item'),60)
            for page_number in range(1,101):
                await acquire()
                stage='核对账号及读取列表'
                await actor.check_account()
                raw=await actor.page.locator('.post-item').evaluate_all('''els=>els.map(e=>({
                    body:e.querySelector('.wtt-text')?.innerText||'',
                    time:e.querySelector('.time')?.innerText||'',
                    status:e.querySelector('.tags')?.innerText||'',
                    stats:[...e.querySelectorAll('.stat li')].map(n=>n.innerText)
                }))''')
                signature=tuple((r['body'],r['time']) for r in raw)
                if signature in pages:warnings.append('分页重复，覆盖不完整');break
                pages.add(signature)
                release()
                for index,item in enumerate(raw):
                    if not item['body']:continue
                    record=normalize_row(item,datetime.now(TIMEZONE))
                    published=record.get('published_at')
                    if not published:warnings.append('作品发布时间不可识别');continue
                    when=datetime.fromisoformat(published)
                    if when.date()>target or (when.date()!=target and when<cutoff):continue
                    await acquire()
                    try:
                        await self.browser.check_blocking_ui(actor.page)
                        stage='读取公开作品ID'
                        row=actor.page.locator('.post-item').nth(index)
                        popup=None
                        try:
                            async with actor.page.expect_popup(timeout=6000) as opened:
                                await row.locator('.wtt-text').click(timeout=3000)
                            popup=await opened.value
                            # Only the rendered public navigation URL is used as identity.
                            await popup.wait_for_url(re.compile(r'https://www\.toutiao\.com/w/\d+/?(?:\?.*)?$'),timeout=6000,wait_until='commit')
                            match=re.match(r'https://www\.toutiao\.com/w/(\d+)',popup.url)
                            if match:record.update(platform_id=match[1],source_url='https://www.toutiao.com/w/'+match[1]+'/')
                        except Exception:warnings.append('部分作品公开链接不可读取')
                        finally:
                            if popup:await close_page(popup)
                        stage='打开单篇数据'
                        await row.get_by_text('查看数据',exact=True).click(timeout=3000)
                        drawer=actor.page.locator('.byte-drawer')
                        await drawer.locator('.single-data-board-group').wait_for(timeout=10000)
                        await drawer.locator('.interaction-alone .inter-item').first.wait_for(timeout=10000)
                        stage='读取单篇指标'
                        cells=await drawer.locator('.primary,.second,.up,.inter-item').evaluate_all('''els=>els.filter(e=>e.querySelector('.num')).map(e=>{
                            const n=e.querySelector('.num');const c=e.cloneNode(true);c.querySelectorAll('.num,svg').forEach(n=>n.remove());
                            return {label:c.textContent.replace(/元/g,'').trim(),value:n.innerText};})''')
                        metrics=detail_metrics(cells)
                        record['metrics'].update(metrics)
                        record['field_sources']['metrics']='作品管理 → 查看数据 → 单篇流量/互动/粉丝/收益'
                        # Scope must be evidenced in this exact rendered data panel.
                        scope_text=await drawer.locator('.single-data-board-group').inner_text()
                        if any(line.strip() in ('累计数据','作品累计数据','累计作品数据') for line in scope_text.splitlines()):
                            record['scope']='cumulative';record['statistical_scope']='单篇累计指标（界面标注累计）；部分数据延迟更新'
                        record['observed_at']=datetime.now(TIMEZONE).isoformat()
                        record['verification']={'list_drawer_match':all(record['metrics'].get(k)==normalize_row(item,now)['metrics'].get(k) for k in ('impressions','reads','likes','comments')),
                            'time_match':(await drawer.locator('.info-time').inner_text()).strip()==item['time'].strip(),
                            'public_id_verified':bool(record['platform_id'])}
                        if when.date()==target and sample_captures<10:
                            directory=self.directory/'screenshots';directory.mkdir(parents=True,exist_ok=True,mode=0o700)
                            screenshot=directory/f'validation-{capture_id}-{sample_captures+1}.png'
                            await drawer.screenshot(path=str(screenshot));screenshot.chmod(0o600)
                            sample_captures+=1
                        stage='关闭单篇抽屉'
                        for attempt in range(2):
                            if not await drawer.is_visible():break
                            try:
                                await drawer.locator('.byte-drawer-close-icon').click(timeout=8000,no_wait_after=True)
                                await drawer.wait_for(state='hidden',timeout=10000)
                                break
                            except Exception:
                                if not await drawer.is_visible():break
                                if attempt:raise

                        for key,value in record['metrics'].items():
                            if value is not None:capabilities[key]=True
                    finally:release()
                    if not record['platform_id']:warnings.append('作品ID缺失');continue
                    if record['platform_id'] in seen:continue
                    self.attach_metadata(record)
                    seen.add(record['platform_id']);rows.append(record)
                    checkpoint.write_text(json.dumps(rows,ensure_ascii=False))
                await acquire()
                next_button=actor.page.locator('.fake-pagination-item').filter(has=actor.page.locator('svg.byte-icon-right'))
                if await next_button.count()!=1:warnings.append('分页下一页控件不可识别');break
                classes=await next_button.get_attribute('class') or ''
                active=(await actor.page.locator('.fake-pagination-item.active').inner_text()).strip()
                if 'disabled' in classes:
                    complete=True;break
                first=await actor.page.locator('.post-item').first.inner_text()
                stage='切换下一页'
                await next_button.click(timeout=3000)
                await actor.page.wait_for_function('''arg=>{const e=document.querySelector('.post-item');return e&&e.innerText&&e.innerText!==arg.old&&document.querySelector('.fake-pagination-item.active')?.innerText.trim()===String(arg.next)}''',arg={'old':first,'next':int(active)+1},timeout=15000)
                release()
            else:warnings.append('超过100页安全上限')
            for row in rows:
                if row['scope']!='cumulative':warnings.append('单篇指标累计口径未得到界面明确确认');break
            return result()
        except Exception as exc:
            warnings.append(stage+'失败（'+type(exc).__name__+'）')
            error=self.directory/'raw'/f'collection-error-{capture_id}.json'
            error.touch(mode=0o600)
            error.write_text(json.dumps({'stage':stage,'error_type':type(exc).__name__,'detail':str(exc)[:2000]},ensure_ascii=False))
            complete=False
            response=result()
            if isinstance(exc,NeedsAttention):response['account_checked']=False
            return response
        finally:
            try:await asyncio.wait_for(actor.close(),7)
            except Exception:pass
