import copy
import json
import random
import unittest
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from workbench import micro_schedule


SHANGHAI = ZoneInfo('Asia/Shanghai')
DAY = date(2026, 10, 29)


class MicroScheduleTests(unittest.TestCase):
    def test_many_seeds_have_bounded_target_times_and_minimum_spacing(self):
        seen_targets = set()
        for seed in range(1000):
            with self.subTest(seed=seed):
                plan = micro_schedule.generate_daily_plan(DAY, random.Random(seed))
                seen_targets.add(plan['target'])
                self.assertEqual(set(plan), {'date', 'timezone', 'target', 'slots', 'content_mix'})
                self.assertEqual(plan['date'], DAY.isoformat())
                self.assertEqual(plan['timezone'], 'Asia/Shanghai')
                self.assertTrue(24 <= plan['target'] <= 30)
                self.assertEqual(len(plan['slots']), plan['target'])
                self.assertEqual(len({slot['id'] for slot in plan['slots']}), plan['target'])
                times = [datetime.fromisoformat(slot['scheduled_at']) for slot in plan['slots']]
                start = datetime(2026, 10, 29, 8, tzinfo=SHANGHAI)
                end = start + timedelta(hours=14)
                width = (end - start) / plan['target']
                for index, (slot, scheduled) in enumerate(zip(plan['slots'], times)):
                    self.assertEqual(set(slot), {'id', 'scheduled_at', 'status', 'content_type'})
                    self.assertEqual(slot['status'], 'pending')
                    self.assertEqual(scheduled.utcoffset(), timedelta(hours=8))
                    self.assertGreaterEqual(scheduled, start + width * (index + .3))
                    self.assertLessEqual(scheduled, start + width * (index + .7))
                    self.assertLess(scheduled, end)
                self.assertTrue(all(b - a >= timedelta(minutes=15) for a, b in zip(times, times[1:])))
                self.assertEqual(json.loads(json.dumps(plan)), plan)
        self.assertEqual(seen_targets, set(range(24, 31)))

    def test_seed_reproduces_plan_and_other_seeds_change_it(self):
        plan = micro_schedule.generate_daily_plan(DAY, random.Random(7))
        self.assertEqual(plan, micro_schedule.generate_daily_plan(DAY, random.Random(7)))
        self.assertNotEqual(plan, micro_schedule.generate_daily_plan(DAY, random.Random(8)))

    def test_middle_of_day_marks_past_slots_without_redistribution(self):
        now = datetime(2026, 10, 29, 14, tzinfo=SHANGHAI)
        full = micro_schedule.generate_daily_plan(DAY, random.Random(7))
        partial = micro_schedule.generate_daily_plan(now, random.Random(7))
        self.assertEqual(full['target'], partial['target'])
        self.assertEqual([s['scheduled_at'] for s in full['slots']], [s['scheduled_at'] for s in partial['slots']])
        for slot in partial['slots']:
            expected = 'skipped' if datetime.fromisoformat(slot['scheduled_at']) < now else 'pending'
            self.assertEqual(slot['status'], expected)

    def test_utc_clock_uses_shanghai_calendar_day(self):
        now = datetime(2026, 10, 28, 23, 30, tzinfo=timezone.utc)
        plan = micro_schedule.generate_daily_plan(now, random.Random(1))
        self.assertEqual(plan['date'], DAY.isoformat())
        self.assertTrue(all(slot['status'] == 'pending' for slot in plan['slots']))

    def test_after_day_end_skips_every_slot_and_has_no_next_time(self):
        now = datetime(2026, 10, 29, 23, tzinfo=SHANGHAI)
        plan = micro_schedule.generate_daily_plan(now, random.Random(1))
        self.assertTrue(all(slot['status'] == 'skipped' for slot in plan['slots']))
        self.assertIsNone(micro_schedule.next_scheduled_at(plan, now))

    def test_expire_missed_slots_is_pure_and_does_not_catch_up(self):
        plan = micro_schedule.generate_daily_plan(DAY, random.Random(2))
        original = copy.deepcopy(plan)
        clock = datetime.fromisoformat(plan['slots'][5]['scheduled_at'])
        expired = micro_schedule.expire_missed_slots(plan, clock)
        self.assertEqual(plan, original)
        self.assertTrue(all(s['status'] == 'skipped' for s in expired['slots'][:5]))
        self.assertEqual(expired['slots'][5]['status'], 'pending')
        self.assertEqual(micro_schedule.next_scheduled_at(plan, clock), clock)
        later = clock + timedelta(seconds=1)
        self.assertEqual(micro_schedule.next_scheduled_at(plan, later), datetime.fromisoformat(plan['slots'][6]['scheduled_at']))
        self.assertEqual(micro_schedule.expire_missed_slots(expired, clock), expired)
        # A backward wall-clock adjustment must never revive a skipped slot.
        earlier = clock - timedelta(hours=4)
        self.assertEqual(micro_schedule.expire_missed_slots(expired, earlier), expired)
        self.assertEqual(micro_schedule.next_scheduled_at(expired, earlier), clock)

    def test_next_time_ignores_skipped_slots_and_accepts_utc_clock(self):
        plan = micro_schedule.generate_daily_plan(DAY, random.Random(2))
        plan['slots'][0]['status'] = 'skipped'
        before_day = datetime(2026, 10, 28, 23, tzinfo=timezone.utc)
        result = micro_schedule.next_scheduled_at(plan, before_day)
        self.assertEqual(result, datetime.fromisoformat(plan['slots'][1]['scheduled_at']))
        self.assertEqual(result.tzinfo, SHANGHAI)

    def test_naive_clocks_are_rejected(self):
        naive = datetime(2026, 10, 29, 14)
        plan = micro_schedule.generate_daily_plan(DAY, random.Random(2))
        for operation in [lambda: micro_schedule.generate_daily_plan(naive, random.Random(2)),
                          lambda: micro_schedule.expire_missed_slots(plan, naive),
                          lambda: micro_schedule.next_scheduled_at(plan, naive)]:
            with self.assertRaises(ValueError):
                operation()


if __name__ == '__main__':
    unittest.main()
