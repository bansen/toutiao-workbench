import json
import os
import tempfile
import unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
from workbench.store import Store
from workbench.review_store import ReviewStore
from tests.test_review_metrics import row

NOW=datetime(2026,11,1,5,tzinfo=timezone(timedelta(hours=8)))

class ReviewStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.store=ReviewStore(Store(self.root/'state.db'),self.root/'reviews')
    def tearDown(self):self.tmp.cleanup()
    def claim(self,**kwargs):return self.store.claim('secret-account','2026-10-31',now=NOW,**kwargs)
    def ready(self):
        record=self.claim()
        return self.store.save_collection(record['id'],[row()],True,{'reads':True},['private warning'],now=NOW)
    def test_unique_claim_validation_and_public_redaction(self):
        a=self.claim(); b=self.claim(); v=self.claim(validation=True)
        self.assertEqual(a['id'],b['id'])
        self.assertNotEqual(a['id'],v['id'])
        ready=self.store.save_collection(a['id'],[row()],True,{'reads':True},['secret-account'],now=NOW)
        self.assertEqual(ready['state'],'ready')
        self.assertEqual(self.store.raw_rows(a['id'])[0]['platform_id'],'secret-post')
        exposed=json.dumps([ready,self.store.get('secret-account','2026-10-31'),self.store.analysis(a['id'])],ensure_ascii=False)
        for s in ('secret-account','secret-post','秘密标题','秘密正文','private.invalid'):
            self.assertNotIn(s,exposed)
        self.assertEqual(len(list((self.root/'reviews'/'reports').glob('*.md'))),1)
    def test_incomplete_collection_attention_and_finish_idempotent(self):
        a=self.claim()
        saved=self.store.save_collection(a['id'],[row()],False,{},[],now=NOW)
        self.assertEqual(saved['state'],'attention')
        with self.assertRaises(ValueError):self.store.finish(a['id'],[],[],{},now=NOW)
        self.store.fail(a['id'],'secret-account',now=NOW)
        self.assertEqual(self.claim()['state'],'collecting')
        self.store.save_collection(a['id'],[row()],True,{},[],now=NOW)
        result=self.store.finish(a['id'],['ok'],[],{'action':'none'},now=NOW)
        self.assertEqual(result['state'],'completed')
        self.assertEqual(self.store.finish(a['id'],['changed'],[],{},now=NOW),result)
        self.assertEqual(self.claim()['state'],'completed')
    def test_recover_collection_retry_and_application_needs_attention(self):
        a=self.claim();self.store.recover()
        self.assertEqual(self.store.get('secret-account','2026-10-31')['state'],'failed')
        self.assertEqual(self.claim()['id'],a['id'])
        self.store.save_collection(a['id'],[row()],True,{},[],now=NOW)
        self.assertTrue(self.store.begin_application(a['id'],now=NOW)['application_claimed'])
        self.assertFalse(self.store.begin_application(a['id'],now=NOW)['application_claimed'])
        self.store.recover()
        self.assertEqual(self.store.get('secret-account','2026-10-31')['state'],'attention')
        self.assertEqual(self.claim()['state'],'attention')
    def test_retention_raw_72h_aggregate_30d(self):
        a=self.ready();identity=a['id']
        self.store.finish(identity,[],[],{},now=NOW)
        extra=self.root/'reviews'/'screenshots';extra.mkdir(exist_ok=True);(extra/(identity+'.png')).write_bytes(b'raw')
        self.store.prune(now=NOW+timedelta(hours=72))
        self.assertEqual(self.store.raw_rows(identity),[])
        self.assertEqual(list((self.root/'reviews'/'reports').glob('*')),[])
        self.assertEqual(list(extra.glob('*')),[])
        self.assertEqual(self.store.analysis(identity)['sample_count'],1)
        self.store.prune(now=NOW+timedelta(days=30))
        self.assertIsNone(self.store.get('secret-account','2026-10-31'))
    def test_collection_cannot_overwrite_completed_review(self):
        a=self.ready(); self.store.finish(a['id'],[],[],{},now=NOW)
        with self.assertRaises(ValueError):self.store.save_collection(a['id'],[],True,{},[],now=NOW)

    def test_claim_has_single_owner_and_persists_application_intent(self):
        first=self.claim();second=self.claim()
        self.assertTrue(first['claimed'])
        self.assertFalse(second['claimed'])
        self.store.save_collection(first['id'],[row()],True,{},[],now=NOW)
        intent={'expected_version':3,'changes':{'target_length':450},'fingerprint':'opaque'}
        self.store.begin_application(first['id'],now=NOW,findings=['ok'],suggestions=[],policy_intent=intent)
        self.store.recover()
        saved=self.store.get('secret-account','2026-10-31')
        self.assertEqual(saved['policy_intent'],intent)
        self.assertEqual(self.store.finish(first['id'],['ok'],[],{'reconciled':True},now=NOW)['state'],'completed')

    def test_explicit_retry_attention_and_failed_are_claimable(self):
        record=self.claim(validation=True)
        old=self.store.save_collection(record['id'],[row()],False,{},[],now=NOW)
        retry=self.store.retry_collection(record['id'],now=NOW+timedelta(minutes=1))
        self.assertEqual(retry['state'],'failed')
        self.assertEqual(retry['report'],old['report'])
        self.assertEqual(retry['validation'],True)
        self.assertEqual(self.claim(validation=True)['state'],'collecting')
        self.store.fail(record['id'],'transient',now=NOW)
        self.assertEqual(self.store.retry_collection(record['id'],now=NOW)['state'],'failed')
        self.assertTrue(self.claim(validation=True)['claimed'])

    def test_explicit_retry_never_resets_active_or_implemented_reviews(self):
        record=self.claim()
        with self.assertRaises(ValueError):self.store.retry_collection(record['id'],now=NOW)
        self.store.save_collection(record['id'],[row()],True,{},[],now=NOW)
        with self.assertRaises(ValueError):self.store.retry_collection(record['id'],now=NOW)
        self.store.begin_application(record['id'],now=NOW,policy_intent={'expected_version':1})
        with self.assertRaises(ValueError):self.store.retry_collection(record['id'],now=NOW)
        self.store.recover()
        before=self.store.get('secret-account','2026-10-31')
        with self.assertRaises(ValueError):self.store.retry_collection(record['id'],now=NOW)
        self.assertEqual(self.store.get('secret-account','2026-10-31'),before)
        self.store.finish(record['id'],[],[],{'applied':True},now=NOW)
        before=self.store.get('secret-account','2026-10-31')
        with self.assertRaises(ValueError):self.store.retry_collection(record['id'],now=NOW)
        self.assertEqual(self.store.get('secret-account','2026-10-31'),before)

    def test_prune_legacy_probe_files_at_72h_without_touching_other_root_files(self):
        root=self.root/'reviews'
        names=('probe-old.json','probe-old.png','probe-recent.json','other.json','probe-old.txt')
        for name in names:
            path=root/name;path.write_text('private',encoding='utf-8')
            stamp=(NOW-timedelta(hours=71 if name=='probe-recent.json' else 72)).timestamp()
            os.utime(path,(stamp,stamp))
        result=self.store.prune(now=NOW)
        self.assertEqual(result['raw'],2)
        self.assertFalse((root/'probe-old.json').exists())
        self.assertFalse((root/'probe-old.png').exists())
        for name in ('probe-recent.json','other.json','probe-old.txt'):
            self.assertTrue((root/name).exists())
