"""Durable micro-post coordinator. All platform effects cross the injected guard."""
import asyncio
import hashlib
import uuid
from collections import Counter
from datetime import datetime, timedelta

from .history_retention import compact_history, recent_history
from .browser import NeedsAttention
from .micro_content import MicroContent, TOPICS
from .micro_plan_store import MicroPlanStore
from .micro_queue import MicroQueue
from .micro_schedule import TIMEZONE
from .publication_ledger import body_hash, _canonical_source

POSSIBLE = {'reserved', 'submitting', 'submitted', 'published', 'uncertain', 'rejected'}
DEFAULT_KEYWORDS = {'民生': ['民生'], 'AI': ['人工智能'], '科技': ['科技']}


def _time(value):
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return result.astimezone(TIMEZONE) if result.tzinfo else None
    except (ValueError, TypeError, AttributeError):
        return None


class MicroRuntime:
    def __init__(self, store, browser, llm, guard, publisher, clock=None):
        self.store, self.browser, self.llm = store, browser, llm
        self.guard, self.publisher = guard, publisher
        self.clock = clock or (lambda: datetime.now(TIMEZONE))
        self.plans, self.queue, self.content = MicroPlanStore(store), MicroQueue(store), MicroContent(llm)
        self._loop_task = self._prepare_task = None
        self._tick_lock, self._prepare_lock = asyncio.Lock(), asyncio.Lock()
        self._closed, self._running, self._last_tick, self._last_prepare = False, False, None, None
        self._last_retention = None
        self._active_slot = None
        self.publisher.allowed = self._allowed
        if not hasattr(browser, 'publishing_idle'):
            browser.publishing_idle = asyncio.Event()
            browser.publishing_idle.set()

    def _now(self, now=None):
        value = now or self.clock()
        if value.tzinfo is None:
            raise ValueError('排期时间必须包含时区')
        return value.astimezone(TIMEZONE)

    def _account(self):
        return (self.store.setting('publishing_account', {}) or {}).get('id')

    def _enabled(self):
        return (self.store.setting('publishing', {'enabled': True}) or {}).get('enabled', True) is True

    def _model_ready(self):
        try:
            return self.llm.status().get('configured') is True
        except (AttributeError, TypeError):
            return False

    def _ready(self):
        verified = self.store.setting('publishing_verification', {}) or {}
        return bool(self._model_ready() and self._account() and verified.get('account_id') == self._account()
                    and verified.get('declaration_verified') is True
                    and verified.get('ai_declaration', False) == (self.store.setting('publishing', {}) or {}).get('ai_declaration', False)
                    and not self.store.setting('publication_import_error', False))

    def _paused(self):
        return bool(self.store.setting('publishing_pause', {}).get('paused'))

    def _log(self, reason):
        logs = self.store.setting('publishing_logs', [])
        self.store.set_setting('publishing_logs', (logs + [{'at': self._now().isoformat(), 'message': reason}])[-100:])

    def _pause_revision(self):
        return int((self.store.setting('publishing_pause', {}) or {}).get('revision', 0))

    def pause(self, reason):
        revision = self._pause_revision() + 1
        self.store.set_setting('publishing_pause', {'paused': True, 'reason': str(reason)[:300], 'revision': revision})
        self._log(str(reason)[:300])

    async def verify(self):
        async with self._tick_lock:
            return await self._verify()

    async def _verify(self):
        revision, account = self._pause_revision(), self._account()
        expected_ai = (self.store.setting('publishing', {}) or {}).get('ai_declaration', False)
        self.store.set_setting('publishing_verification', {})
        try:
            result = await self.publisher.probe()
            if (self._pause_revision() != revision or self._account() != account
                    or (self.store.setting('publishing', {}) or {}).get('ai_declaration', False) != expected_ai):
                raise NeedsAttention('校验期间暂停、账号或声明设置已变化，请重新校验')
            if not isinstance(result, dict) or result.get('account_id') != account or result.get('declaration_verified') is not True or result.get('ai_declaration', False) != expected_ai:
                raise NeedsAttention('账号或声明校验未通过')
            self.store.set_setting('publishing_verification', {**result, 'verified_at': self._now().isoformat()})
            return result
        except Exception:
            self.store.set_setting('publishing_verification', {})
            if self._pause_revision() == revision:
                self.pause('发布校验未通过，请检查登录、账号和声明状态')
            raise
        finally:
            await self.publisher.close()

    async def resume(self):
        revision = self._pause_revision()
        result = await self.verify()
        if self._pause_revision() != revision:
            return result
        if any(r['status'] == 'uncertain' for r in self.queue.items()):
            self.pause('仍有待核实的发布回执')
            return result
        self.store.set_setting('publishing_pause', {'paused': False, 'reason': '', 'revision': revision})
        return result

    async def start(self):
        if self._loop_task and not self._loop_task.done():
            return
        self._closed = False
        self.queue.recover()
        ledger = getattr(self.guard, 'ledger', None)
        if ledger:
            ledger.recover()
        now = self._now()
        self.plans.get_or_create(now)
        for row in self.queue.items():
            if row['status'] == 'uncertain':
                changes = {'status': 'uncertain'}
                matches = [r for r in self._records() if r.get('body_hash') == body_hash(row['body'])]
                if matches:
                    changes['publication_id'] = matches[-1]['id']
                    self.queue.update(row['id'], {'publication_id': matches[-1]['id']})
                self.plans.update_slot(row['slot_id'], changes)
                self.pause('上次发布回执不明确，禁止自动重发')
            elif row['status'] in {'submitted', 'published', 'rejected'}:
                changes = {key: row[key] for key in ('status', 'publication_id', 'receipt', 'reason') if key in row}
                self.plans.update_slot(row['slot_id'], changes)
            elif row['status'] in {'cancelled', 'skipped', 'failed'}:
                saved_plan = self.plans.load(_time(row['scheduled_at']).date())
                saved_slot = next((s for s in (saved_plan or {}).get('slots', []) if s['id'] == row['slot_id']), None)
                if saved_slot and saved_slot['status'] in {'failed', 'skipped', 'cancelled', 'completed', 'published', 'rejected'}:
                    continue
                self.plans.update_slot(row['slot_id'], {'status': 'failed' if row['status'] == 'failed' else 'skipped',
                                                       'reason': row.get('reason', '队列稿件已停止发布')})
            elif row['status'] in {'pending', 'awaiting_confirmation'} and _time(row['scheduled_at']) < now:
                self._skip(row['slot_id'], row, '重启后不补发错过排期')
        self._retain_history(now)
        self._last_tick = now
        self._loop_task = asyncio.create_task(self._loop())

    async def close(self):
        self._closed = True
        cleanup = asyncio.create_task(self._close())
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await cleanup
            raise

    async def _close(self):
        tasks = [task for task in (self._loop_task, self._prepare_task) if task and task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        async with self._tick_lock:
            await self.publisher.close()
            self.browser.publishing_idle.set()
            self._running = False

    async def _loop(self):
        while not self._closed:
            try:
                await self.tick()
                now = self._now()
                if self._last_retention is None or now - self._last_retention >= timedelta(hours=1):
                    async with self._tick_lock:
                        self._retain_history(now)
                if self._eligible(now) and (self._last_prepare is None or now - self._last_prepare >= timedelta(hours=1)) and not (self._prepare_task and not self._prepare_task.done()):
                    self._last_prepare = now
                    self._prepare_task = asyncio.create_task(self.prepare())
            except asyncio.CancelledError:
                raise
            except Exception:
                self.pause('自动发布运行异常，请人工检查')
            await asyncio.sleep(2)

    def _retain_history(self, now):
        if getattr(self.guard, 'ledger', None) is not None:
            compact_history(self.store, 'publication_history', now)
        compact_history(self.store, 'micro_publish_queue', now)
        self._last_retention = now

    def _eligible(self, now):
        return not self._closed and self._enabled() and self._ready() and not self._paused() and 8 <= now.hour < 22

    def _records(self):
        ledger = getattr(self.guard, 'ledger', None)
        return ledger.records(self._account()) if ledger and self._account() else []

    def _within_limits(self, now, target):
        records = [r for r in self._records() if r.get('status') in POSSIBLE]
        today = [r for r in records if (stamp := _time(r.get('created_at'))) and stamp.date() == now.date()]
        count = len(today)
        # Optional verified platform/manual totals are additive only when explicit.
        external = self.store.setting('publishing_daily_count', {}) or {}
        if isinstance(external, dict) and external.get('date') == now.date().isoformat():
            count = max(count, int(external.get('count', 0)))
        if count >= target:
            return False
        for row in records:
            stamp = _time(row.get('updated_at')) or _time(row.get('created_at'))
            if stamp and timedelta(0) <= now - stamp < timedelta(minutes=15):
                return False
        return True

    def _allowed(self, *args, **kwargs):
        now = self._now()
        if not self._active_slot or not self._eligible(now):
            return False
        slot = self._active_slot
        if self._account() != self._active_account:
            return False
        try:
            current_draft = self.store.draft(self._active_row['draft_id'])
        except KeyError:
            return False
        if any(current_draft.get(k) != self._active_row.get(k) for k in ('title', 'body', 'verify', 'sources')):
            return False
        plan = self.plans.load(now.date())
        if not plan or not timedelta(0) <= now - _time(slot['scheduled_at']) <= timedelta(seconds=60):
            return False
        # The current reservation is not a previous actual submission.
        records = self._records()
        current = [r for r in records if r.get('body_hash') == self._active_hash and r.get('status') in {'reserved', 'submitting'}]
        prior = [r for r in records if r not in current]
        count = sum(1 for r in prior if (stamp := _time(r.get('created_at'))) and stamp.date() == now.date())
        external = self.store.setting('publishing_daily_count', {}) or {}
        if isinstance(external, dict) and external.get('date') == now.date().isoformat():
            count = max(count, int(external.get('count', 0)))
        if count >= plan['target']:
            return False
        return not any((stamp := (_time(r.get('updated_at')) or _time(r.get('created_at')))) and timedelta(0) <= now-stamp < timedelta(minutes=15) for r in prior)

    def status(self):
        now = self._now()
        plan = self.plans.load(now.date()) or {'target': 0, 'slots': []}
        rows = self.queue.items()
        pause = self.store.setting('publishing_pause', {})
        future = [s['scheduled_at'] for s in plan['slots'] if s['status'] in {'pending', 'queued', 'awaiting_confirmation'} and _time(s['scheduled_at']) >= now]
        ready, enabled = self._ready(), self._enabled()
        return {'enabled': enabled, 'ready': ready, 'running': self._running,
                'state': 'paused' if pause.get('paused') else ('disabled' if not enabled else (('ready' if 8 <= now.hour < 22 else 'idle') if ready else 'unverified')),
                'reason': pause.get('reason', '') if pause.get('paused') else (('' if 8 <= now.hour < 22 else '等待北京时间08:00发布窗口') if ready else ('模型配置缺失，请先配置模型' if not self._model_ready() else '请先核实发布账号与声明')),
                'paused': bool(pause.get('paused')), 'today': plan,
                'service_active': bool(self._loop_task and not self._loop_task.done()),
                'counts': dict(Counter(r['status'] for r in rows if (_time(r['scheduled_at']) or now).date() == now.date())),
                'next_at': min(future, default=None), 'preparing': self._prepare_lock.locked()}

    def _skip(self, slot_id, row, reason):
        if row and row.get('reason') and row['reason'] not in reason:
            reason += '；原检查：' + row['reason']
        if row and row['status'] in {'pending', 'awaiting_confirmation'}:
            self.queue.update(row['id'], {'status': 'skipped', 'reason': reason})
        self.plans.update_slot(slot_id, {'status': 'skipped', 'reason': reason})
        self._log('跳过排期：' + reason)

    async def _browser_step(self, method, *args):
        while True:
            await self.browser.publishing_idle.wait()
            async with self.browser.lock:
                if self.browser.publishing_idle.is_set():
                    return await method(*args)

    async def _refresh(self, draft, priority=False):
        refreshed = []
        for source in draft.get('sources') or []:
            if priority:
                async with self.browser.lock:
                    row = await self.browser.detail(source['url'], source.get('kind', 'micro' if '/w/' in source['url'] else 'article'))
            else:
                row = await self._browser_step(self.browser.detail, source['url'], source.get('kind', 'micro' if '/w/' in source['url'] else 'article'))
            if not isinstance(row, dict) or not row.get('body', row.get('content', '')).strip():
                raise ValueError('来源正文不可见')
            refreshed.append(row)
        return refreshed

    async def tick(self, now=None):
        async with self._tick_lock:
            now = self._now(now)
            plan = self.plans.load(now.date())
            if plan is None:
                plan = self.plans.get_or_create(now)
            gap = self._last_tick is not None and now - self._last_tick > timedelta(seconds=60)
            self._last_tick = now
            rows = {r['slot_id']: r for r in self.queue.items()}
            for row in rows.values():
                if row['status'] in {'pending', 'awaiting_confirmation'} and _time(row['scheduled_at']).date() < now.date():
                    self._skip(row['slot_id'], row, '跨日后不补发过期排期')
            for slot in plan['slots']:
                if slot['status'] not in {'pending', 'queued', 'awaiting_confirmation'}:
                    continue
                due = _time(slot['scheduled_at'])
                if due > now:
                    continue
                row = rows.get(slot['id'])
                if gap or now - due > timedelta(seconds=60):
                    self._skip(slot['id'], row, '错过排期，不补发')
                    continue
                if not self._eligible(now):
                    continue
                if not row or row['status'] != 'pending':
                    self._skip(slot['id'], row, '当前排期没有通过检查的稿件')
                    continue
                if not self._within_limits(now, plan['target']):
                    self._skip(slot['id'], row, '达到每日上限或不足15分钟间隔')
                    continue
                await self._publish(slot, row)

    async def _publish(self, slot, row):
        self.browser.publishing_idle.clear()
        self._running = True
        claimed = False
        submit_started = False
        stage = '来源检查'
        try:
            draft = self.store.draft(row['draft_id'])
            if any(draft.get(k) != row.get(k) for k in ('title', 'body', 'verify', 'sources')):
                self.queue.update(row['id'], {'status': 'cancelled', 'reason': '草稿已编辑，旧快照作废'})
                self.plans.update_slot(slot['id'], {'status': 'skipped', 'reason': '草稿已编辑'})
                return
            sources = await self._refresh(draft, priority=True)
            check = await self.content.check({**row, 'source_snapshots': draft.get('source_snapshots', []), 'editorial_review':draft.get('editorial_review', [])}, sources, row['topic'])
            if check.get('passed') is not True:
                issues = [v for v in check.get('issues', []) if isinstance(v, str) and v.strip()]
                reason = '发布前来源检查未通过：' + ('；'.join(issues) or '检查结果不可用')
                self.queue.update(row['id'], {'status': 'failed', 'reason': reason, 'prepublication_check': check})
                self.plans.update_slot(slot['id'], {'status': 'skipped', 'reason': reason})
                self._log(reason)
                return
            self._active_slot, self._active_hash = slot, body_hash(row['body'])
            self._active_account, self._active_row = self._account(), row
            if not self._allowed():
                self._skip(slot['id'], row, '发布条件已变化')
                return
            self.queue.update(row['id'], {'status': 'publishing', 'confirmed_sha256': row['body_sha256']}, expected_statuses=['pending'])
            claimed = True
            self.plans.update_slot(slot['id'], {'status': 'publishing'})
            stage = '发布历史查重'
            async def preflight(snapshot):
                nonlocal stage
                stage = '编辑器与声明检查'
                if not self._allowed():
                    raise NeedsAttention('发布条件已变化')
                checks = await self.publisher.preflight(snapshot)
                if not self._allowed():
                    raise NeedsAttention('发布条件已变化')
                return checks
            async def submit(snapshot):
                nonlocal submit_started, stage
                if not self._allowed():
                    raise NeedsAttention('发布条件已变化')
                submit_started = True
                stage = '提交与回执核查'
                return await self.publisher.submit(snapshot)
            record = await self.guard.send(row, self._account(), row['body_sha256'], preflight, submit)
            receipt = record.get('receipt', record)
            status = record.get('status', receipt.get('status', 'uncertain'))
            if status not in {'submitted', 'published', 'rejected', 'uncertain'}:
                status = 'uncertain'
            changes = {'status': status, 'receipt': receipt}
            if record.get('id'):
                changes['publication_id'] = record['id']
            self.queue.update(row['id'], changes)
            self.plans.update_slot(slot['id'], changes)
            self._postprocess_receipt(receipt, status)
            self._log('提交结果：' + status)
        except BaseException as exc:
            if claimed:
                ledger = getattr(self.guard, 'ledger', None)
                matching = [r for r in (ledger.records(self._account(), include_inactive=True) if ledger else [])
                            if r.get('body_hash') == body_hash(row['body'])]
                record = matching[-1] if matching else None
                status = 'uncertain' if submit_started else 'failed'
                # Only fixed application errors are safe to expose; arbitrary
                # HTTP/model/browser exception text can contain credentials.
                detail = str(exc)[:240] if isinstance(exc, NeedsAttention) else type(exc).__name__
                reason = f'{stage}失败：{detail}'
                if submit_started:
                    reason = '发布结果待核实，禁止重发；' + reason
                else:
                    reason = '提交前失败，未发送；' + reason
                changes = {'status': status, 'reason': reason}
                if record:
                    changes['publication_id'] = record['id']
                self.queue.update(row['id'], changes, before_submit=not submit_started)
                self.plans.update_slot(slot['id'], changes, before_submit=not submit_started)
                self._log(reason)
                if submit_started or isinstance(exc, NeedsAttention):
                    self.pause(reason)
            else:
                current = self.queue.get(row['id'])
                if current['status'] == 'pending':
                    self.queue.update(row['id'], {'status': 'failed', 'reason': '来源或模型检查不可用'})
                    self.plans.update_slot(slot['id'], {'status': 'skipped', 'reason': '来源或模型检查不可用'})
                if isinstance(exc, NeedsAttention):
                    self.pause('来源访问需要人工处理登录、验证码或限制')
                self._log('发布前检查失败，未发送')
            if isinstance(exc, asyncio.CancelledError):
                raise
        finally:
            self._active_slot = None
            try:
                await self.publisher.close()
            finally:
                self.browser.publishing_idle.set()
                self._running = False

    def _postprocess_receipt(self, receipt, status):
        platform_count = receipt.get('visible_today_count')
        if isinstance(platform_count, int) and not isinstance(platform_count, bool) and platform_count >= 0:
            day = self._now().date().isoformat()
            old_count = self.store.setting('publishing_daily_count', {}) or {}
            old = old_count.get('count', 0) if old_count.get('date') == day else 0
            self.store.set_setting('publishing_daily_count', {'date': day, 'count': max(platform_count, old)})
        if receipt.get('unexpected_ai_tag'):
            self.pause('平台公开AI声明与预期不一致，请人工核实')
        if receipt.get('unexpected_exclusive_tag'):
            self.pause('平台出现意外首发标签，请人工核实并联系平台处理')
        if status == 'uncertain':
            self.pause('发布回执不明确，禁止自动重发')

    async def stage(self, draft_id, topic, sha):
        async with self._tick_lock:
            now = self._now()
            draft = self.store.draft(draft_id)
            if topic not in TOPICS or not self._ready() or self._closed:
                raise ValueError('选题无效或发布服务尚未就绪')
            if hashlib.sha256(draft.get('body', '').encode()).hexdigest() != sha or draft.get('verify'):
                raise ValueError('正文哈希不一致或仍有待核实事项')
            plan = self.plans.load(now.date()) or self.plans.get_or_create(now)
            occupied = {row['slot_id'] for row in self.queue.items()}
            slots = [slot for slot in plan['slots'] if slot['status'] == 'pending' and slot['id'] not in occupied
                     and now < _time(slot['scheduled_at']) <= now + timedelta(hours=1)]
            if not slots:
                raise ValueError('未来一小时内没有空闲排期')
            slot = min(slots, key=lambda value: value['scheduled_at'])
            account = self._account()
            refreshed = await self._refresh(draft)
            result = await self.content.check(draft, refreshed, topic)
            if result.get('passed') is not True:
                issues = [v for v in result.get('issues', []) if isinstance(v, str)]
                raise ValueError('来源或事实检查未通过：' + '；'.join(issues)[:1000])
            verdict = await self.guard.inspect(draft, account)
            if verdict.get('decision') != 'clear':
                raise ValueError('发布历史查重未通过：' + '；'.join(verdict.get('reasons') or ['事件是否重复尚不明确'])[:1000])
            history = self.queue.history()
            origins = {_canonical_source(source['url']) for source in draft.get('sources', [])}
            if not origins or any(origins & {_canonical_source(source['url']) for source in row.get('sources', [])} for row in history):
                raise ValueError('已有队列稿件使用相同来源')
            history = recent_history(history, self._now())
            for offset in range(0, len(history), 20):
                duplicate = await self.llm.judge_duplicate(draft, history[offset:offset+20])
                if duplicate.get('decision') != 'distinct':
                    raise ValueError('事件重复或无法确定，未排期')
            current = self.store.draft(draft_id)
            if any(current.get(key) != draft.get(key) for key in ('title', 'body', 'sources', 'verify', 'source_snapshots')):
                raise ValueError('检查期间草稿已变化，请重新确认')
            if not self._ready() or self._account() != account or _time(slot['scheduled_at']) <= self._now() or self._closed:
                raise ValueError('发布条件已变化，请重新检查')
            # Preparation can occupy a future slot while the model runs.
            current_plan = self.plans.load(now.date())
            current_slot = next(value for value in current_plan['slots'] if value['id'] == slot['id'])
            if current_slot['status'] != 'pending' or any(row['slot_id'] == slot['id'] for row in self.queue.items()):
                raise ValueError('排期已被占用，请重试')
            event_key = hashlib.sha256('\n'.join(sorted(origins)).encode()).hexdigest()
            row = self.queue.add(slot, current, topic, event_key)
            self.queue.update(row['id'], {'confirmed_sha256': sha})
            self.plans.update_slot(slot['id'], {'status': 'queued', 'queue_id': row['id'], 'draft_id': draft_id, 'topic': topic, 'event_key': event_key})
            return self.queue.get(row['id'])

    async def confirm(self, identity, sha):
        async with self._tick_lock:
            row = self.queue.get(identity)
            now = self._now()
            scheduled = _time(row['scheduled_at'])
            if row['status'] != 'awaiting_confirmation' or sha != row['body_sha256']:
                raise ValueError('只能确认等待人工检查的准确正文快照')
            if not self._ready() or not scheduled or scheduled <= now or scheduled.date() != now.date() or not 8 <= scheduled.hour < 22:
                raise ValueError('账号尚未核实或排期已过期')
            draft = self.store.draft(row['draft_id'])
            if any(draft.get(k) != row.get(k) for k in ('title', 'body', 'sources', 'verify')) or draft.get('verify'):
                raise ValueError('草稿已编辑或仍有待核实事项，旧快照不可确认')
            refreshed = await self._refresh(draft)
            result = await self.content.check({**row, 'source_snapshots': draft.get('source_snapshots', []), 'editorial_review':draft.get('editorial_review', [])}, refreshed, row['topic'])
            verdict = await self.guard.inspect(row, self._account())
            if result.get('passed') is not True or verdict.get('decision') != 'clear':
                raise ValueError('来源、事实或事件查重检查未通过')
            # Model/browser calls may consume the remaining time or settings may change.
            current = self.store.draft(row['draft_id'])
            if scheduled <= self._now() or not self._ready() or any(current.get(k) != row.get(k) for k in ('title', 'body', 'sources', 'verify')):
                raise ValueError('发布条件已变化，请重新检查')
            confirmed = self.queue.update(identity, {'status': 'pending', 'confirmed_sha256': sha, 'reason': ''}, expected_statuses=['awaiting_confirmation'])
            self.plans.update_slot(row['slot_id'], {'status': 'queued', 'reason': ''})
            return confirmed

    async def reject(self, identity):
        async with self._tick_lock:
            row = self.queue.get(identity)
            if row['status'] not in {'pending', 'awaiting_confirmation'}:
                raise ValueError('可能已经提交的稿件不能拒绝或重新发布，请核实回执')
            result = self.queue.update(identity, {'status': 'cancelled', 'reason': '人工拒绝发布'}, expected_statuses=['pending', 'awaiting_confirmation'])
            self.plans.update_slot(row['slot_id'], {'status': 'skipped', 'reason': '人工拒绝发布'})
            return result

    async def reconcile(self, identity):
        async with self._tick_lock:
            return await self._reconcile(identity)

    async def _reconcile(self, identity):
        row = self.queue.get(identity)
        if row['status'] not in {'uncertain', 'submitted'}:
            raise ValueError('只允许核实待确认回执')
        try:
            receipt = await self.publisher.reconcile(row)
            if receipt.get('status') not in {'submitted', 'published', 'rejected', 'uncertain'}:
                raise ValueError('回执无效')
            changes = {'status': receipt['status'], 'receipt': receipt}
            if receipt['status'] in {'published', 'submitted', 'rejected'}:
                changes['reason'] = receipt.get('platform_status', '') if receipt['status'] == 'rejected' else ''
            ledger = getattr(self.guard, 'ledger', None)
            if ledger and row.get('publication_id'):
                ledger.update(row['publication_id'], receipt['status'], receipt)
            self.queue.update(identity, changes)
            self.plans.update_slot(row['slot_id'], changes)
            self._postprocess_receipt(receipt, receipt['status'])
            self._log('核查回执结果：' + receipt['status'])
            return self.queue.get(identity)
        finally:
            await self.publisher.close()

    def _save_review_draft(self, draft, reasons):
        payload = {**draft, 'verify': list(dict.fromkeys([*(draft.get('verify') or []), *reasons]))}
        return self.store.save_generated(uuid.uuid4().hex, uuid.uuid4().hex, 1, payload)

    async def prepare(self, now=None):
        if self._prepare_lock.locked():
            return
        async with self._prepare_lock:
            now = self._now(now)
            if not self._eligible(now):
                return
            plan = self.plans.load(now.date()) or self.plans.get_or_create(now)
            slots = [s for s in plan['slots'] if s['status'] == 'pending' and now < _time(s['scheduled_at']) <= now+timedelta(hours=1)]
            if not slots:
                return
            seen = set(self.store.setting('micro_seen_sources', []))
            for row in self.queue.history()+self._records():
                seen.update(s.get('url') for s in row.get('sources', []))
            keywords = self.store.setting('publishing_keywords', DEFAULT_KEYWORDS) or DEFAULT_KEYWORDS
            candidates = []
            discovered = {url for url in seen if isinstance(url,str)}
            self._log('开始本轮自动备稿：民生 / AI / 科技，总候选额度30')
            try:
                for topic in TOPICS:
                    for kind in ('article', 'micro'):
                        words = keywords.get(topic) or DEFAULT_KEYWORDS[topic]
                        # Each topic/kind has a total budget of five, even with multiple keywords.
                        for index, word in enumerate(words[:5]):
                            budget = 5//len(words[:5]) + (index < 5 % len(words[:5]))
                            try:
                                found = await self._browser_step(self.browser.search, word, kind, budget, sorted(discovered), now-timedelta(hours=24))
                            except ValueError:
                                self._log('此组合未取得结果，继续搜索：' + topic + ' / ' + kind)
                                continue
                            if not found:
                                self._log('搜索无新候选：' + topic + ' / ' + kind)
                            for card in found[:budget]:
                                try:discovery_url=_canonical_source(card.get('url'))
                                except ValueError:continue
                                if discovery_url in discovered:
                                    self._log('跳过搜索重复链接：来源已处理或本轮已发现')
                                    continue
                                discovered.add(discovery_url)
                                stamp = _time(card.get('published_at'))
                                if stamp is None or timedelta(0) <= now-stamp <= timedelta(hours=24):
                                    candidates.append((topic, kind, card))
                                    if stamp is None:
                                        self._log('列表时间未知，将在详情页核实')
                                else:
                                    self._log('跳过候选：发布时间缺失、过期或在未来')
                self._log('发现候选：' + str(len(candidates)) + ' 条，列表时间未知的将核实详情')
                candidates.sort(key=lambda v: (v[2].get('comments') or 0, v[2].get('likes') or 0, _time(v[2].get('published_at')) or datetime.min.replace(tzinfo=TIMEZONE)), reverse=True)
                for topic, kind, card in candidates[:30]:
                    if not slots or not self._eligible(self._now()):
                        break
                    try:
                        url = _canonical_source(card['url'])
                        if url in seen:
                            self._log('跳过候选：来源已处理或已有队列/发布记录')
                            continue
                        seen.add(url)
                        self.store.set_setting('micro_seen_sources', sorted(seen))
                        item = await self._browser_step(self.browser.detail, url, kind)
                        for key in ('published_at', 'author', 'comments', 'likes'):
                            if item.get(key) in (None, ''):
                                item[key] = card.get(key)
                        stamp = _time(item.get('published_at'))
                        if not stamp or not timedelta(0) <= now-stamp <= timedelta(hours=24):
                            self._log('跳过候选：来源时间缺失、过期或在未来')
                            continue
                        if not item.get('body', item.get('content', '')).strip():
                            self._log('跳过候选：来源正文缺失或不可见')
                            continue
                        item['metrics_source'] = card.get('search_url', url)
                        material, _ = self.store.upsert_material(item)
                        draft = await self.content.generate([material], topic)
                        draft['source_snapshots'] = [material]
                        verdict = await self.guard.inspect(draft, self._account())
                        if verdict.get('decision') != 'clear':
                            reasons = verdict.get('reasons')
                            reasons = [value for value in reasons if isinstance(value, str) and value.strip()] if isinstance(reasons, list) else []
                            reasons = reasons or ['发布历史查重未通过，请人工核实']
                            reason_text = '；'.join(' '.join(value.split())[:120] for value in reasons[:3])
                            if verdict.get('decision') == 'duplicate':
                                self._log('跳过重复事件：' + reason_text)
                            else:
                                self._save_review_draft(draft, reasons)
                                self._log('已保存待人工处理稿：' + reason_text)
                            continue
                        distinct = True
                        history = recent_history(self.queue.history(), self._now())
                        for offset in range(0, len(history), 20):
                            result = await self.llm.judge_duplicate(draft, history[offset:offset+20])
                            if result.get('decision') != 'distinct':
                                distinct = False
                                if result.get('decision') == 'duplicate':
                                    self._log('跳过候选：与队列中的稿件属于同一事件')
                                else:
                                    self._save_review_draft(draft, ['与队列稿件是否属于同一事件尚不明确，请人工核实'])
                                    self._log('已保存待人工处理稿：队列事件查重尚不明确')
                                break
                        if not distinct:
                            continue
                        refreshed = await self._refresh(draft)
                        check = await self.content.check(draft, refreshed, topic)
                        if check.get('passed') is not True:
                            issues = check.get('issues') or ['稿件质量检查未通过']
                            try:
                                revised = await self.content.repair(draft, refreshed, topic, issues)
                                revised_topic = (revised.get('options') or {}).get('topic')
                                if revised_topic not in TOPICS:
                                    raise ValueError('删改后的选题无效')
                                draft, topic = revised, revised_topic
                                check = await self.content.check(draft, refreshed, topic)
                                if check.get('passed') is True:
                                    verdict = await self.guard.inspect(draft, self._account())
                                    if verdict.get('decision') != 'clear':
                                        check = {'passed':False,'issues':verdict.get('reasons') or ['删改稿发布查重未通过']}
                                    history = recent_history(self.queue.history(), self._now())
                                    for offset in range(0, len(history), 20):
                                        if check.get('passed') is not True:
                                            break
                                        duplicate = await self.llm.judge_duplicate(draft, history[offset:offset+20])
                                        if duplicate.get('decision') != 'distinct':
                                            check = {'passed':False,'issues':['删改稿与队列重复或无法确定']}
                                self._log('已完成一次质量删改并重新检查：' + topic)
                            except Exception:
                                check = {'passed':False,'issues':issues}
                                self._log('质量删改不可用，保留原问题待处理')
                        if check.get('passed') is not True:
                            issues = check.get('issues') or ['稿件质量检查未通过']
                            draft['verify'] = list(dict.fromkeys([*(draft.get('verify') or []), *issues]))
                            self._log('模型QA未通过：' + '；'.join(issues)[:500])
                        saved = self.store.save_generated(uuid.uuid4().hex, uuid.uuid4().hex, 1, draft)
                        # Model work can outlive a slot while tick() expires it.
                        # Keep the draft, then use only still-free future slots.
                        current_now = self._now()
                        current_plan = self.plans.load(current_now.date()) or {}
                        free = {s['id'] for s in current_plan.get('slots', []) if s['status'] == 'pending'}
                        occupied = {r['slot_id'] for r in self.queue.items()}
                        slots = [s for s in slots if s['id'] in free and s['id'] not in occupied
                                 and _time(s['scheduled_at']) > current_now]
                        if not slots or not self._eligible(current_now):
                            self._log('草稿已保存，本轮没有可用的未来排期，不补发')
                            break
                        slot = slots.pop(0)
                        event_key = hashlib.sha256('\n'.join(sorted(_canonical_source(s['url']) for s in draft['sources'])).encode()).hexdigest()
                        status = 'pending' if check.get('passed') is True else 'awaiting_confirmation'
                        row = self.queue.add(slot, saved, topic, event_key, status=status)
                        if status == 'awaiting_confirmation':
                            row = self.queue.update(row['id'], {'reason': '；'.join(str(v) for v in check.get('issues', ['检查未通过']))[:1000]})
                        self.plans.update_slot(slot['id'], {'status': 'queued' if status == 'pending' else status, 'queue_id': row['id'], 'draft_id': saved['id'], 'topic': topic, 'event_key': event_key})
                        self._log(('成功入队：' if status == 'pending' else '等待人工确认：') + topic)
                    except NeedsAttention:
                        self.pause('素材访问需要人工处理登录、验证码或限制')
                        break
                    except Exception:
                        self._log('候选稿检查失败，未进入自动发布')
            except NeedsAttention:
                self.pause('素材搜索需要人工处理登录、验证码或限制')
            except Exception:
                self._log('自动备稿失败，未放行发布')
            finally:
                self._log('本轮完成：自动备稿检查结束')
