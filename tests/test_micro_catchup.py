import copy
import random
import unittest
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
from workbench.micro_schedule import add_catchup_slots

TZ=ZoneInfo('Asia/Shanghai')

class CatchupTests(unittest.TestCase):
    def plan(self):
        return {'date':'2026-11-01','target':4,'content_mix':{'news':2,'tool-guide':2},'slots':[
            {'id':'a','scheduled_at':'2026-11-01T08:20:00+08:00','status':'skipped','content_type':'news'},
            {'id':'b','scheduled_at':'2026-11-01T09:00:00+08:00','status':'skipped','content_type':'tool-guide'},
            {'id':'c','scheduled_at':'2026-11-01T11:00:00+08:00','status':'queued','content_type':'news'},
            {'id':'d','scheduled_at':'2026-11-01T12:00:00+08:00','status':'pending','content_type':'tool-guide'}]}
    def test_missed_slots_get_persistent_future_children_without_redrawing_originals(self):
        old=self.plan();original=copy.deepcopy(old);now=datetime(2026,11,1,10,tzinfo=TZ)
        result=add_catchup_slots(old,now,rng=random.Random(4))
        self.assertEqual(old,original)
        self.assertEqual(result['target'],4)
        children=[s for s in result['slots'] if s.get('catchup_for')]
        self.assertEqual(len(children),2)
        for source in original['slots']:
            saved=next(s for s in result['slots'] if s['id']==source['id'])
            for key in ('id','scheduled_at','status','content_type'):self.assertEqual(saved[key],source[key])
        for child in children:
            parent=next(s for s in result['slots'] if s['id']==child['catchup_for'])
            self.assertEqual(parent['catchup_slot_id'],child['id'])
            self.assertEqual(parent['content_type'],child['content_type'])
            self.assertGreaterEqual(datetime.fromisoformat(child['scheduled_at']),now+timedelta(minutes=10))
        active=sorted(datetime.fromisoformat(s['scheduled_at']) for s in result['slots'] if s['status'] in {'pending','queued'})
        self.assertTrue(all(b-a>=timedelta(minutes=16) for a,b in zip(active,active[1:])))
        self.assertEqual(add_catchup_slots(result,now,rng=random.Random(9)),result)
    def test_no_overnight_overflow_or_possible_submission_retry(self):
        plan=self.plan()
        for s,state in zip(plan['slots'],['submitted','uncertain','published','rejected']):s['status']=state
        self.assertEqual(add_catchup_slots(plan,datetime(2026,11,1,10,tzinfo=TZ)),plan)
        plan=self.plan()
        self.assertEqual(add_catchup_slots(plan,datetime(2026,11,1,23,tzinfo=TZ)),plan)
        self.assertEqual(add_catchup_slots(plan,datetime(2026,11,2,10,tzinfo=TZ)),plan)

    def test_full_gaps_can_shift_only_unprepared_future_slot_to_fit_missed_target(self):
        now=datetime(2026,11,1,10,tzinfo=TZ)
        plan=self.plan();plan['target']=5
        plan['slots']=[plan['slots'][0]]+[
            {'id':str(i),'scheduled_at':(now+timedelta(minutes=20+28*i)).isoformat(),
             'status':'pending','content_type':'tool-guide'} for i in range(4)]
        # Limit the remaining day to a dense final sequence, with no free 32m gap.
        now=datetime(2026,11,1,21,tzinfo=TZ)
        for i,s in enumerate(plan['slots'][1:]):s['scheduled_at']=(now+timedelta(minutes=20+28*i)).isoformat()
        result=add_catchup_slots(plan,now,rng=random.Random(1))
        self.assertTrue(next(s for s in result['slots'] if s['id']=='a').get('catchup_slot_id'))
        active=sorted(datetime.fromisoformat(s['scheduled_at']) for s in result['slots'] if s['status']=='pending')
        self.assertEqual(len(active),5)
        self.assertTrue(all(b-a>=timedelta(minutes=16) for a,b in zip(active,active[1:])))
        self.assertTrue(all(t.hour<23 for t in active))
        for original in plan['slots']:
            self.assertEqual(next(s for s in result['slots'] if s['id']==original['id'])['scheduled_at'],original['scheduled_at'])
    def test_manual_cancel_and_confirmed_before_submit_failure_are_not_blindly_retried(self):
        plan=self.plan();plan['slots'][0]['reason']='人工拒绝发布';plan['slots'][1]['status']='failed'
        self.assertEqual(add_catchup_slots(plan,datetime(2026,11,1,10,tzinfo=TZ)),plan)
        plan=self.plan()
        for s in plan['slots'][:2]:s['reason']='内容定位已切换为AI工具，旧稿不再发布'
        self.assertEqual(add_catchup_slots(plan,datetime(2026,11,1,10,tzinfo=TZ)),plan)

    def test_protected_missed_slot_is_never_replaced_even_if_plan_status_is_stale(self):
        plan=self.plan()
        result=add_catchup_slots(plan,datetime(2026,11,1,10,tzinfo=TZ),protected_ids=['a','b'])
        self.assertEqual(result,plan)
    def test_expired_child_can_get_one_new_child_and_respects_actual_submission(self):
        now=datetime(2026,11,1,10,tzinfo=TZ)
        plan=add_catchup_slots(self.plan(),now,rng=random.Random(1))
        children=[s for s in plan['slots'] if s.get('catchup_for')]
        first=children[0];first['status']='skipped'
        later=datetime.fromisoformat(first['scheduled_at'])+timedelta(minutes=1)
        result=add_catchup_slots(plan,later,blocked_times=[later],rng=random.Random(2))
        new=[s for s in result['slots'] if s['id'] not in {v['id'] for v in plan['slots']}]
        self.assertEqual(len(new),1)
        self.assertEqual(new[0]['catchup_for'],first['id'])
        self.assertGreaterEqual(datetime.fromisoformat(new[0]['scheduled_at'])-later,timedelta(minutes=16))

    def test_catchup_uses_extended_evening_window(self):
        result=add_catchup_slots(self.plan(),datetime(2026,11,1,22,tzinfo=TZ),rng=random.Random(1))
        children=[s for s in result['slots'] if s.get('catchup_for')]
        self.assertEqual(len(children),2)
        self.assertTrue(all(22<=datetime.fromisoformat(s['scheduled_at']).hour<23 for s in children))
