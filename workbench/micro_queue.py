"""Durable immutable micro-post snapshots and fail-closed publication states.

This module performs no network operations. A possible submission is never
returned to a publishable state, including after an interrupted process.
"""
import hashlib
import json
import sqlite3
import uuid
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone

from .parsing import now_iso


_TRANSITIONS = {
    'awaiting_confirmation': {'pending', 'cancelled', 'skipped', 'failed'},
    'pending': {'publishing', 'cancelled', 'skipped', 'failed'},
    'publishing': {'submitted', 'published', 'rejected', 'uncertain'},
    'submitted': {'published', 'rejected', 'uncertain'},
    'uncertain': {'submitted', 'published', 'rejected'},
    'published': set(), 'rejected': set(), 'failed': set(),
    'skipped': set(), 'cancelled': set(),
}
_SNAPSHOT_FIELDS = ('title', 'body', 'sources', 'verify', 'draft_id')
_UPDATE_FIELDS = {'status', 'reason', 'receipt', 'publication_id', 'confirmed_sha256'}


class MicroQueue:
    def __init__(self, store):
        self.store = store
        with store.connection() as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='micro_publish_queue'").fetchone()
        if not exists:
            directory = store.path.parent / 'backups'
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            name = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:8] + '.db'
            path = directory / name
            path.touch(mode=0o600, exist_ok=False)
            with store.connection() as source, closing(sqlite3.connect(path)) as target:
                source.backup(target)
            with store.connection() as db:
                db.execute('CREATE TABLE IF NOT EXISTS micro_publish_queue (id TEXT PRIMARY KEY, slot_id TEXT UNIQUE NOT NULL, payload TEXT NOT NULL)')

    def items(self, limit=None):
        with self.store.connection() as db:
            rows = [json.loads(row[0]) for row in db.execute('SELECT payload FROM micro_publish_queue')]
        rows.sort(key=lambda row: (row['scheduled_at'], row['id']))
        return rows if limit is None else rows[:limit]

    def get(self, identity):
        with self.store.connection() as db:
            row = db.execute('SELECT payload FROM micro_publish_queue WHERE id=?', (identity,)).fetchone()
        if row is None:
            raise KeyError('微头条队列记录不存在')
        return json.loads(row[0])

    def add(self, slot, draft, topic, event_key, status='pending'):
        if status not in ('pending', 'awaiting_confirmation'):
            raise ValueError('新队列记录必须等待发布或确认')
        snapshot = {key: deepcopy(draft.get(key)) for key in ('title', 'body', 'sources', 'verify')}
        snapshot['draft_id'] = draft['id']
        if not isinstance(snapshot['body'], str) or not snapshot['body'].strip():
            raise ValueError('发布正文不能为空')
        row = {
            **snapshot, 'id': uuid.uuid4().hex, 'slot_id': slot['id'],
            'scheduled_at': slot['scheduled_at'], 'topic': deepcopy(topic),
            'event_key': deepcopy(event_key), 'status': status,
            'body_sha256': hashlib.sha256(snapshot['body'].encode('utf-8')).hexdigest(),
            'created_at': now_iso(),
        }
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT payload FROM micro_publish_queue WHERE slot_id=?', (slot['id'],)).fetchone()
            if old:
                old = json.loads(old[0])
                if any(old[key] != snapshot[key] for key in _SNAPSHOT_FIELDS):
                    raise ValueError('排期已关联其他草稿或正文快照')
                return old
            db.execute('INSERT INTO micro_publish_queue VALUES(?,?,?)', (row['id'], row['slot_id'], self.store.dump(row)))
        return row

    def update(self, identity, changes, expected_statuses=None):
        if set(changes) - _UPDATE_FIELDS:
            raise ValueError('不能修改发布正文快照或队列标识')
        changes = deepcopy(changes)
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            record = db.execute('SELECT payload FROM micro_publish_queue WHERE id=?', (identity,)).fetchone()
            if record is None:
                raise KeyError('微头条队列记录不存在')
            row = json.loads(record[0])
            status = row['status']
            if expected_statuses is not None and status not in expected_statuses:
                raise ValueError('队列状态已变化')
            next_status = changes.get('status', status)
            if status == 'publishing' and changes.get('status') == 'publishing':
                raise ValueError('发布已领取，禁止重复发布')
            if next_status != status and next_status not in _TRANSITIONS[status]:
                raise ValueError('不允许此队列状态转换')
            if 'confirmed_sha256' in changes:
                if status not in ('pending', 'awaiting_confirmation') or changes['confirmed_sha256'] != row['body_sha256']:
                    raise ValueError('只能确认当前待发布正文的准确哈希')
            if status == 'awaiting_confirmation' and next_status == 'pending':
                if changes.get('confirmed_sha256', row.get('confirmed_sha256')) != row['body_sha256']:
                    raise ValueError('发布前必须确认正文哈希')
            row.update(changes, updated_at=now_iso())
            db.execute('UPDATE micro_publish_queue SET payload=? WHERE id=?', (self.store.dump(row), identity))
        return row

    def recover(self):
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            for record in db.execute('SELECT id,payload FROM micro_publish_queue').fetchall():
                row = json.loads(record['payload'])
                if row['status'] == 'publishing':
                    row.update(status='uncertain', reason='程序退出时发布未完成，必须核实平台回执；禁止自动重发。', updated_at=now_iso())
                    db.execute('UPDATE micro_publish_queue SET payload=? WHERE id=?', (self.store.dump(row), record['id']))

    def history(self):
        return [row for row in self.items() if row['status'] not in ('cancelled', 'skipped', 'failed')]
