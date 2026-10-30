"""Persistent daily micro-post plans; this module never submits content."""
import json
import random
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone

from .micro_schedule import expire_missed_slots, generate_daily_plan, TIMEZONE


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
