import random
import unittest
from copy import deepcopy
from datetime import date

from workbench.micro_schedule import generate_daily_plan


class ContentMixTests(unittest.TestCase):
    def test_daily_plan_reserves_half_for_news_and_persists_types(self):
        for seed in range(40):
            plan = generate_daily_plan(date(2026, 10, 31), random.Random(seed))
            news = sum(s.get('content_type') == 'news' for s in plan['slots'])
            guides = sum(s.get('content_type') == 'tool-guide' for s in plan['slots'])
            self.assertEqual(news, (plan['target'] + 1) // 2)
            self.assertEqual(guides, plan['target'] // 2)
            self.assertEqual(plan['content_mix']['news'], news)
            self.assertEqual(plan['slots'][0]['content_type'], 'news')

    def test_migration_preserves_times_states_and_occupied_content(self):
        from workbench.content_mix import assign_content_mix
        plan = generate_daily_plan(date(2026, 10, 31), random.Random(3))
        plan.pop('content_mix', None)
        for slot in plan['slots']:
            slot.pop('content_type', None)
        plan['slots'][0]['status'] = 'published'
        before = deepcopy(plan)
        assigned = assign_content_mix(plan, {plan['slots'][0]['id']: 'tool-guide'})
        self.assertEqual(assigned['slots'][0]['content_type'], 'tool-guide')
        self.assertEqual([(s['id'], s['scheduled_at'], s['status']) for s in assigned['slots']],
                         [(s['id'], s['scheduled_at'], s['status']) for s in before['slots']])
        self.assertEqual(sum(s['content_type'] == 'news' for s in assigned['slots']), (plan['target']+1)//2)
        self.assertEqual(assign_content_mix(assigned), assigned)
