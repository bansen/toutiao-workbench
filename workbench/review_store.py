"""Durable daily reviews; private source material never enters public records.

Raw JSON, screenshots and detailed reports expire after 72 hours. Aggregates
and application results expire after 30 days. Only opaque review IDs are public.
"""
import hashlib
import json
import os
import uuid
from datetime import date,datetime,timedelta,timezone
from pathlib import Path
from .review_metrics import build_report,local_markdown


def _now(value=None):
    value=value or datetime.now(timezone.utc)
    if isinstance(value,str):value=datetime.fromisoformat(value)
    if value.tzinfo is None or value.utcoffset() is None:raise ValueError('aware timestamp required')
    return value.astimezone(timezone.utc)


class ReviewStore:
    def __init__(self,store,directory):
        self.store=store;self.directory=Path(directory)
        for kind in ('raw','reports','screenshots'):
            path=self.directory/kind;path.mkdir(parents=True,exist_ok=True);path.chmod(0o700)
        with store.connection() as db:
            db.execute('CREATE TABLE IF NOT EXISTS daily_reviews (id TEXT PRIMARY KEY, account_key TEXT NOT NULL, target_date TEXT NOT NULL, validation INTEGER NOT NULL, payload TEXT NOT NULL, UNIQUE(account_key,target_date,validation))')

    @staticmethod
    def _account(account_id):
        if not isinstance(account_id,str) or not account_id:raise ValueError('account required')
        return hashlib.sha256(account_id.encode()).hexdigest()

    @staticmethod
    def _public(record):
        return {k:v for k,v in record.items() if k!='account_key'}

    def _load(self,db,identity):
        row=db.execute('SELECT payload FROM daily_reviews WHERE id=?',(identity,)).fetchone()
        if not row:raise KeyError('review not found')
        return json.loads(row[0])

    def _save(self,db,record):
        db.execute('UPDATE daily_reviews SET payload=? WHERE id=?',(json.dumps(record,ensure_ascii=False,allow_nan=False),record['id']))

    def claim(self,account_id,target_date,validation=False,now=None):
        account=self._account(account_id);day=date.fromisoformat(target_date).isoformat();stamp=_now(now).isoformat();claimed=False
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT payload FROM daily_reviews WHERE account_key=? AND target_date=? AND validation=?',(account,day,int(bool(validation)))).fetchone()
            if row:
                record=json.loads(row[0])
                if record['state']=='failed' and record['phase']!='application':
                    record.update(state='collecting',phase='collection',updated_at=stamp,error_code=None)
                    for key in ('report','capability_validation','collected_at'):record.pop(key,None)
                    self._save(db,record);claimed=True
            else:
                claimed=True
                record={'id':uuid.uuid4().hex,'date':day,'validation':bool(validation),'state':'collecting','phase':'collection','created_at':stamp,'updated_at':stamp,'error_code':None}
                db.execute('INSERT INTO daily_reviews VALUES (?,?,?,?,?)',(record['id'],account,day,int(bool(validation)),json.dumps(record)))
        return {**self._public(record),'claimed':claimed}

    def get(self,account_id,target_date,validation=False):
        with self.store.connection() as db:
            row=db.execute('SELECT payload FROM daily_reviews WHERE account_key=? AND target_date=? AND validation=?',(self._account(account_id),date.fromisoformat(target_date).isoformat(),int(bool(validation)))).fetchone()
        return self._public(json.loads(row[0])) if row else None

    def _write(self,path,text):
        temporary=path.with_suffix(path.suffix+'.tmp')
        fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
        with os.fdopen(fd,'w',encoding='utf-8') as stream:stream.write(text)
        os.replace(temporary,path)

    def save_collection(self,review_id,rows,complete,capabilities,warnings,now=None):
        stamp=_now(now).isoformat()
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE');record=self._load(db,review_id)
            if record['state']!='collecting':raise ValueError('review is not collecting')
            report=build_report(record['date'],rows,complete,capabilities,warnings)
            # The caller supplies normalized rows; raw source text is kept off SQLite.
            self._write(self.directory/'raw'/f'{review_id}.json',json.dumps(rows,ensure_ascii=False,allow_nan=False))
            self._write(self.directory/'reports'/f'{review_id}.md',local_markdown(record['date'],rows))
            record.update(state='ready' if report['complete'] else 'attention',phase='analysis',report=report,local_report_path=str((self.directory/'reports'/f'{review_id}.md').resolve()),collected_at=stamp,updated_at=stamp,raw_expires_at=(_now(now)+timedelta(hours=72)).isoformat())
            self._save(db,record)
        return self._public(record)

    def analysis(self,review_id):
        with self.store.connection() as db:record=self._load(db,review_id)
        if 'report' not in record:raise ValueError('collection not available')
        return record['report']

    def raw_rows(self,review_id):
        # Internal collector/validation API. Never forward through HTTP handlers.
        with self.store.connection() as db:self._load(db,review_id)
        path=self.directory/'raw'/f'{review_id}.json'
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else []

    def begin_application(self,review_id,now=None,*,findings=None,suggestions=None,policy_intent=None):
        """Exclusive durable claim; reconcile interrupted applications before finish."""
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE');record=self._load(db,review_id)
            claimed=record['state']=='ready' and not record.get('validation')
            if claimed:
                record.update(phase='application',state='applying',updated_at=_now(now).isoformat(),findings=findings,suggestions=suggestions,policy_intent=policy_intent);self._save(db,record)
        return {**self._public(record),'application_claimed':claimed}

    def finish(self,review_id,findings,suggestions,policy_result,now=None):
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE');record=self._load(db,review_id)
            if record['state']=='completed':return self._public(record)
            if not record.get('report',{}).get('complete') or record['state'] not in ('ready','applying','attention'):raise ValueError('review cannot finish')
            if record['state']=='attention' and record['phase']!='application':raise ValueError('application reconciliation required')
            record.update(state='completed',phase='completed',findings=findings,suggestions=suggestions,policy_result=policy_result,updated_at=_now(now).isoformat(),completed_at=_now(now).isoformat())
            self._save(db,record)
        return self._public(record)

    def fail(self,review_id,reason,attention=False,now=None):
        # Exception text may contain browser content or URLs: retain a code only.
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE');record=self._load(db,review_id)
            if record['state']=='completed':return self._public(record)
            record.update(state='attention' if attention or record['phase']=='application' else 'failed',error_code='requires_attention' if attention else 'review_failed',updated_at=_now(now).isoformat());self._save(db,record)
        return self._public(record)

    def retry_collection(self,review_id,now=None):
        """Explicit operator retry; never reset active or implemented work."""
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE');record=self._load(db,review_id)
            if record['phase']=='application' or record['state'] not in ('failed','attention'):
                raise ValueError('review collection cannot be retried')
            record.update(state='failed',error_code='collection_retry_requested',updated_at=_now(now).isoformat())
            self._save(db,record)
        return self._public(record)

    def recover(self):
        counts={'collection':0,'application':0}
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            for row in db.execute('SELECT payload FROM daily_reviews').fetchall():
                record=json.loads(row[0])
                if record['state'] not in ('collecting','applying'):continue
                application=record['state']=='applying';counts['application' if application else 'collection']+=1
                record.update(state='attention' if application else 'failed',error_code='application_reconciliation_required' if application else 'collection_interrupted');self._save(db,record)
        return counts

    def prune(self,now=None):
        stamp=_now(now);counts={'raw':0,'reviews':0}
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            for row in db.execute('SELECT payload FROM daily_reviews').fetchall():
                record=json.loads(row[0]);identity=record['id']
                created=_now(record['created_at'])
                raw_expired=stamp>=_now(record.get('raw_expires_at',(created+timedelta(hours=72)).isoformat()))
                expired=stamp>=created+timedelta(days=30)
                if raw_expired or expired:
                    for kind in ('raw','reports','screenshots'):
                        for path in (self.directory/kind).glob(identity+'*'):
                            if path.is_file():path.unlink();counts['raw']+=1
                if expired:
                    db.execute('DELETE FROM daily_reviews WHERE id=?',(identity,));counts['reviews']+=1
            # Crash-created files have no committed record; age them out independently.
            for kind in ('raw','reports','screenshots'):
                for path in (self.directory/kind).iterdir():
                    if path.is_file() and stamp.timestamp()-path.stat().st_mtime>=72*3600:
                        path.unlink();counts['raw']+=1
            # Older probe versions wrote their sensitive artifacts at this root.
            for path in self.directory.glob('probe-*'):
                if path.suffix not in ('.json','.png'):continue
                if path.is_file() and stamp.timestamp()-path.stat().st_mtime>=72*3600:
                    path.unlink();counts['raw']+=1
        return counts
