import asyncio
import time
from contextlib import suppress

from .browser import NeedsAttention
from .parsing import select_items


class Engine:
    def __init__(self,store,browser,llm,capabilities):
        self.store=store
        self.browser=browser
        self.llm=llm
        self.capabilities=capabilities
        self.queue=asyncio.Queue(maxsize=100)
        self.queued=set()
        self.stop_ids=set()
        self.paused=False
        self.active_id=None
        self.execution_lock=asyncio.Lock()
        self.worker_task=None
        self.clock_task=None
        self.next_run=None

    async def start(self):
        self.store.recover()
        schedule=self.store.setting('schedule',{'enabled':False,'minutes':30,'collection':None})
        self.next_run=time.monotonic()+schedule['minutes']*60 if schedule.get('enabled') else None
        self.worker_task=asyncio.create_task(self.worker())
        self.clock_task=asyncio.create_task(self.clock())

    async def close(self):
        for task in (self.worker_task,self.clock_task):
            if task:
                task.cancel()
        for task in (self.worker_task,self.clock_task):
            if task:
                with suppress(asyncio.CancelledError): await task
        await self.browser.close()

    def submit(self,kind,options):
        if self.queue.full():
            raise ValueError('任务队列已满，请等待现有任务完成')
        task=self.store.create_task(kind,options)
        self.enqueue(task['id'])
        return task

    def enqueue(self,identity):
        if identity not in self.queued and identity!=self.active_id:
            self.queue.put_nowait(identity)
            self.queued.add(identity)

    def stopped(self,identity):
        return identity in self.stop_ids

    def stop(self,identity):
        self.store.task(identity)
        self.stop_ids.add(identity)
        if identity!=self.active_id:
            self.store.update_task(identity,status='stopped')
        self.store.log(identity,'已请求停止；当前页面或模型请求结束后停止。')

    def retry(self,identity):
        task=self.store.task(identity)
        if task['status'] not in ('failed','partial','stopped'):
            raise ValueError('此任务无需重试；待人工处理的任务请使用恢复采集')
        if self.queue.full():
            raise ValueError('任务队列已满')
        self.stop_ids.discard(identity)
        self.store.update_task(identity,status='waiting',errors=0)
        self.store.log(identity,'手动重试，已生成的成功版本将跳过。')
        self.enqueue(identity)

    async def resume(self):
        attention=[t for t in self.store.tasks(limit=None) if t['status']=='attention']
        if len(attention)+self.queue.qsize()>100:
            raise ValueError('队列已满，暂时无法恢复')
        self.paused=False
        for task in attention:
            self.store.update_task(task['id'],status='waiting')
            self.enqueue(task['id'])

    async def worker(self):
        while True:
            identity=await self.queue.get()
            self.queued.discard(identity)
            try:
                await self.execute(identity)
            finally:
                self.queue.task_done()

    async def execute(self,identity):
        async with self.execution_lock:
            self.active_id=identity
            task=self.store.task(identity)
            try:
                if self.stopped(identity):
                    self.store.update_task(identity,status='stopped')
                    return
                if self.paused and task['kind'] in ('collect','import'):
                    self.store.update_task(identity,status='attention')
                    self.store.log(identity,'采集已暂停，请处理浏览器后点击恢复。')
                    return
                self.store.update_task(identity,status='running',errors=0)
                if task['kind'] in ('collect','import'):
                    await self.collect(identity,task['options'],task['kind']=='import')
                else:
                    await self.generate(identity,task['options'])
                current=self.store.task(identity)
                status='stopped' if self.stopped(identity) else ('partial' if current['errors'] and (current['accepted'] or current['generated']) else ('failed' if current['errors'] else 'success'))
                self.store.update_task(identity,status=status)
            except NeedsAttention as exc:
                self.paused=True
                self.store.update_task(identity,status='attention')
                self.store.log(identity,str(exc))
            except (ValueError,KeyError) as exc:
                self.store.update_task(identity,status='failed')
                self.store.log(identity,str(exc))
            except Exception:
                self.store.update_task(identity,status='failed')
                self.store.log(identity,'任务异常终止，请检查浏览器是否关闭，或重新运行采集验证。')
            finally:
                self.active_id=None

    async def browser_step(self,method,*args):
        # Release between pages, so a due publication goes before the next
        # collection step. No model request holds the browser lock.
        idle=getattr(self.browser,'publishing_idle',None)
        while True:
            if idle is not None:await idle.wait()
            async with self.browser.lock:
                if idle is not None and not idle.is_set():continue
                return await method(*args)

    async def collect(self,identity,options,is_import=False):
        kind=options.get('kind','article')
        kinds=options.get('kinds') or [kind]
        if any(not self.capabilities.get(selected) for selected in kinds):
            raise ValueError('该体裁的正文采集未通过验证')
        if options.get('auto_generate') and not self.llm.status()['configured']:
            raise ValueError('自动生成需要先配置模型；关闭自动生成仍可采集素材')
        self.store.update_task(identity,checked=0,accepted=0,unknown=0)
        if is_import:
            cards=[{'url':u,'kind':'micro' if '/w/' in u else 'article'} for u in options['urls']]
        else:
            domains=options.get('domains') or [options.get('domain','')]
            combinations=[(selected,domain) for domain in domains for selected in kinds]
            total=options.get('limit',30)
            if len(combinations)>total:
                raise ValueError('每轮检查上限小于所选体裁与领域组合数，请提高上限')
            cards=[]
            seen=set()
            for index,(selected,domain) in enumerate(combinations):
                if self.stopped(identity):break
                keyword=' '.join(x for x in (options.get('keyword'),domain) if x)
                budget=total//len(combinations)+(1 if index<total%len(combinations) else 0)
                self.store.log(identity,f'搜索：{keyword} / {selected}，候选额度 {budget}；领域为关键词映射。')
                try:
                    found=await self.browser_step(self.browser.search,keyword,selected,budget)
                except ValueError as exc:
                    self.store.log(identity,f'此组合未取得结果：{exc}')
                    continue
                for card in found[:budget]:
                    if card['url'] not in seen:
                        cards.append(card)
                        seen.add(card['url'])
        self.store.log(identity,f'发现 {len(cards)} 个候选；仅对本轮发现结果筛选。')
        for card in cards:
            if self.stopped(identity): break
            state=self.store.task(identity)
            self.store.update_task(identity,checked=state['checked']+1)
            try:
                item=await self.browser_step(self.browser.detail,card['url'],card.get('kind',kind))
                for field in ('author','published_at','published_raw','comments','likes'):
                    if item.get(field) in (None,'') and card.get(field) not in (None,''):
                        item[field]=card[field]
                if card.get('comments') is not None or card.get('likes') is not None:
                    item['metrics_source']=card.get('search_url',item.get('url'))
                rows,unknown=select_items([item],{} if is_import else options)
                if unknown:
                    state=self.store.task(identity)
                    self.store.update_task(identity,unknown=state['unknown']+unknown)
                if not rows: continue
                row,new=self.store.upsert_material(item)
                state=self.store.task(identity)
                new_ids=state['new_ids']+([row['id']] if new and row['id'] not in state['new_ids'] else [])
                self.store.update_task(identity,accepted=state['accepted']+1,new_ids=new_ids)
                self.store.log(identity,('新增：' if new else '更新：')+item['title'][:80])
            except NeedsAttention:
                raise
            except ValueError as exc:
                state=self.store.task(identity)
                self.store.update_task(identity,errors=state['errors']+1)
                self.store.log(identity,f'跳过候选：{exc}')
            except Exception:
                state=self.store.task(identity)
                self.store.update_task(identity,errors=state['errors']+1)
                self.store.log(identity,'候选读取失败，可能超时或浏览器已关闭。')
        if options.get('auto_generate') and not self.stopped(identity):
            # Persist the IDs so resume/retry does not lose new-item provenance.
            ids=self.store.task(identity)['new_ids'][:3]
            if ids:
                await self.generate(identity,{**options.get('generation',{}),'ids':ids,'mode':'single'})

    async def generate(self,identity,options):
        ids=options.get('ids',[])
        groups=[ids] if options.get('mode')=='multi' else [[i] for i in ids]
        for group in groups:
            sources=[self.store.material(i) for i in group]
            base_key=':'.join(sorted(group))
            for style in options.get('styles') or [options.get('style','口语化')]:
                key=base_key+'|'+style if options.get('styles') else base_key
                selected_options={**options,'style':style,'styles':[style]}
                for version in range(1,options.get('versions',1)+1):
                    if self.stopped(identity): return
                    if self.store.generated(identity,key,version):
                        self.store.log(identity,f'跳过已成功版本：{style} / {version}')
                        continue
                    try:
                        draft=await self.llm.generate(sources,selected_options,version)
                        self.store.save_generated(identity,key,version,draft)
                        state=self.store.task(identity)
                        self.store.update_task(identity,generated=state['generated']+1)
                        self.store.log(identity,f'草稿已保存：{style} / 版本 {version}')
                    except ValueError as exc:
                        state=self.store.task(identity)
                        self.store.update_task(identity,errors=state['errors']+1)
                        self.store.log(identity,str(exc))

    def set_schedule(self,options):
        self.store.set_setting('schedule',options)
        self.next_run=time.monotonic()+options['minutes']*60 if options.get('enabled') else None

    async def tick(self,now=None):
        now=time.monotonic() if now is None else now
        options=self.store.setting('schedule',{'enabled':False,'minutes':30})
        if not options.get('enabled') or self.next_run is None or now<self.next_run:
            return
        self.next_run=now+options['minutes']*60
        if self.paused or self.active_id or not self.queue.empty():
            return
        if options.get('collection'):
            self.submit('collect',options['collection'])

    async def clock(self):
        while True:
            await asyncio.sleep(5)
            await self.tick()
