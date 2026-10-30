import random
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from workbench.store import Store
from workbench.micro_plan_store import MicroPlanStore

TZ=ZoneInfo('Asia/Shanghai')


class DailyPlanStoreTests(unittest.TestCase):
    def test_plan_survives_reopen_without_new_random_draw(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'workbench.db'
            store=Store(path)
            store.set_setting('preferences',{'keyword':'保留旧设置'})
            now=datetime(2026,10,29,7,tzinfo=TZ)
            first=MicroPlanStore(store).get_or_create(now,random.Random(4))
            second=MicroPlanStore(Store(path)).get_or_create(now,random.Random(17))
            self.assertEqual(first,second)
            self.assertEqual(Store(path).setting('preferences'),{'keyword':'保留旧设置'})

    def test_midday_and_restart_skip_without_redistribution(self):
        with tempfile.TemporaryDirectory() as tmp:
            plans=MicroPlanStore(Store(Path(tmp)/'workbench.db'))
            first=plans.get_or_create(datetime(2026,10,29,12,tzinfo=TZ),random.Random(4))
            later=plans.get_or_create(datetime(2026,10,29,18,tzinfo=TZ),random.Random(1))
            self.assertEqual(first['target'],later['target'])
            self.assertEqual([s['scheduled_at'] for s in first['slots']],[s['scheduled_at'] for s in later['slots']])
            self.assertGreater(sum(s['status']=='skipped' for s in later['slots']),sum(s['status']=='skipped' for s in first['slots']))
            next_day=plans.get_or_create(datetime(2026,10,30,7,tzinfo=TZ),random.Random(1))
            self.assertEqual(next_day['date'],'2026-10-30')
            self.assertTrue(all(s['status']=='pending' for s in next_day['slots']))

    def test_backup_before_migration_only_once_and_preserves_old_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'workbench.db'
            store=Store(path)
            store.set_setting('existing',42)
            MicroPlanStore(store)
            backups=list((Path(tmp)/'backups').glob('*.db'))
            self.assertEqual(len(backups),1)
            self.assertEqual(Store(backups[0]).setting('existing'),42)
            MicroPlanStore(store)
            self.assertEqual(len(list((Path(tmp)/'backups').glob('*.db'))),1)
            self.assertEqual(backups[0].stat().st_mode & 0o777,0o600)

    def test_next_day_expires_old_plan_even_after_clock_rolls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            plans=MicroPlanStore(Store(Path(tmp)/'workbench.db'))
            plans.get_or_create(datetime(2026,10,29,7,tzinfo=TZ),random.Random(4))
            plans.get_or_create(datetime(2026,10,30,7,tzinfo=TZ),random.Random(1))
            old=plans.get_or_create(datetime(2026,10,29,7,tzinfo=TZ),random.Random(9))
            self.assertTrue(all(s['status']=='skipped' for s in old['slots']))

    def test_load_and_atomic_constrained_slot_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            plans=MicroPlanStore(Store(Path(tmp)/'workbench.db'))
            self.assertIsNone(plans.load('2026-10-29'))
            plan=plans.get_or_create(datetime(2026,10,29,7,tzinfo=TZ),random.Random(4))
            slot=plan['slots'][0]
            updated=plans.update_slot(slot['id'],{'status':'queued','queue_id':'queue-1'},expected_statuses=['pending'])
            self.assertEqual(updated['slots'][0]['status'],'queued')
            self.assertEqual(plans.load(plan['date']),updated)
            for changes in [{'id':'changed'},{'scheduled_at':'changed'},{'target':1},{'unknown':True}]:
                with self.assertRaises(ValueError):
                    plans.update_slot(slot['id'],changes)
            with self.assertRaises(ValueError):
                plans.update_slot(slot['id'],{'status':'pending'},expected_statuses=['pending'])
            with self.assertRaises(KeyError):
                plans.update_slot('absent',{'status':'queued'})

    def test_terminal_plan_slots_cannot_resurrect(self):
        with tempfile.TemporaryDirectory() as tmp:
            plans=MicroPlanStore(Store(Path(tmp)/'workbench.db'))
            plan=plans.get_or_create(datetime(2026,10,29,7,tzinfo=TZ),random.Random(4))
            for slot,status in zip(plan['slots'],['skipped','completed']):
                plans.update_slot(slot['id'],{'status':status})
                with self.assertRaises(ValueError):
                    plans.update_slot(slot['id'],{'status':'pending'})

    def test_conditional_slot_claim_is_atomic_between_connections(self):
        from concurrent.futures import ThreadPoolExecutor
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'workbench.db'
            plans=MicroPlanStore(Store(path))
            plan=plans.get_or_create(datetime(2026,10,29,7,tzinfo=TZ),random.Random(4))
            slot_id=plan['slots'][0]['id']
            def claim(_):
                try:
                    MicroPlanStore(Store(path)).update_slot(slot_id,{'status':'queued'},expected_statuses=['pending'])
                    return True
                except ValueError:
                    return False
            with ThreadPoolExecutor(max_workers=6) as executor:
                self.assertEqual(sum(executor.map(claim,range(12))),1)
            self.assertEqual(plans.load(plan['date'])['slots'][0]['status'],'queued')

    def test_possible_submission_plan_slots_cannot_be_reactivated(self):
        with tempfile.TemporaryDirectory() as tmp:
            plans=MicroPlanStore(Store(Path(tmp)/'workbench.db'))
            plan=plans.get_or_create(datetime(2026,10,29,7,tzinfo=TZ),random.Random(4))
            for slot,state in zip(plan['slots'],['publishing','submitted','uncertain']):
                plans.update_slot(slot['id'],{'status':'queued'})
                plans.update_slot(slot['id'],{'status':'publishing'})
                if state!='publishing':
                    plans.update_slot(slot['id'],{'status':state})
                for forbidden in ['pending','queued','awaiting_confirmation','skipped','cancelled','failed']:
                    with self.assertRaises(ValueError):
                        plans.update_slot(slot['id'],{'status':forbidden})
                if state=='uncertain':
                    with self.assertRaises(ValueError):
                        plans.update_slot(slot['id'],{'status':'publishing'})
                plans.update_slot(slot['id'],{'status':'published'})
                self.assertEqual(next(s for s in plans.load(plan['date'])['slots'] if s['id']==slot['id'])['status'],'published')
