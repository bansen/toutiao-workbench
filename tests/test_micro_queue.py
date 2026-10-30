import hashlib
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from workbench.store import Store


class MicroQueueTests(unittest.TestCase):
    def setUp(self):
        from workbench.micro_queue import MicroQueue
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / 'workbench.db')
        self.store.set_setting('preserved', {'value': 1})
        self.queue = MicroQueue(self.store)
        self.slot = {'id': '2026-10-30:01', 'scheduled_at': '2026-10-30T08:10:00+08:00', 'status': 'pending'}
        self.draft = {'id': 'draft-1', 'title': '标题', 'body': '原文\n正文 ', 'sources': [{'url': 'https://example.test/a'}], 'verify': ['核实']}

    def add(self, **kwargs):
        return self.queue.add(self.slot, self.draft, {'name': 'topic'}, 'event-1', **kwargs)

    def test_backup_is_private_online_and_created_once(self):
        from workbench.micro_queue import MicroQueue
        backups = list((Path(self.tmp.name) / 'backups').glob('*.db'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)
        with sqlite3.connect(backups[0]) as db:
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='micro_publish_queue'").fetchone())
        self.assertEqual(Store(backups[0]).setting('preserved'), {'value': 1})
        MicroQueue(self.store)
        self.assertEqual(len(list((Path(self.tmp.name) / 'backups').glob('*.db'))), 1)

    def test_snapshot_and_return_values_are_isolated_and_survive_restart(self):
        from workbench.micro_queue import MicroQueue
        row = self.add()
        expected_hash = hashlib.sha256(self.draft['body'].encode()).hexdigest()
        self.assertEqual(row['body_sha256'], expected_hash)
        self.assertEqual(row['draft_id'], 'draft-1')
        self.draft['sources'][0]['url'] = 'changed'
        row['body'] = 'changed'
        row['sources'].clear()
        loaded = MicroQueue(Store(self.store.path)).get(row['id'])
        self.assertEqual(loaded['body'], '原文\n正文 ')
        self.assertEqual(loaded['sources'], [{'url': 'https://example.test/a'}])
        self.assertEqual(loaded['body_sha256'], expected_hash)

    def test_duplicate_slot_is_idempotent_but_rejects_changed_snapshot(self):
        row = self.add()
        self.assertEqual(self.add()['id'], row['id'])
        for field, value in [('body', 'different'), ('id', 'draft-2'), ('sources', []), ('title', 'changed'), ('verify', [])]:
            changed = {**self.draft, field: value}
            with self.assertRaises(ValueError):
                self.queue.add(self.slot, changed, {}, 'event-1')
        self.assertEqual(len(self.queue.items()), 1)

    def test_concurrent_duplicate_slot_has_one_row(self):
        with ThreadPoolExecutor(max_workers=6) as executor:
            rows = list(executor.map(lambda _: self.add(), range(12)))
        self.assertEqual(len({r['id'] for r in rows}), 1)
        self.assertEqual(len(self.queue.items()), 1)

    def test_items_are_scheduled_order_with_limit_and_history_exclusions(self):
        first = self.add()
        for n, status in [(2, 'cancelled'), (3, 'skipped'), (4, 'failed'), (5, 'published')]:
            slot = {**self.slot, 'id': f'slot-{n}', 'scheduled_at': f'2026-10-30T0{n}:00:00+08:00'}
            row = self.queue.add(slot, self.draft, {}, f'event-{n}')
            if status == 'published':
                self.queue.update(row['id'], {'status': 'publishing'})
            self.queue.update(row['id'], {'status': status})
        self.assertEqual([r['slot_id'] for r in self.queue.items(2)], ['slot-2', 'slot-3'])
        self.assertEqual({r['id'] for r in self.queue.history()}, {first['id'], self.queue.items()[-2]['id']})

    def test_update_is_conditional_and_preserves_immutable_fields(self):
        row = self.add()
        for field in ['body', 'draft_id', 'title', 'sources', 'verify', 'body_sha256', 'slot_id', 'scheduled_at', 'id', 'event_key', 'topic']:
            with self.assertRaises(ValueError):
                self.queue.update(row['id'], {field: 'changed'})
        with self.assertRaises(ValueError):
            self.queue.update(row['id'], {'status': 'publishing'}, expected_statuses=['awaiting_confirmation'])
        updated = self.queue.update(row['id'], {'status': 'publishing'}, expected_statuses=['pending'])
        self.assertEqual(updated['status'], 'publishing')
        with self.assertRaises(ValueError):
            self.queue.update(row['id'], {'status': 'publishing'}, expected_statuses=['pending'])

    def test_confirmation_requires_exact_snapshot_hash(self):
        row = self.add(status='awaiting_confirmation')
        with self.assertRaises(ValueError):
            self.queue.update(row['id'], {'status': 'pending'})
        with self.assertRaises(ValueError):
            self.queue.update(row['id'], {'status': 'pending', 'confirmed_sha256': 'bad'})
        confirmed = self.queue.update(row['id'], {'status': 'pending', 'confirmed_sha256': row['body_sha256']})
        self.assertEqual(confirmed['confirmed_sha256'], row['body_sha256'])
        with self.assertRaises(ValueError):
            self.queue.update(row['id'], {'confirmed_sha256': 'bad'})

    def test_possible_submission_cannot_be_cancelled_or_requeued(self):
        for state in ['publishing', 'submitted', 'published', 'rejected', 'uncertain']:
            slot = {**self.slot, 'id': state}
            row = self.queue.add(slot, self.draft, {}, state)
            self.queue.update(row['id'], {'status': 'publishing'})
            if state != 'publishing':
                self.queue.update(row['id'], {'status': state})
            for forbidden in ['pending', 'awaiting_confirmation', 'cancelled', 'skipped', 'failed']:
                with self.assertRaises(ValueError):
                    self.queue.update(row['id'], {'status': forbidden})
            with self.assertRaises(ValueError):
                self.queue.update(row['id'], {'confirmed_sha256': row['body_sha256']})

    def test_terminal_states_cannot_resurrect_and_new_status_is_restricted(self):
        for state in ['cancelled', 'skipped', 'failed']:
            row = self.queue.add({**self.slot, 'id': state}, self.draft, {}, state)
            self.queue.update(row['id'], {'status': state})
            with self.assertRaises(ValueError):
                self.queue.update(row['id'], {'status': 'pending'})
        with self.assertRaises(ValueError):
            self.queue.add({**self.slot, 'id': 'bad'}, self.draft, {}, 'bad', status='published')

    def test_restart_recovery_only_marks_publishing_uncertain(self):
        from workbench.micro_queue import MicroQueue
        row = self.add()
        self.queue.update(row['id'], {'status': 'publishing'})
        pending = self.queue.add({**self.slot, 'id': 'pending'}, self.draft, {}, 'other')
        reopened = MicroQueue(Store(self.store.path))
        reopened.recover()
        reopened.recover()
        self.assertEqual(reopened.get(row['id'])['status'], 'uncertain')
        self.assertEqual(reopened.get(pending['id'])['status'], 'pending')
        with self.assertRaises(ValueError):
            reopened.update(row['id'], {'status': 'publishing'})
        submitted = reopened.update(row['id'], {'status': 'submitted', 'receipt': {'ok': True}})
        published = reopened.update(row['id'], {'status': 'published', 'publication_id': 'remote-1'})
        self.assertEqual(submitted['receipt'], {'ok': True})
        self.assertEqual(published['publication_id'], 'remote-1')

    def test_atomic_claim_only_one_concurrent_publisher(self):
        row = self.add()
        def claim(_):
            try:
                self.queue.update(row['id'], {'status': 'publishing'}, expected_statuses=['pending'])
                return True
            except ValueError:
                return False
        with ThreadPoolExecutor(max_workers=6) as executor:
            self.assertEqual(sum(executor.map(claim, range(12))), 1)

    def test_publishing_cannot_be_claimed_twice_even_without_expected_statuses(self):
        row = self.add()
        self.queue.update(row['id'], {'status': 'publishing'})
        with self.assertRaises(ValueError):
            self.queue.update(row['id'], {'status': 'publishing'})
        self.assertEqual(self.queue.get(row['id'])['status'], 'publishing')

    def test_bad_change_is_atomic_and_missing_rows_are_errors(self):
        row = self.add()
        with self.assertRaises(ValueError):
            self.queue.update(row['id'], {'reason': 'must not persist', 'status': 'invented'})
        self.assertNotIn('reason', self.queue.get(row['id']))
        with self.assertRaises(KeyError):
            self.queue.get('missing')
        with self.assertRaises(KeyError):
            self.queue.update('missing', {'status': 'cancelled'})
        with self.assertRaises(ValueError):
            self.queue.update(row['id'], {'confirmed_sha256': 'bad'})
