import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from workbench.store import Store
from workbench.publication_ledger import PublicationLedger
from workbench.publication_guard import PublicationGuard
from workbench.micro_queue import MicroQueue

NOW = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)


class RetentionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / 'test.db')
        self.ledger = PublicationLedger(self.store)
        self.queue = MicroQueue(self.store)

    def age(self, table, record, age):
        record['created_at'] = (NOW - age).isoformat()
        record['updated_at'] = NOW.isoformat()
        with self.store.connection() as db:
            db.execute(f'UPDATE {table} SET payload=? WHERE id=?', (self.store.dump(record), record['id']))
        return record

    def published(self):
        return self.ledger.record_verified(
            {'body': '旧事件的完整报道正文', 'sources': [{'url': 'https://www.toutiao.com/article/7701979050666902079/'}],
             'source_snapshots': [{'body': '源文正文'}]}, 'account',
            {'status': 'published', 'content_id': '1880433446879241', 'url': 'https://www.toutiao.com/w/1880433446879241/'})

    async def test_model_history_excludes_old_even_before_cleanup(self):
        self.age('publication_history', self.published(), timedelta(hours=72, seconds=1))
        calls = []
        async def judge(candidate, history):
            calls.append(history)
            return {'decision': 'uncertain'}
        guard = PublicationGuard(self.ledger, judge, clock=lambda: NOW)
        candidate = {'body': '完全不同的新事件正文', 'sources': [{'url': 'https://www.toutiao.com/article/7701979050666902080/'}]}
        self.assertEqual((await guard.inspect(candidate, 'account'))['decision'], 'clear')
        self.assertEqual(calls, [])

    def test_boundary_timezone_and_unknown_dates(self):
        from workbench.history_retention import recent_history
        rows = [
            {'id': 'boundary', 'created_at': (NOW - timedelta(hours=72)).astimezone(timezone(timedelta(hours=8))).isoformat()},
            {'id': 'old', 'created_at': (NOW - timedelta(hours=72, microseconds=1)).isoformat()},
            {'id': 'unknown', 'created_at': 'bad'},
            {'id': 'naive', 'created_at': '2020-01-01T00:00:00'},
        ]
        self.assertEqual([r['id'] for r in recent_history(rows, NOW)], ['boundary', 'unknown', 'naive'])

    def test_compaction_keeps_permanent_dedup_and_receipt(self):
        from workbench.history_retention import compact_history
        row = self.age('publication_history', self.published(), timedelta(days=4))
        compact_history(self.store, 'publication_history', NOW)
        loaded = self.ledger.get(row['id'])
        self.assertTrue(loaded['history_compacted'])
        self.assertNotIn('source_snapshots', loaded)
        self.assertEqual(loaded.get('body', ''), '')
        self.assertEqual(loaded['receipt'], row['receipt'])
        self.assertEqual(self.ledger.check_local({'body': row['body'], 'sources': [{'url': 'https://www.toutiao.com/article/7701979050666902080/'}]}, 'account')['decision'], 'duplicate')
        self.assertEqual(self.ledger.check_local({'body': '不同内容', 'sources': row['sources']}, 'account')['decision'], 'duplicate')
        self.assertEqual(self.ledger.check_local({'body': '新事件', 'sources': [{'url': 'https://www.toutiao.com/article/7701979050666902080/'}]}, 'account')['decision'], 'clear')
        compact_history(self.store, 'publication_history', NOW)
        self.assertEqual(self.ledger.get(row['id']), loaded)

    def test_unresolved_queue_and_ledger_keep_reconciliation_body(self):
        from workbench.history_retention import compact_history, recent_history
        row = self.ledger.reserve({'body': '未确认的提交正文', 'sources': [{'url': 'https://www.toutiao.com/article/7701979050666902080/'}]}, 'account', self.ledger._version([]))
        self.ledger.update(row['id'], 'uncertain')
        self.age('publication_history', self.ledger.get(row['id']), timedelta(days=5))
        q = self.queue.add({'id': 'old', 'scheduled_at': '2026-10-25T12:00:00Z'}, {'id': 'draft', 'body': '待核查正文', 'sources': [], 'verify': []}, 'AI', 'event')
        self.queue.update(q['id'], {'status': 'publishing'})
        self.queue.update(q['id'], {'status': 'uncertain'})
        self.age('micro_publish_queue', self.queue.get(q['id']), timedelta(days=5))
        for table in ('publication_history', 'micro_publish_queue'):
            compact_history(self.store, table, NOW)
        self.assertEqual(self.ledger.get(row['id'])['body'], '未确认的提交正文')
        self.assertEqual(self.queue.get(q['id'])['body'], '待核查正文')
        self.assertEqual(recent_history(self.queue.history(), NOW), [])

    def test_terminal_queue_compacts_but_recent_body_remains(self):
        from workbench.history_retention import compact_history
        for index, age in enumerate((timedelta(days=4), timedelta(hours=72))):
            row = self.queue.add({'id': str(index), 'scheduled_at': NOW.isoformat()}, {'id': 'draft', 'body': '正文', 'sources': [], 'verify': []}, '科技', 'event')
            self.queue.update(row['id'], {'status': 'skipped'})
            self.age('micro_publish_queue', self.queue.get(row['id']), age)
        compact_history(self.store, 'micro_publish_queue', NOW)
        rows = self.queue.items()
        self.assertEqual(sum(bool(r.get('body')) for r in rows), 1)
        self.assertTrue(all(r['body_sha256'] for r in rows))
