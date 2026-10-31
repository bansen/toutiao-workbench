"""Mandatory boundary for future publishers; no browser submission implementation."""
import hashlib
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .history_retention import recent_history
from .browser import NeedsAttention
from .publishing_checks import check_publish_declarations
from .post_layout import external_link_issues
from .draft_review import draft_hash


def digest(body):
    return hashlib.sha256(body.encode()).hexdigest()


class PublicationGuard:
    def __init__(self, ledger, judge, clock=None, ready=None, expected_ai=None):
        self.ledger, self.judge = ledger, judge
        self.ready = ready or (lambda account: True)
        self.expected_ai = expected_ai or (lambda: True)
        self.clock = clock or (lambda: datetime.now(ZoneInfo('Asia/Shanghai')))

    async def inspect(self, draft, account_id):
        draft = json.loads(json.dumps(draft, ensure_ascii=False))
        result = self.ledger.check_local(draft, account_id)
        if not self.ready(account_id):
            return {**result, 'decision': 'review', 'reasons': ['发布账号或历史回执尚未核实，未放行发送']}
        if result['decision'] != 'clear':
            return result
        link_issues = external_link_issues(draft.get('body',''))
        if link_issues:
            return {**result, 'decision':'review', 'reasons':link_issues}
        if draft.get('verify'):
            return {**result, 'decision': 'review', 'reasons': ['仍有待核实事项，请先人工处理']}
        cached=draft.get('publication_approval') or {}
        try:
            age=self.clock()-datetime.fromisoformat(cached['at'])
            if (cached.get('version')==1 and cached.get('account')==account_id
                    and cached.get('history_version')==result['history_version']
                    and cached.get('draft_hash')==draft_hash(draft) and timedelta(0)<=age<=timedelta(hours=3)):
                return {**result,'approval':cached}
        except (KeyError,ValueError,TypeError):pass
        records = self.ledger.records(account_id)
        if self.ledger._version(records) != result['history_version']:
            return {**result, 'decision': 'review', 'retryable':True, 'reasons': ['发布历史已变化，请重新查重']}
        records = recent_history(records, self.clock())
        batches, batch, size = [], [], 0
        for record in records:
            item = {k: record.get(k) for k in ('id', 'title', 'body', 'created_at')}
            length = len(json.dumps(item, ensure_ascii=False))
            if batch and (len(batch) >= 20 or size + length > 24000):
                batches.append(batch)
                batch, size = [], 0
            batch.append(item)
            size += length
        if batch:
            batches.append(batch)
        for batch in batches:
            try:
                verdict = await self.judge(draft, batch)
                decision = verdict['decision']
                if decision == 'duplicate':
                    record = next(r for r in records if r['id'] == verdict['matched_id'])
                    return {**result, 'decision': 'duplicate', 'reasons': ['同一事件已有发布记录'],
                            'matches': [{'id': record['id'], 'title': record.get('title', ''),
                                         'url': (record.get('receipt') or {}).get('url', ''),
                                         'reason': '模型判定为同一事件'}]}
                if decision != 'distinct':
                    return {**result, 'decision': 'review', 'reasons': ['事件是否重复尚不明确，须人工核实']}
            except Exception:
                return {**result, 'decision': 'review', 'retryable':True, 'reasons': ['事件查重服务不可用，未放行发送']}
        if self.ledger.check_local(draft, account_id)['history_version'] != result['history_version']:
            return {**result, 'decision': 'review', 'retryable':True, 'reasons': ['发布历史已变化，请重新查重']}
        return {**result,'approval':{'version':1,'account':account_id,'at':self.clock().isoformat(),
                    'draft_hash':draft_hash(draft),'history_version':result['history_version']}}

    def review_current(self,draft,account_id,history_version):
        cached=draft.get('publication_approval') or {}
        try:
            return bool(cached.get('version')==1 and cached.get('account')==account_id
                and cached.get('history_version')==history_version and cached.get('draft_hash')==draft_hash(draft)
                and timedelta(0)<=self.clock()-datetime.fromisoformat(cached['at'])<=timedelta(hours=3))
        except (KeyError,ValueError,TypeError):return False

    def check_time(self):
        now = self.clock()
        if now.tzinfo is None or not 8 <= now.astimezone(ZoneInfo('Asia/Shanghai')).hour < 22:
            raise NeedsAttention('仅允许北京时间08:00至22:00提交，不补发错过时段')

    async def send(self, candidate, account_id, confirmed_sha256, preflight, submit):
        frozen = json.loads(json.dumps(candidate, ensure_ascii=False))
        body = frozen.get('body', '')
        link_issues = external_link_issues(body)
        if link_issues:
            raise NeedsAttention('；'.join(link_issues))
        if digest(body) != confirmed_sha256:
            raise NeedsAttention('正文与确认快照不一致，请重新确认')
        self.check_time()
        result = await self.inspect(frozen, account_id)
        if result['decision'] != 'clear':
            raise NeedsAttention('；'.join(result['reasons']))
        try:
            record = self.ledger.reserve(frozen, account_id, result['history_version'])
        except ValueError as exc:
            raise NeedsAttention(str(exc)) from None
        identity = record['id']
        try:
            checks = await preflight(json.loads(json.dumps(frozen)))
            if checks.get('account_id') != account_id or checks.get('body_sha256') != confirmed_sha256:
                raise NeedsAttention('发布账号或编辑器正文与确认快照不一致')
            check_publish_declarations(checks.get('exclusive') or {}, checks.get('ai') or {},expected_ai=self.expected_ai())
            self.check_time()
            if not self.ready(account_id):
                raise NeedsAttention('发布账号或历史回执尚未核实，未继续提交')
        except BaseException:
            self.ledger.update(identity, 'failed_before_submit')
            raise
        self.ledger.update(identity, 'submitting')
        try:
            receipt = await submit(json.loads(json.dumps(frozen)))
            status = receipt.get('status')
            if status not in {'submitted', 'published', 'uncertain', 'rejected'}:
                raise NeedsAttention('提交回执无法确认，禁止重发')
            if status == 'published':
                from .publication_ledger import _canonical_source
                url = _canonical_source(receipt.get('url'))
                if url != f"https://www.toutiao.com/w/{receipt.get('content_id')}/":
                    raise NeedsAttention('发布链接与内容ID不一致，须核查')
            return self.ledger.update(identity, status, receipt)
        except BaseException:
            self.ledger.update(identity, 'uncertain')
            raise
