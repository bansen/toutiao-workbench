"""Bounded editorial experiments. No executable instructions or publishing settings."""
import copy
import hashlib
import json
import math
from .review_metrics import SUPPORTED_SCOPES
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ANGLES = {'concrete_change': '标题优先具体变化', 'reader_impact': '标题优先读者实际影响', 'practical_value': '标题优先可兑现的实用价值'}
OPENINGS = {'scene_first': '开头先交代真实使用场景', 'fact_first': '开头先交代有来源的关键事实', 'impact_first': '开头先交代有来源的实际影响'}
DEFAULT = {'headline_angle': 'concrete_change', 'opening': 'scene_first', 'target_length': 400,
           'news_paragraph_length': 90, 'topic_weights': dict.fromkeys(('hot','business','tech','social'),1.0)}
AGES = {'6-12','12-18','18-24','24-48','48-72'}
KINDS = {'news','tool-guide'}
KEY = 'editorial_policy_state_v1'


def _number(value):
    return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value) and value >= 0


def _day(value):
    return date.fromisoformat(value).isoformat()


def render_preferences(context):
    """Only server enums and bounded numbers become generation instructions."""
    if isinstance(context,dict) and context.get('policy_version')==0 and context.get('experiment_id') is None:return ''
    p = context.get('preferences', {}) if isinstance(context,dict) else {}
    lines = [ANGLES[p['headline_angle']]] if p.get('headline_angle') in ANGLES else []
    if p.get('opening') in OPENINGS:lines.append(OPENINGS[p['opening']])
    for key,lo,hi,label in (('target_length',250,600,'正文目标字数'),('news_paragraph_length',60,120,'新闻自然段目标字数')):
        value=p.get(key)
        if type(value) is int and lo <= value <= hi:lines.append(f'{label}约{value}字')
    return '\n'.join(lines)


def select_candidate(candidates, context):
    """Deterministic weighted draw within one content bucket, before selecting a source."""
    kind=context['content_kind'];preferences=context.get('preferences',{})
    eligible=[c for c in candidates if ('tool-guide' if c[1]=='official' else 'news')==kind]
    if context.get('policy_version')==0 and context.get('experiment_id') is None:
        return eligible[0] if eligible else None
    def score(candidate):
        card=candidate[2]
        key=card.get('news_category') if kind=='news' else (card.get('author') or '').removesuffix(' 官方文档')
        weights=preferences.get('topic_weights' if kind=='news' else 'tool_weights',{})
        weight=weights.get(key,1)
        if not _number(weight) or not .5<=weight<=1.5:weight=1
        digest=hashlib.sha256((context['intent_id']+'|'+str(card.get('url',''))).encode()).hexdigest()
        unit=(int(digest,16)+1)/(2**256+1)
        return -math.log(unit)/weight
    return min(eligible,key=score) if eligible else None


class EditorialPolicy:
    def __init__(self, store):
        self.store=store
        with store.connection() as db:
            db.executescript('CREATE TABLE IF NOT EXISTS editorial_intents (key TEXT PRIMARY KEY, day TEXT NOT NULL, payload TEXT NOT NULL); CREATE TABLE IF NOT EXISTS editorial_versions (version INTEGER PRIMARY KEY, day TEXT NOT NULL, payload TEXT NOT NULL);')

    def _initial(self):
        from .ai_tools import PRODUCTS
        p={**copy.deepcopy(DEFAULT),'tool_weights':dict.fromkeys((r[0] for r in PRODUCTS),1.0)}
        return {'version':0,'current':p,'baseline':copy.deepcopy(p),'active':None,'started_days':[],'last_started_day':None,'last_proposal':None,'last_action':'baseline'}

    def _load(self,db):
        row=db.execute('SELECT payload FROM settings WHERE key=?',(KEY,)).fetchone()
        return json.loads(row[0]) if row else self._initial()

    def status(self):
        with self.store.connection() as db:return self._load(db)

    def _save(self,db,state,day):
        db.execute('INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload',(KEY,self.store.dump(state)))
        db.execute('INSERT OR REPLACE INTO editorial_versions VALUES (?,?,?)',(state['version'],day,self.store.dump(state)))
        self._prune(db,state,day)

    @staticmethod
    def _prune(db,state,day):
        cutoff=(date.fromisoformat(day)-timedelta(days=30)).isoformat()
        keep=[state['version']]
        if state['active']:keep.append(state['active']['baseline_version'])
        if state.get('rollback_version') is not None:keep.append(state['rollback_version'])
        versions=db.execute('DELETE FROM editorial_versions WHERE day < ? AND version NOT IN ('+','.join('?' for _ in keep)+')',(cutoff,*keep)).rowcount
        intents=db.execute('DELETE FROM editorial_intents WHERE day < ?',(cutoff,)).rowcount
        return {'versions':versions,'intents':intents}

    def prune(self,now=None):
        """Expire history independently of generation or experiment activity.

        The current policy and necessary active/rollback baseline are effective
        configuration, not historical records; retain these even beyond 30 days.
        """
        stamp=now or datetime.now(timezone.utc)
        if stamp.tzinfo is None or stamp.utcoffset() is None:raise ValueError('retention time requires timezone')
        day=stamp.astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat()
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            return self._prune(db,self._load(db),day)

    @staticmethod
    def _cas(s,expected):
        if type(expected) is not int or s['version']!=expected:raise ValueError('策略版本冲突，请重新读取')

    @staticmethod
    def _changes(current,changes):
        if not isinstance(changes,dict) or len(changes)!=1:raise ValueError('每次仅允许调整一项策略')
        key,value=next(iter(changes.items()))
        p=copy.deepcopy(current)
        if key in ('headline_angle','opening'):
            if not isinstance(value,str) or value not in (ANGLES if key=='headline_angle' else OPENINGS):raise ValueError('策略枚举不允许')
        elif key in ('target_length','news_paragraph_length'):
            lo,hi=(250,600) if key=='target_length' else (60,120)
            if type(value) is not int or not lo<=value<=hi:raise ValueError('字数策略越界')
        elif key in ('topic_weights','tool_weights'):
            if not isinstance(value,dict) or len(value)!=1:raise ValueError('每次仅允许调整一个权重')
            name,weight=next(iter(value.items()))
            if name not in current[key] or not _number(weight) or not .5<=weight<=1.5 or abs(weight/current[key][name]-1)>.2000000001:raise ValueError('权重越界')
            value={**current[key],name:weight}
        else:raise ValueError('不允许修改此字段')
        if p[key]==value:raise ValueError('策略未变化')
        p[key]=value
        return p

    def propose(self,report,changes,expected_version,now=None):
        day=_day(report['date'])
        fingerprint=hashlib.sha256(json.dumps([day,changes,expected_version],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE');s=self._load(db)
            if s.get('last_proposal')==fingerprint:return s
            self._cas(s,expected_version)
            trial=self._changes(s['baseline'],changes)
            if s['active'] or s['last_started_day']==day or day in s.get('started_days',[]):raise ValueError('已有实验或当日已启动实验')
            totals=report.get('totals') or {}
            if report.get('complete') is not True or report.get('quality_alert') is True or not _number(report.get('sample_count')) or report['sample_count']<10 or not any(_number(totals.get(k)) and totals[k]>=n for k,n in (('impressions',1000),('reads',200))):return s
            self._save(db,s,day)
            s['started_days']=(s.get('started_days',[])+[day])[-366:]
            previous=s['version'];s['version']+=1
            s.update(current=trial,active={'id':fingerprint[:24],'started_day':day,'baseline_version':previous,'trial_version':s['version'],'streak':0,'evaluated_days':[]},last_started_day=day,last_proposal=fingerprint,last_action='started')
            self._save(db,s,day);return s

    def rollback(self,expected_version,now=None):
        day=(now or datetime.now(timezone.utc)).date().isoformat()
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE');s=self._load(db);self._cas(s,expected_version)
            if not s['active'] and not s.get('rollback_baseline'):return s
            s['current']=copy.deepcopy(s['baseline'] if s['active'] else s['rollback_baseline'])
            s.update(baseline=copy.deepcopy(s['current']),active=None,version=s['version']+1,last_action='rolled_back')
            s.pop('rollback_baseline',None);self._save(db,s,day);return s

    @staticmethod
    def _comparison(report,active):
        grouped={}
        cohorts=report.get('cohorts') or []
        unavailable=(report.get('capabilities') or {}).get('impressions') is False
        # Aggregated null can conceal partial known values. A platform that
        # produced any impressions cannot be classified as lacking the feature.
        if unavailable:
            for row in cohorts:
                known=((row.get('metric_coverage') or {}).get('impressions') or {}).get('known')
                if _number(row.get('impressions')) or (_number(known) and known>0):return None
        for r in cohorts:
            if r.get('arm') not in ('baseline','trial') or r.get('content_kind') not in KINDS or r.get('age_band') not in AGES:continue
            if r.get('experiment_id') != active['id'] or r.get('scope') not in SUPPORTED_SCOPES:continue
            if r.get('publication_band') not in {'00-06','06-12','12-18','18-24'}:continue
            try:_day(r.get('publication_date'))
            except (TypeError,ValueError):continue
            key=(r['content_kind'],r['age_band'],r['scope'],r['publication_date'],r['publication_band'])
            grouped.setdefault(key,{}).setdefault(r['arm'],[]).append(r)
        pairs=[v for v in grouped.values() if 'baseline' in v and 'trial' in v]
        rows={arm:[r for p in pairs for r in p[arm]] for arm in ('baseline','trial')}
        if not all(rows.values()):return None
        def total(arm,key):
            vals=[r.get(key) for r in rows[arm]]
            return sum(vals) if all(_number(v) for v in vals) else None
        posts={a:total(a,'posts') for a in rows}
        if any(v is None or v<5 for v in posts.values()):return None
        reads={a:total(a,'reads') for a in rows};imps={a:total(a,'impressions') for a in rows}
        if any(v is None for v in reads.values()):return None
        exposure=all(v is not None for v in imps.values())
        missing_exposure=all(r.get('impressions') is None for arm in rows.values() for r in arm)
        if not exposure and (not missing_exposure or not unavailable):return None
        if exposure and any(v<1000 for v in imps.values()):return None
        if not exposure and any(v<200 for v in reads.values()):return None
        # Standardize each trial stratum to baseline exposure/post distribution.
        def standardized(metric,denominator):
            base=trial=0
            for pair in pairs:
                sums={a:sum(r[metric] for r in pair[a]) for a in rows}
                ds={a:sum(r[denominator] for r in pair[a]) for a in rows}
                if not all(ds.values()):return None
                base+=sums['baseline'];trial+=sums['trial']/ds['trial']*ds['baseline']
            return (trial/base-1) if base else None
        main=standardized('reads','impressions' if exposure else 'posts')
        common=[k for k in ('likes','favorites','shares') if all(total(a,k) is not None for a in rows)]
        interactions={}
        for k in common:
            value=standardized(k,'reads')
            if value is not None:interactions[k]=value
            elif total('trial',k)==0 and total('baseline',k)==0:interactions[k]=0
        return {'primary_lift':main,'interaction_lifts':interactions,'interaction_metric':'per_1000_reads','interaction_ok':bool(interactions) and all(v>=-.1-1e-9 for v in interactions.values()),'metric':'read_rate' if exposure else 'reads_per_post'}

    def _valid_preferences(self,p):
        initial=self._initial()['current']
        if not isinstance(p,dict) or set(p)!=set(initial):return False
        if p.get('headline_angle') not in ANGLES or p.get('opening') not in OPENINGS:return False
        for key,lo,hi in (('target_length',250,600),('news_paragraph_length',60,120)):
            if type(p.get(key)) is not int or not lo<=p[key]<=hi:return False
        for key in ('topic_weights','tool_weights'):
            if not isinstance(p.get(key),dict) or set(p[key])!=set(initial[key]):return False
            if not all(_number(v) and .5<=v<=1.5 for v in p[key].values()):return False
        return True

    def evaluate(self,report,now=None):
        day=_day(report['date'])
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE');s=self._load(db);a=s['active']
            if not a or day<a['started_day']:return s
            invalid=not self._valid_preferences(s['current']) or not self._valid_preferences(s['baseline'])
            if invalid or report.get('quality_alert') is True:
                safe=s['baseline'] if self._valid_preferences(s['baseline']) else self._initial()['baseline']
                s.update(current=copy.deepcopy(safe),baseline=copy.deepcopy(safe),active=None,version=s['version']+1,last_action='rolled_back')
            elif day in a['evaluated_days'] or report.get('complete') is not True:return s
            else:
                comparison=self._comparison(report,a)
                if comparison is None or comparison['primary_lift'] is None:return s
                a['evaluated_days'].append(day);a['evaluated_days']=a['evaluated_days'][-31:];a['comparison']=comparison
                if comparison['primary_lift']<=-.2+1e-9:
                    s.update(current=copy.deepcopy(s['baseline']),active=None,version=s['version']+1,last_action='rolled_back')
                else:
                    a['streak']=a['streak']+1 if comparison['primary_lift']>=.1-1e-9 and comparison['interaction_ok'] else 0
                    if a['streak']>=2:
                        s.update(rollback_baseline=copy.deepcopy(s['baseline']),rollback_version=a['baseline_version'],baseline=copy.deepcopy(s['current']),active=None,version=s['version']+1,last_action='promoted')
            self._save(db,s,day);return s

    def context_for_intent(self,kind,intent_key,day):
        if kind not in KINDS:raise ValueError('未知内容类型')
        day=_day(day)
        identity=hashlib.sha256(json.dumps([day,kind,str(intent_key)],ensure_ascii=False).encode()).hexdigest()
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT payload FROM editorial_intents WHERE key=?',(identity,)).fetchone()
            if row:return json.loads(row[0])
            s=self._load(db);a=s['active']
            self._prune(db,s,day)
            trial=bool(a and int(hashlib.sha256((a['id']+identity).encode()).hexdigest(),16)%100<20)
            context={'intent_id':identity,'experiment_id':a['id'] if a else None,'arm':'trial' if trial else 'baseline','policy_version':(a['trial_version'] if trial else a['baseline_version']) if a else s['version'],'content_kind':kind,'day':day,'preferences':copy.deepcopy(s['current'] if trial else s['baseline'])}
            db.execute('INSERT INTO editorial_intents VALUES (?,?,?)',(identity,day,self.store.dump(context)))
            return context
