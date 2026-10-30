import hashlib
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

from workbench.store import Store
from workbench.publication_ledger import PublicationLedger, normalise_body, body_hash


def draft(body='城市公园本周完成升级，新增步道和休息区域，周末向居民开放。', source='https://www.toutiao.com/article/7699775205312315954/'):
    return {'title': '公园开放', 'body': body, 'sources': [{'url': source}]}


class PublicationLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'workbench.db'
        self.store = Store(self.path)
        self.ledger = PublicationLedger(self.store)

    def reserve(self, value=None, account='account'):
        value = value or draft()
        return self.ledger.reserve(value, account, self.ledger.check_local(value, account)['history_version'])

    def test_normalise_strips_attribution_urls_and_unicode_formatting(self):
        self.assertEqual(normalise_body('ＡＢＣ， 世界！\u200b\n资料来源：人民日报\nhttps://example.com/a'), 'abc世界')
        self.assertEqual(normalise_body('来源：同一个媒体'), '')

    def test_reservation_is_immutable_persistent_and_account_isolated(self):
        value = draft()
        row = self.reserve(value)
        value['body'] = '修改'
        value['sources'][0]['url'] = 'https://example.com/changed'
        reopened = PublicationLedger(Store(self.path))
        self.assertEqual(reopened.get(row['id'])['body'], draft()['body'])
        self.assertEqual(reopened.records('account'), [row])
        self.assertEqual(reopened.check_local(draft(), 'other')['decision'], 'clear')
        result = reopened.check_local(draft(), 'account')
        self.assertEqual(result['decision'], 'duplicate')
        self.assertEqual(result['body_hash'], hashlib.sha256(normalise_body(draft()['body']).encode()).hexdigest())
        self.assertEqual(body_hash(draft()['body']), result['body_hash'])
        self.assertEqual(result['matches'][0]['id'], row['id'])

    def test_source_aliases_and_changed_title_are_permanent_duplicates(self):
        self.reserve()
        other = draft('今天另一条完全不同的正文。', 'http://toutiao.com/a7699775205312315954/?utm_source=feed#comment')
        self.assertEqual(self.ledger.check_local(other, 'account')['decision'], 'duplicate')

    def test_exact_and_near_text_with_different_sources(self):
        self.reserve()
        value = draft(source='https://www.toutiao.com/article/7699775205312315000/')
        value['body'] = draft()['body'].replace('，', '！') + '\n来源：新媒体'
        self.assertEqual(self.ledger.check_local(value, 'account')['decision'], 'duplicate')
        value['body'] = draft()['body'].replace('本周', '昨日')
        self.assertEqual(self.ledger.check_local(value, 'account')['decision'], 'duplicate')
        value['body'] = '海洋考察队完成深海勘测，采集珍贵生物样本，并公开全部研究资料。\n来源：同一个媒体'
        self.assertEqual(self.ledger.check_local(value, 'account')['decision'], 'clear')

    def test_invalid_body_sources_and_account_require_review(self):
        for value in [draft(''), draft('来源：报纸'), draft('x' * 5001), draft(source='javascript:bad'), draft(source='https://example.com/unsupported'), {**draft(), 'sources': []}, {**draft(), 'sources': [None]}, {**draft(), 'body': None}]:
            with self.subTest(value=value):
                self.assertEqual(self.ledger.check_local(value, 'account')['decision'], 'review')
                with self.assertRaises(ValueError):
                    self.ledger.reserve(value, 'account', self.ledger.check_local(value, 'account')['history_version'])
        self.assertEqual(self.ledger.check_local(draft(), '')['decision'], 'review')

    def test_history_versions_and_atomic_concurrent_reservation(self):
        value = draft()
        version = self.ledger.check_local(value, 'account')['history_version']
        barrier = Barrier(2)
        def attempt(_):
            barrier.wait()
            try:
                return self.ledger.reserve(value, 'account', version)['id']
            except ValueError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, range(2)))
        self.assertEqual(sum(x is not None for x in results), 1)
        self.assertNotEqual(self.ledger.check_local(value, 'account')['history_version'], version)
        with self.assertRaises(ValueError):
            self.ledger.reserve(draft('不相同的正文', 'https://example.com/else'), 'account', version)

    def test_recovery_is_idempotent_and_uncertain_remains_blocking(self):
        row = self.reserve()
        self.ledger.update(row['id'], 'submitting')
        self.ledger.recover()
        recovered = self.ledger.get(row['id'])
        self.assertEqual(recovered['status'], 'uncertain')
        self.ledger.recover()
        self.assertEqual(self.ledger.get(row['id']), recovered)
        self.assertEqual(self.ledger.check_local(draft(), 'account')['decision'], 'duplicate')
        with self.assertRaises(ValueError):
            self.ledger.update(row['id'], 'failed_before_submit')
        with self.assertRaises(ValueError):
            self.ledger.update(row['id'], 'bogus')

    def test_verified_receipt_import_is_idempotent_and_preserves_original_body(self):
        value = draft()
        receipt = {'status': 'published', 'content_id': '7699775205312315999', 'url': 'https://www.toutiao.com/a7699775205312315999/'}
        row = self.ledger.record_verified(value, 'account', receipt)
        repeated = self.ledger.record_verified(value, 'account', {**receipt, 'url': 'https://www.toutiao.com/article/7699775205312315999/?x=1'})
        self.assertEqual(row, repeated)
        changed = draft('完全不同的正文')
        with self.assertRaises(ValueError):
            self.ledger.record_verified(changed, 'account', receipt)
        self.assertEqual(self.ledger.get(row['id'])['body'], value['body'])
        self.ledger.record_verified(value, 'account', {**receipt, 'content_id': '7699775205312315888', 'url': 'https://www.toutiao.com/article/7699775205312315888/'})
        self.assertEqual(len(self.ledger.records('account')), 2)
        self.assertEqual(self.ledger.update(row['id'], 'published', receipt)['status'], 'published')

    def test_submission_states_cannot_release_duplicate_protection(self):
        for status in ['submitting', 'submitted', 'published', 'uncertain', 'rejected']:
            with self.subTest(status=status):
                row = self.reserve(account=status)
                self.ledger.update(row['id'], status)
                for target in ['cancelled', 'failed_before_submit']:
                    with self.assertRaises(ValueError):
                        self.ledger.update(row['id'], target)
                self.assertEqual(self.ledger.check_local(draft(), status)['decision'], 'duplicate')
                if status == 'published':
                    for target in ['reserved', 'submitting', 'submitted', 'uncertain', 'rejected']:
                        with self.assertRaises(ValueError):
                            self.ledger.update(row['id'], target)
        for status in ['cancelled', 'failed_before_submit']:
            row = self.reserve(account=status)
            self.ledger.update(row['id'], status)
            self.assertEqual(self.ledger.check_local(draft(), status)['decision'], 'clear')
        row = self.reserve(account='reconcile')
        self.ledger.update(row['id'], 'uncertain')
        for status in ['submitted', 'rejected', 'published']:
            self.assertEqual(self.ledger.update(row['id'], status)['status'], status)

    def test_published_receipt_metadata_cannot_replace_post_identity(self):
        receipt = {'status': 'published', 'content_id': '7699775205312315999',
                   'url': 'https://www.toutiao.com/article/7699775205312315999/'}
        row = self.ledger.record_verified(draft(), 'account', receipt)
        updated = self.ledger.update(row['id'], 'published', {'verified_at': 'today'})
        self.assertEqual(updated['receipt']['content_id'], receipt['content_id'])
        self.assertEqual(updated['receipt']['verified_at'], 'today')
        with self.assertRaises(ValueError):
            self.ledger.update(row['id'], 'published', {'content_id': '7699775205312315000'})

    def test_verified_receipt_rejects_mismatched_url_identity(self):
        with self.assertRaises(ValueError):
            self.ledger.record_verified(draft(), 'account', {
                'status': 'published', 'content_id': '7699775205312315888',
                'url': 'https://www.toutiao.com/article/7699775205312315999/'})

    def test_entire_history_and_rejected_records_still_block(self):
        first = self.reserve()
        for number in range(105):
            self.ledger.record_verified(draft(), 'other', {
                'status': 'published', 'content_id': str(7699775205312300000 + number)})
        self.ledger.update(first['id'], 'rejected')
        self.assertEqual(self.ledger.check_local(draft(), 'account')['decision'], 'duplicate')
        self.assertEqual(len(self.ledger.records('other')), 105)
        with self.assertRaises(ValueError):
            self.ledger.update(first['id'], 'cancelled')
        row = self.reserve(draft('一段独立的新事件正文。', 'https://www.toutiao.com/article/7699775205312315000/'))
        self.ledger.recover()
        self.assertEqual(self.ledger.get(row['id'])['status'], 'uncertain')

    def test_migration_backup_is_private_pre_schema_and_once(self):
        backups = list((self.path.parent / 'backups').glob('*.db'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)
        with sqlite3.connect(backups[0]) as db:
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='publication_history'").fetchone())
        PublicationLedger(self.store)
        self.assertEqual(list((self.path.parent / 'backups').glob('*.db')), backups)
