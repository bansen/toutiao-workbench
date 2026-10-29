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
