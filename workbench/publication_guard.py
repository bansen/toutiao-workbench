"""Mandatory boundary for future publishers; no browser submission implementation."""
import hashlib
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .browser import NeedsAttention
from .publishing_checks import check_publish_declarations
from .post_layout import external_link_issues, unsafe_markup_issues
from .draft_review import draft_hash
from .micro_schedule import TIMEZONE, PUBLISH_START_HOUR, PUBLISH_END_HOUR


def digest(body):
    return hashlib.sha256(body.encode()).hexdigest()


class PublicationGuard:
    def __init__(self, ledger, judge=None, clock=None, ready=None, expected_ai=None):
        self.ledger = ledger
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
        link_issues = external_link_issues(draft.get('body',''))+unsafe_markup_issues(draft.get('body',''))
        if link_issues:
            return {**result, 'decision':'review', 'reasons':link_issues}
        if draft.get('verify'):
            return {**result, 'decision': 'review', 'reasons': ['仍有待核实事项，请先人工处理']}
        return {**result,'approval':{'version':2,'account':account_id,'at':self.clock().isoformat(),
                    'draft_hash':draft_hash(draft)}}

    def review_current(self,draft,account_id,history_version=None):
        cached=draft.get('publication_approval') or {}
        try:
            return bool(cached.get('version')==2 and cached.get('account')==account_id
                and cached.get('draft_hash')==draft_hash(draft)
                and timedelta(0)<=self.clock()-datetime.fromisoformat(cached['at'])<=timedelta(hours=3))
        except (KeyError,ValueError,TypeError):return False

    def check_time(self):
        now = self.clock()
        if now.tzinfo is None or not PUBLISH_START_HOUR <= now.astimezone(TIMEZONE).hour < PUBLISH_END_HOUR:
            raise NeedsAttention('仅允许北京时间07:00至23:00提交')

    async def send(self, candidate, account_id, confirmed_sha256, preflight, submit):
        frozen = json.loads(json.dumps(candidate, ensure_ascii=False))
        if not isinstance(frozen.get('slot_id'),str) or not frozen['slot_id'].strip():
            raise NeedsAttention('缺少发布任务标识，不能提交')
        body = frozen.get('body', '')
        link_issues = external_link_issues(body)+unsafe_markup_issues(body)
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
