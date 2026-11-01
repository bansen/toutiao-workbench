"""Local daily review orchestration. Collection and policy writes are separate claims.

An interrupted application is deliberately never replayed: its durable intent is
retained for inspection, while repeated finish requests return attention.
"""
import asyncio
import json
from datetime import date,datetime,timedelta
from zoneinfo import ZoneInfo

from .browser import NeedsAttention

ZONE=ZoneInfo('Asia/Shanghai')
CONFIG='daily_review_settings_v1'


class ReviewService:
    def __init__(self,review_store,policy,collector,account,clock=None):
        self.reviews=review_store;self.policy=policy;self.collector=collector;self.account=account
        self.clock=clock or (lambda:datetime.now(ZONE));self._tasks={};self._retention_task=None
        self.reviews.recover()
        if self.reviews.store.setting(CONFIG) is None:
            now=self._now();first=now.replace(hour=5,minute=0,second=0,microsecond=0)
            if first<=now:first+=timedelta(days=1)
            self.reviews.store.set_setting(CONFIG,{'enabled':False,'installed_at':now.isoformat(),'first_due':first.isoformat()})

    async def start(self):
        """Retention runs even while reviews are disabled or need attention."""
        if self._retention_task and not self._retention_task.done():return
        self._prune_once()
        self._retention_task=asyncio.create_task(self._retention_loop())

    def _prune_once(self):
        for owner in (self.reviews,self.policy):
            try:owner.prune(self._now())
            except Exception:
                # One failed filesystem/database cleanup must not disable the
                # other owner or permanently stop future hourly attempts.
                continue

    async def _retention_loop(self):
        while True:
            await asyncio.sleep(3600)
            self._prune_once()

    def _now(self,value=None):
        now=value or self.clock()
        if now.tzinfo is None or now.utcoffset() is None:raise ValueError('review time requires timezone')
        return now.astimezone(ZONE)

    @staticmethod
    def _bool(value):
        if type(value) is not bool:raise ValueError('validation/enabled must be boolean')
        return value

    def _account(self):
        value=self.account()
        if not isinstance(value,str) or not value:raise ValueError('publishing account required')
        return value

    def _validation_passed(self):
        try:key=self.reviews._account(self._account())
        except ValueError:return False
        with self.reviews.store.connection() as db:
            rows=db.execute('SELECT payload FROM daily_reviews WHERE account_key=? AND validation=1',(key,)).fetchall()
        return any((r:=json.loads(row[0])).get('state')=='completed' and r.get('report',{}).get('complete') is True and r.get('capability_validation',{}).get('passed') is True for row in rows)

    def settings(self):
        config=self.reviews.store.setting(CONFIG,{})
        return {**config,'validated':self._validation_passed()}

    def configure(self,enabled,now=None):
        self._bool(enabled)
        if enabled and not self._validation_passed():raise ValueError('complete real validation before enabling reviews')
        config=self.reviews.store.setting(CONFIG,{})
        stamp=self._now(now)
        if enabled and not config.get('activated_at'):
            first=stamp.replace(hour=5,minute=0,second=0,microsecond=0)
            if first<=stamp:first+=timedelta(days=1)
            config.update(activated_at=stamp.isoformat(),first_due=first.isoformat())
        config.update(enabled=enabled,updated_at=stamp.isoformat())
        self.reviews.store.set_setting(CONFIG,config)
        return self.settings()

    def _latest(self,now=None):
        now=self._now(now);due=now.replace(hour=5,minute=0,second=0,microsecond=0)
        if due>now:due-=timedelta(days=1)
        return due,(due.date()-timedelta(days=1)).isoformat()

    def _target(self,target_date=None,validation=False,now=None):
        self._bool(validation)
        _,latest=self._latest(now)
        if target_date is None:return latest
        if not isinstance(target_date,str):raise ValueError('target date must be ISO date')
        day=date.fromisoformat(target_date).isoformat()
        if day!=target_date or day!=latest:raise ValueError('only the latest due publication day can be reviewed')
        return day

    def _record(self,review_id):
        with self.reviews.store.connection() as db:record=self.reviews._load(db,review_id)
        owned=self.reviews.get(self._account(),record['date'],record['validation'])
        if not owned or owned['id']!=review_id:raise KeyError('review not found')
        return owned

    def status(self,review_id=None,target_date=None,validation=False):
        if review_id is not None:return self._record(review_id)
        return self.reviews.get(self._account(),self._target(target_date,validation),validation)

    def anonymous(self,review_id):
        self._record(review_id)
        return self.reviews.analysis(review_id)

    async def start_run(self,target_date=None,validation=False,now=None):
        day=self._target(target_date,validation,now);account=self._account()
        self.reviews.prune(self._now(now))
        record=self.reviews.claim(account,day,validation,self._now(now))
        if record['claimed']:
            task=asyncio.create_task(self._collect(record['id'],day,account))
            self._tasks[record['id']]=task
            def completed(done,key=record['id']):
                if self._tasks.get(key) is done:self._tasks.pop(key,None)
            task.add_done_callback(completed)
        return {k:v for k,v in record.items() if k!='claimed'}

    async def _collect(self,review_id,day,account):
        try:
            result=await self.collector.collect(day)
            if self.account()!=account:
                self.reviews.fail(review_id,'account_changed',attention=True,now=self._now());return
            if not isinstance(result,dict):raise ValueError('invalid collector result')
            saved=self.reviews.save_collection(review_id,result.get('rows',[]),result.get('complete') is True,
                                         result.get('capabilities') or {},result.get('warnings') or [],now=self._now())
            validation=self._capability_validation(saved,result)
            if not validation['passed']:self.reviews.fail(review_id,'capability_validation_failed',attention=True,now=self._now())
        except asyncio.CancelledError:
            self.reviews.fail(review_id,'collection_interrupted',now=self._now());raise
        except NeedsAttention:
            self.reviews.fail(review_id,'collector_attention',attention=True,now=self._now())
        except Exception:
            self.reviews.fail(review_id,'collection_failed',now=self._now())

    def _capability_validation(self,record,result):
        from .review_metrics import timestamp
        target=record['date'];rows=result.get('rows') or []
        target_rows=[r for r in rows if r.get('status')=='published' and
                     (when:=timestamp(r.get('published_at'))) is not None and when.date().isoformat()==target]
        keys=('list_drawer_match','time_match','public_id_verified')
        verified=sum(all((r.get('verification') or {}).get(k) is True for k in keys) for r in target_rows)
        count=record.get('report',{}).get('post_count',0);required=min(10,count)
        raw_count=result.get('verified_samples');collector_count=raw_count if type(raw_count) is int and raw_count>=0 else 0
        pagination=result.get('pagination') or {};pages=pagination.get('pages')
        pagination={'pages':pages if type(pages) is int and pages>=0 else None,'complete':pagination.get('complete') is True}
        failures=[]
        if result.get('warnings'):failures.append('collection_warnings')
        if count<1:failures.append('no_target_posts')
        if result.get('account_checked') is not True:failures.append('account_not_verified')
        if not pagination['complete'] or pagination['pages'] is None or pagination['pages']<1:failures.append('pagination_incomplete')
        if verified<required or collector_count<required:failures.append('sample_verification_incomplete')
        if record.get('report',{}).get('complete') is not True:failures.append('report_incomplete')
        if not target_rows or any(r.get('scope')!='cumulative' for r in target_rows):failures.append('statistical_scope_unverified')
        warnings=[v[:500] for v in (result.get('warnings') or [])[:30] if isinstance(v,str)]
        summary={'passed':not failures,'target_rows':count,'required_samples':required,'verified_samples':verified,
                 'collector_verified_samples':collector_count,'account_checked':result.get('account_checked') is True,
                 'pagination':pagination,'warning_count':len(warnings),'failure_codes':failures}
        # Concrete browser warnings remain in the private capability artifact;
        # only counts/codes are exposed alongside aggregate API records.
        self.reviews._write(self.reviews.directory/'reports'/(record['id']+'-capabilities.json'),
                            json.dumps({**summary,'warnings':warnings},ensure_ascii=False,allow_nan=False))
        with self.reviews.store.connection() as db:
            db.execute('BEGIN IMMEDIATE');current=self.reviews._load(db,record['id'])
            current['capability_validation']=summary;self.reviews._save(db,current)
        return summary

    async def wait(self,review_id):
        self._record(review_id)
        task=self._tasks.get(review_id)
        if task:await asyncio.shield(task)
        return self._record(review_id)

    async def close(self):
        tasks=list(self._tasks.values())
        if self._retention_task:
            tasks.append(self._retention_task);self._retention_task=None
        for task in tasks:task.cancel()
        if tasks:await asyncio.gather(*tasks,return_exceptions=True)

    @staticmethod
    def _notes(value):
        value=[] if value is None else value
        if not isinstance(value,list) or len(value)>12 or any(not isinstance(v,str) or len(v)>500 for v in value):
            raise ValueError('findings/suggestions require at most 12 strings of 500 characters')
        # Stored as inert local notes, never interpolated into generation prompts.
        return [v.strip() for v in value if v.strip()]

    def finish(self,review_id,findings=None,suggestions=None,changes=None,expected_version=None):
        record=self._record(review_id)
        if record['state']=='completed':return record
        if record['phase']=='application':return record
        findings=self._notes(findings);suggestions=self._notes(suggestions)
        if record['state']!='ready' or record.get('report',{}).get('complete') is not True:
            raise ValueError('only a complete server-collected report can finish')
        self._target(record['date'],record['validation'])
        if record['validation']:
            return self.reviews.finish(review_id,findings,suggestions,{'action':'validation_only'},now=self._now())
        state=self.policy.status()
        if expected_version is not None:self.policy._cas(state,expected_version)
        if changes is not None:
            if expected_version is None:raise ValueError('expected_version required for policy changes')
            self.policy._changes(state['baseline'],changes)
        intent={'expected_version':state['version'],'changes':changes,'operation':'evaluate' if state.get('active') or changes is None else 'propose'}
        claim=self.reviews.begin_application(review_id,now=self._now(),findings=findings,suggestions=suggestions,policy_intent=intent)
        if not claim['application_claimed']:return self._record(review_id)
        try:
            # Always reload the server archive; callers cannot supply report totals.
            report=self.reviews.analysis(review_id)
            result=(self.policy.evaluate(report,now=self._now()) if intent['operation']=='evaluate'
                    else self.policy.propose(report,changes,intent['expected_version'],now=self._now()))
            return self.reviews.finish(review_id,findings,suggestions,result,now=self._now())
        except Exception:
            # This claim may already have changed policy. Never retry it blindly.
            return self.reviews.fail(review_id,'application_interrupted',attention=True,now=self._now())

    def check_due(self,now=None):
        stamp=self._now(now);config=self.settings();due,day=self._latest(stamp)
        reason=None
        if not config.get('enabled'):reason='disabled'
        elif not config['validated']:reason='validation_required'
        elif due<datetime.fromisoformat(config['first_due']):reason='before_first_due'
        else:
            record=self.reviews.get(self._account(),day)
            if record and record['state'] not in ('failed','ready'):reason='already_'+record['state']
        return {'due':reason is None,'date':day,'due_at':due.isoformat(),'reason':reason}

    async def run_due(self,now=None):
        check=self.check_due(now)
        if not check['due']:return {'state':'noop',**check}
        return await self.start_run(check['date'],now=now)
