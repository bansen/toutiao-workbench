"""Durable publication reservations. No function here sends a platform request.

Near duplicates use character trigram Dice similarity >= 0.82, with a minimum
of 20 normalized characters and length ratio >= 0.82 to avoid short-text noise.
Input is bounded to 5,000 characters; source and exact-body matches never expire.
"""
import hashlib
import json
import re
import sqlite3
import unicodedata
import uuid
from contextlib import closing
from datetime import datetime, timezone
from urllib.parse import urlsplit

from .parsing import canonical_url, now_iso

ACTIVE_STATUSES = frozenset({'reserved', 'submitting', 'submitted', 'published', 'uncertain', 'rejected'})
VALID_STATUSES = ACTIVE_STATUSES | {'cancelled', 'failed_before_submit'}
# Once a submission may have happened, no path may return to a releasable state.
_ALLOWED_TRANSITIONS = {
    'reserved': VALID_STATUSES,
    'submitting': {'submitting', 'submitted', 'published', 'uncertain', 'rejected'},
    'submitted': {'submitted', 'published', 'uncertain', 'rejected'},
    'uncertain': {'uncertain', 'submitted', 'published', 'rejected'},
    'rejected': {'rejected', 'submitted', 'published', 'uncertain'},
    'published': {'published'},
    'cancelled': {'cancelled'},
    'failed_before_submit': {'failed_before_submit'},
}
MAX_BODY_LENGTH = 5000
NEAR_DUPLICATE_THRESHOLD = 0.82


def normalise_body(body):
    if not isinstance(body, str):
        return ''
    lines = []
    for line in unicodedata.normalize('NFKC', body).splitlines():
        line = line.strip()
        if re.match(r'^(?:资料来源|来源)\s*[:：]', line) or re.fullmatch(r'https?://\S+', line, re.I):
            continue
        lines.append(line)
    return ''.join(c for c in '\n'.join(lines).lower()
                   if not c.isspace() and not unicodedata.category(c).startswith('P')
                   and unicodedata.category(c) != 'Cf')


def body_hash(body):
    return hashlib.sha256(normalise_body(body).encode('utf-8')).hexdigest()


def _canonical_source(url):
    if not isinstance(url, str):
        raise ValueError('来源链接无效')
    url = url.strip()
    p = urlsplit(url)
    if p.scheme.lower() not in ('http', 'https') or not p.hostname or p.username or p.password or any(c.isspace() for c in url):
        raise ValueError('来源链接无效')
    if p.hostname.lower() in {'toutiao.com', 'www.toutiao.com', 'so.toutiao.com', 'sou.toutiao.com'}:
        return canonical_url(url)
    from .ai_tools import canonical_official_url
    return canonical_official_url(url)


def _sources(draft):
    sources = draft.get('sources')
    if not isinstance(sources, list) or not sources:
        raise ValueError('缺少有效来源链接，须人工核实')
    result = []
    for source in sources:
        if not isinstance(source, dict):
            raise ValueError('来源链接无效，须人工核实')
        result.append({**source, 'url': _canonical_source(source.get('url'))})
    return result


def _near(first, second):
    if min(len(first), len(second)) < 20 or min(len(first), len(second)) / max(len(first), len(second)) < NEAR_DUPLICATE_THRESHOLD:
        return False
    a = {first[i:i + 3] for i in range(len(first) - 2)}
    b = {second[i:i + 3] for i in range(len(second) - 2)}
    return 2 * len(a & b) / (len(a) + len(b)) >= NEAR_DUPLICATE_THRESHOLD


class PublicationLedger:
    def __init__(self, store):
        self.store = store
        with store.connection() as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='publication_history'").fetchone()
        if not exists:
            directory = store.path.parent / 'backups'
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            path = directory / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-publication-' + uuid.uuid4().hex[:8] + '.db')
            path.touch(mode=0o600, exist_ok=False)
            path.chmod(0o600)
            with store.connection() as source, closing(sqlite3.connect(path)) as target:
                source.backup(target)
            with store.connection() as db:
                db.execute('CREATE TABLE IF NOT EXISTS publication_history (id TEXT PRIMARY KEY, account_id TEXT NOT NULL, payload TEXT NOT NULL)')
                db.execute('CREATE INDEX IF NOT EXISTS publication_history_account ON publication_history(account_id)')

    def _records(self, db, account_id, include_inactive=False):
        rows = db.execute('SELECT payload FROM publication_history WHERE account_id=? ORDER BY rowid', (account_id,))
        return [record for row in rows if (record := json.loads(row[0]))['status'] in ACTIVE_STATUSES or include_inactive]

    def records(self, account_id, include_inactive=False):
        with self.store.connection() as db:
            return self._records(db, account_id, include_inactive)

    @staticmethod
    def _version(records):
        payload = json.dumps(records, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(payload.encode()).hexdigest()

    def _check(self, draft, account_id, records):
        reasons, matches = [], []
        body = draft.get('body')
        normalized = normalise_body(body)
        if not isinstance(account_id, str) or not account_id.strip():
            reasons.append('缺少发布账号，须人工核实')
        if not normalized or not isinstance(body, str) or len(body) > MAX_BODY_LENGTH:
            reasons.append('正文为空或超过5000字，须人工核实')
        try:
            source_urls = {s['url'] for s in _sources(draft)}
        except (ValueError, TypeError):
            source_urls = set()
            reasons.append('缺少有效来源链接，须人工核实')
        if normalized and isinstance(body, str) and len(body) <= MAX_BODY_LENGTH:
            for record in records:
                reason = None
                if source_urls & {s['url'] for s in record['sources']}:
                    reason = '相同来源链接已有发布记录'
                elif body_hash(body) == record['body_hash']:
                    reason = '相同正文已有发布记录'
                elif _near(normalized, normalise_body(record['body'])):
                    reason = '高度近似正文已有发布记录（阈值0.82）'
                if reason:
                    matches.append({'id': record['id'], 'title': record.get('title', ''),
                                    'url': (record.get('receipt') or {}).get('url', ''), 'reason': reason})
        return {'decision': 'duplicate' if matches else ('review' if reasons else 'clear'),
                'reasons': reasons + list(dict.fromkeys(m['reason'] for m in matches)), 'matches': matches,
                'body_hash': body_hash(body), 'history_version': self._version(records)}

    def check_local(self, draft, account_id):
        with self.store.connection() as db:
            return self._check(draft, account_id, self._records(db, account_id))

    def _insert(self, db, draft, account_id, status, receipt=None):
        record = json.loads(self.store.dump(draft))
        record.update(id=uuid.uuid4().hex, account_id=account_id, status=status, created_at=now_iso(),
                      body_hash=body_hash(draft.get('body')), sources=_sources(draft))
        if receipt is not None:
            record['receipt'] = json.loads(self.store.dump(receipt))
        db.execute('INSERT INTO publication_history VALUES(?,?,?)', (record['id'], account_id, self.store.dump(record)))
        return record

    def reserve(self, draft, account_id, expected_version):
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            result = self._check(draft, account_id, self._records(db, account_id))
            if result['history_version'] != expected_version:
                raise ValueError('发布历史已变化，须重新检查')
            if result['decision'] != 'clear':
                raise ValueError('；'.join(result['reasons']))
            return self._insert(db, draft, account_id, 'reserved')

    def _get(self, db, identity):
        row = db.execute('SELECT payload FROM publication_history WHERE id=?', (identity,)).fetchone()
        if row is None:
            raise KeyError('发布记录不存在')
        return json.loads(row[0])

    def get(self, identity):
        with self.store.connection() as db:
            return self._get(db, identity)

    def update(self, identity, status, receipt=None):
        if status not in VALID_STATUSES:
            raise ValueError('发布状态无效')
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            record = self._get(db, identity)
            if status not in _ALLOWED_TRANSITIONS[record['status']]:
                raise ValueError('发布状态转换无效；可能已提交的记录不能解除去重保护')
            if receipt is not None:
                incoming = json.loads(self.store.dump(receipt))
                previous = record.get('receipt') or {}
                if incoming.get('url'):
                    incoming['url'] = _canonical_source(incoming['url'])
                if record['status'] == 'published':
                    for key in ('url', 'content_id'):
                        if previous.get(key) and incoming.get(key) and str(previous[key]) != str(incoming[key]):
                            raise ValueError('已发布记录不能替换发布回执身份')
                record['receipt'] = {**previous, **incoming}
            record.update(status=status, updated_at=now_iso())
            db.execute('UPDATE publication_history SET payload=? WHERE id=?', (self.store.dump(record), identity))
            return record

    def recover(self):
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            for row in db.execute('SELECT id,payload FROM publication_history').fetchall():
                record = json.loads(row['payload'])
                if record['status'] in {'reserved', 'submitting'}:
                    record.update(status='uncertain', updated_at=now_iso())
                    db.execute('UPDATE publication_history SET payload=? WHERE id=?', (self.store.dump(record), row['id']))

    def record_verified(self, draft, account_id, receipt):
        receipt = json.loads(self.store.dump(receipt))
        status = receipt.get('status', 'published')
        if status not in {'published', 'submitted', 'uncertain', 'rejected'}:
            raise ValueError('已核实发布状态无效')
        url = receipt.get('url')
        if url:
            receipt['url'] = _canonical_source(url)
        content_id = receipt.get('content_id')
        if receipt.get('url'):
            match = re.search(r'/(?:article/|w/)(\d+)/', receipt['url'])
            if match:
                if content_id and str(content_id) != match[1]:
                    raise ValueError('发布回执链接与内容ID不一致')
                receipt['content_id'] = content_id = match[1]
        if not content_id and not url:
            raise ValueError('发布回执须有内容ID或链接')
        if not account_id or not normalise_body(draft.get('body')) or len(draft['body']) > MAX_BODY_LENGTH:
            raise ValueError('账号或正文无效')
        _sources(draft)
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            for row in db.execute('SELECT payload FROM publication_history WHERE account_id=?', (account_id,)):
                record = json.loads(row[0])
                old = record.get('receipt') or {}
                if ((content_id and str(content_id) == str(old.get('content_id'))) or
                        (receipt.get('url') and receipt['url'] == old.get('url'))):
                    if record['body_hash'] != body_hash(draft['body']):
                        raise ValueError('同一发布回执对应不同正文，须人工核实')
                    return record
            return self._insert(db, draft, account_id, status, receipt)
