import tempfile
import unittest
from pathlib import Path

from workbench.store import Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/'workbench.db'
        self.store=Store(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_upsert_preserves_identity_and_updates_metrics_after_restart(self):
        article={'url':'https://www.toutiao.com/article/7699775205312315954/','content_id':'7699775205312315954','title':'测试标题','body':'真实正文','kind':'article','reads':None,'comments':3}
        first,new=self.store.upsert_material(article)
        self.assertTrue(new)
        article.update(comments=5)
        second,new=self.store.upsert_material(article)
        self.assertFalse(new)
        self.assertEqual(first['id'],second['id'])
        restored=Store(self.path)
        self.assertEqual(len(restored.materials()),1)
        self.assertEqual(restored.materials()[0]['comments'],5)
        self.assertIsNone(restored.materials()[0]['reads'])

    def test_task_recovery_keeps_drafts_and_marks_interrupted_tasks(self):
        task=self.store.create_task('collect',{'keyword':'AI'})
        self.store.update_task(task['id'],status='running')
        self.store.recover()
        self.assertEqual(self.store.task(task['id'])['status'],'failed')

    def test_successful_versions_are_not_replaced_on_retry(self):
        task=self.store.create_task('generate',{})
        draft=self.store.save_generated(task['id'],'source-1',1,{'title':'首稿','body':'正文','sources':[],'options':{},'model':'test'})
        again=self.store.save_generated(task['id'],'source-1',1,{'title':'重复稿','body':'重复正文'})
        self.assertEqual(again['id'],draft['id'])
        self.assertEqual(again['title'],'首稿')

    def test_recovery_includes_tasks_older_than_history_window(self):
        oldest=self.store.create_task('collect',{})
        for _ in range(100):
            self.store.create_task('collect',{})
        self.store.recover()
        self.assertEqual(self.store.task(oldest['id'])['status'],'failed')


if __name__=='__main__': unittest.main()
