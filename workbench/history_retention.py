"""Three-day semantic history, with permanent local duplicate fingerprints.

Ambiguous timestamps are retained conservatively. Possible submissions keep
full snapshots until their platform outcome is known.
"""
import json
from datetime import datetime, timedelta, timezone

HISTORY_DAYS = 3
_TERMINAL = {'published', 'rejected', 'cancelled', 'skipped', 'failed', 'failed_before_submit'}
_FIELDS = {'id', 'account_id', 'status', 'created_at', 'updated_at', 'body_hash',
           'body_sha256', 'slot_id', 'scheduled_at', 'draft_id', 'publication_id',
           'receipt', 'topic', 'event_key', 'confirmed_sha256'}


def _expired(record, now):
    if now.tzinfo is None:
        raise ValueError('历史清理时间必须包含时区')
    try:
        created = datetime.fromisoformat(record['created_at'].replace('Z', '+00:00'))
        return created.tzinfo is not None and created < now - timedelta(days=HISTORY_DAYS)
    except (KeyError, ValueError, TypeError, AttributeError):
        return False


def recent_history(records, now=None):
    now = now or datetime.now(timezone.utc)
    return [row for row in records if not _expired(row, now)]


def compact_history(store, table, now):
    if table not in {'publication_history', 'micro_publish_queue'}:
        raise ValueError('不支持清理该数据表')
    count = 0
    with store.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        for identity, payload in db.execute(f'SELECT id,payload FROM {table}').fetchall():
            row = json.loads(payload)
            if row.get('history_compacted') or row.get('status') not in _TERMINAL or not _expired(row, now):
                continue
            compact = {key: value for key, value in row.items() if key in _FIELDS}
            compact.update(body='', sources=[{'url': source['url']} for source in row.get('sources', [])],
                           history_compacted=True)
            db.execute(f'UPDATE {table} SET payload=? WHERE id=?', (store.dump(compact), identity))
            count += 1
    return count
