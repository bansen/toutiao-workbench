"""Persistent daily micro-post plans; this module never submits content."""
import json
import random
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone

from .micro_schedule import expire_missed_slots, generate_daily_plan, add_catchup_slots, TIMEZONE, MIN_DAILY_TARGET, MAX_DAILY_TARGET
from .content_mix import assign_content_mix, content_kind


class MicroPlanStore:
    def __init__(self, store):
        self.store=store
        with store.connection() as db:
            exists=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='micro_daily_plans'").fetchone()
        if not exists:
            # SQLite's backup API includes WAL state, unlike copying the DB file.
            directory=store.path.parent/'backups'
            directory.mkdir(mode=0o700,parents=True,exist_ok=True)
            name=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'-'+uuid.uuid4().hex[:8]+'.db'
            path=directory/name
            path.touch(mode=0o600,exist_ok=False)
            with store.connection() as source, closing(sqlite3.connect(path)) as target:
                source.backup(target)
            with store.connection() as db:
                db.execute('CREATE TABLE IF NOT EXISTS micro_daily_plans (day TEXT PRIMARY KEY, payload TEXT NOT NULL)')

    def get_or_create(self, now, rng=None):
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError('排期时间必须包含时区')
        day=now.astimezone(TIMEZONE).date().isoformat()
        # One transaction protects against two callers drawing the same day twice.
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            older=db.execute('SELECT day,payload FROM micro_daily_plans WHERE day<?',(day,)).fetchall()
            for old in older:
                expired=expire_missed_slots(json.loads(old['payload']),now)
                db.execute('UPDATE micro_daily_plans SET payload=? WHERE day=?',(self.store.dump(expired),old['day']))
            row=db.execute('SELECT payload FROM micro_daily_plans WHERE day=?',(day,)).fetchone()
            plan=json.loads(row[0]) if row else generate_daily_plan(now,rng if rng is not None else random.SystemRandom())
            if not plan.get('content_mix'):
                fixed = {}
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='micro_publish_queue'").fetchone():
                    fixed = {item['slot_id']:content_kind(item) for record in db.execute('SELECT payload FROM micro_publish_queue')
                             if (item:=json.loads(record[0]))['scheduled_at'].startswith(day)}
                plan=assign_content_mix(plan, fixed)
            if not MIN_DAILY_TARGET<=plan['target']<=MAX_DAILY_TARGET:
                previous=plan['target']
                target=(rng or random.SystemRandom()).randint(MIN_DAILY_TARGET,MAX_DAILY_TARGET)
                queue_fixed={}
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='micro_publish_queue'").fetchone():
                    for record in db.execute('SELECT payload FROM micro_publish_queue'):
                        item=json.loads(record[0])
                        if item['scheduled_at'].startswith(day) and item['status'] in {
                            'pending','awaiting_confirmation','publishing','submitted','published','rejected','uncertain'
                        }:
                            queue_fixed[item['slot_id']]=content_kind(item)
                protected={'queued','awaiting_confirmation','publishing','submitted','published','completed','uncertain','rejected'}
                fixed_ids=set(queue_fixed)
                fixed_ids.update(s['id'] for s in plan['slots'] if s['status'] in protected or s.get('queue_id'))
                fixed_count=sum(s['status'] in protected or s['id'] in fixed_ids for s in plan['slots'])
                # Never invalidate queued or possibly submitted posts. If those already
                # exceed the random target, freeze new slots until the protected count.
                plan['target']=max(target,fixed_count)
                keep=max(0,plan['target']-fixed_count)
                flexible=sorted((s for s in plan['slots'] if s['status']=='pending' and s['id'] not in fixed_ids),
                                key=lambda s:s['scheduled_at'])
                kept={s['id'] for s in flexible[:keep]}
                for slot in flexible:
                    if slot['id'] not in kept:
                        slot.update(status='skipped',reason='每日目标调整，超出10–15条上限')
                fixed={slot_id:kind for slot_id,kind in queue_fixed.items() if kind}
                fixed.update({s['id']:s['content_type'] for s in plan['slots'] if s['id'] in fixed_ids and s.get('content_type')})
                eligible=[s for s in plan['slots'] if s['id'] in fixed_ids or s['id'] in kept]
                mixed=assign_content_mix({'target':plan['target'],'slots':eligible},fixed)
                kinds={s['id']:s['content_type'] for s in mixed['slots']}
                for slot in plan['slots']:
                    if slot['id'] in kinds:slot['content_type']=kinds[slot['id']]
                plan['content_mix']={'version':1,'news_ratio':.5,
                    'news':(plan['target']+1)//2,'tool-guide':plan['target']//2}
                plan['quota_adjustment']={'from':previous,'target':plan['target'],'at':now.isoformat()}
            plan['target_policy']='10-15'
            plan=expire_missed_slots(plan,now)
            db.execute('INSERT INTO micro_daily_plans VALUES(?,?) ON CONFLICT(day) DO UPDATE SET payload=excluded.payload',
                       (day,self.store.dump(plan)))
        return plan

    def load(self, day):
        """Read a stored plan without drawing slots or changing expiry."""
        key=day.isoformat() if hasattr(day,'isoformat') else day
        with self.store.connection() as db:
            row=db.execute('SELECT payload FROM micro_daily_plans WHERE day=?',(key,)).fetchone()
        return json.loads(row[0]) if row else None

    def schedule_catchups(self, now, blocked_times=()):
        day=now.astimezone(TIMEZONE).date().isoformat()
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT payload FROM micro_daily_plans WHERE day=?',(day,)).fetchone()
            if row is None:return None
            plan=json.loads(row[0])
            protected=[]
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='micro_publish_queue'").fetchone():
                protected=[item['slot_id'] for record in db.execute('SELECT payload FROM micro_publish_queue')
                    if (item:=json.loads(record[0]))['status'] in {'pending','awaiting_confirmation','publishing','submitted','published','rejected','uncertain'}]
            result=add_catchup_slots(plan,now,blocked_times,protected_ids=protected)
            if result!=plan:
                db.execute('UPDATE micro_daily_plans SET payload=? WHERE day=?',(self.store.dump(result),day))
            return result

    def update_slot(self, slot_id, changes, expected_statuses=None, *, before_submit=False):
        """Atomically update slot progress while preserving the drawn schedule."""
        allowed={'status','reason','queue_id','draft_id','topic','event_key','publication_id','receipt'}
        statuses={'pending','queued','awaiting_confirmation','publishing','submitted','published',
                  'completed','rejected','uncertain','failed','skipped','cancelled'}
        if set(changes)-allowed or ('status' in changes and changes['status'] not in statuses):
            raise ValueError('不能修改排期标识、时间、目标或未知字段')
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            # The full identity is looked up, never trusted as an SQL identifier.
            for record in db.execute('SELECT day,payload FROM micro_daily_plans').fetchall():
                plan=json.loads(record['payload'])
                slot=next((item for item in plan['slots'] if item['id']==slot_id),None)
                if slot is None:
                    continue
                if expected_statuses is not None and slot['status'] not in expected_statuses:
                    raise ValueError('排期状态已变化')
                possible_submission={
                    'publishing':{'submitted','published','rejected','uncertain'},
                    'submitted':{'published','rejected','uncertain'},
                    'uncertain':{'submitted','published','rejected'},
                }
                next_status=changes.get('status',slot['status'])
                proven_before_submit = before_submit is True and slot['status']=='publishing' and next_status=='failed'
                if slot['status'] in possible_submission and next_status!=slot['status'] and next_status not in possible_submission[slot['status']] and not proven_before_submit:
                    raise ValueError('可能已提交的排期不能重新启用，必须核实平台回执')
                if slot['status'] in ('skipped','completed','published','rejected','cancelled','failed') and changes.get('status',slot['status'])!=slot['status']:
                    raise ValueError('终态排期不能重新启用')
                slot.update(changes)
                db.execute('UPDATE micro_daily_plans SET payload=? WHERE day=?',(self.store.dump(plan),record['day']))
                return plan
            raise KeyError('排期不存在')
