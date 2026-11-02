"""Deterministic, content-free daily review summaries (China publication day)."""
import math
import re
from collections import defaultdict
from datetime import date, datetime
from zoneinfo import ZoneInfo

METRICS=('impressions','reads','likes','comments','favorites','shares','follows','revenue')
TOTALS=METRICS[:6]
ZONE=ZoneInfo('Asia/Shanghai')
SUPPORTED_SCOPES=frozenset(('cumulative','current_snapshot'))


def verified_sample(row):
    field='display_fields_verified' if row.get('scope')=='current_snapshot' else 'list_drawer_match'
    return all((row.get('verification') or {}).get(k) is True for k in (field,'time_match','public_id_verified'))


def number(value):
    return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value) and value >= 0


def timestamp(value):
    try:
        result=datetime.fromisoformat(value)
        return result.astimezone(ZONE) if result.tzinfo and result.utcoffset() is not None else None
    except (TypeError,ValueError):return None


def _summary(rows):
    coverage={}
    totals={}
    for key in METRICS:
        values=[r.get('metrics',{}).get(key) for r in rows]
        known=[v for v in values if number(v)]
        coverage[key]={'known':len(known),'total':len(rows),'known_sum':sum(known) if known else None}
        totals[key]=sum(known) if rows and len(known)==len(rows) else None
    return totals,coverage


def title_traits(title):
    title=title if isinstance(title,str) else ''
    return {'title_length_band':'0-15' if len(title)<=15 else ('16-30' if len(title)<=30 else '31+'),
            'question':bool(re.search(r'[?？]',title)), 'number':bool(re.search(r'\d',title)),
            'contrast':any(t in title for t in ('但是','不过','然而','却','但'))}


def age_band(hours):
    for lo,hi in ((6,12),(12,18),(18,24),(24,48),(48,72)):
        if lo<=hours<hi:return f'{lo}-{hi}'
    return None


def _build_day_report(target_date,rows,complete,capabilities=None,warnings=None):
    """Unknowns stay null; collection failures never imply bad content quality.

    quality_alert is reserved for a separate, explicit content-quality judgment.
    This metrics-only function has no evidence to assert it.
    """
    day=date.fromisoformat(target_date).isoformat()
    published=[];seen=set();bad=False
    warning_codes=set()
    if complete is not True:warning_codes.add('collection_incomplete')
    if warnings:warning_codes.add('collector_warning')
    for row in rows:
        identity=row.get('platform_id')
        if not isinstance(identity,str) or not identity or identity in seen:
            bad=True;warning_codes.add('invalid_row_identity');continue
        seen.add(identity)
        when=timestamp(row.get('published_at'))
        observed=timestamp(row.get('observed_at'))
        if when is None or observed is None or observed<when:
            bad=True;warning_codes.add('invalid_timestamp');continue
        if when.date().isoformat()!=day:continue
        if row.get('status')!='published':
            if row.get('status') not in ('submitted','rejected'):
                bad=True;warning_codes.add('uncertain_status')
            continue
        metrics=row.get('metrics',{})
        if any(value is not None and not number(value) for key,value in metrics.items() if key in METRICS):
            bad=True;warning_codes.add('invalid_metric')
        published.append(row)
    totals,coverage=_summary(published)
    cohort_rows=defaultdict(list);trait_rows=defaultdict(list)
    for row in published:
        when=timestamp(row['published_at']);observed=timestamp(row['observed_at'])
        band=age_band((observed-when).total_seconds()/3600)
        period=f'{when.hour//6*6:02d}-{when.hour//6*6+6:02d}'
        scope=row.get('scope') if row.get('scope') in SUPPORTED_SCOPES else 'unverified'
        if scope not in SUPPORTED_SCOPES:
            bad=True;warning_codes.add('unverified_scope')
        traits=title_traits(row.get('title'))
        trait_key=(row.get('content_kind') if row.get('content_kind') in ('news','tool-guide') else 'unknown',band,period,scope,*traits.values())
        trait_rows[trait_key].append(row)
        context=row.get('editorial_context')
        if not isinstance(context,dict) or not band:continue
        kind=context.get('content_kind')
        experiment=context.get('experiment_id')
        if kind not in ('news','tool-guide') or kind!=row.get('content_kind') or context.get('arm') not in ('baseline','trial') or not isinstance(experiment,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',experiment):continue
        cohort_rows[(experiment,context['arm'],kind,band,period,scope)].append(row)
    cohorts=[]
    for (experiment,arm,kind,band,period,scope),group in cohort_rows.items():
        sums,cov=_summary(group)
        cohorts.append({'experiment_id':experiment,'arm':arm,'content_kind':kind,'age_band':band,'publication_band':period,'publication_date':day,'scope':scope,'posts':len(group),**{k:sums[k] for k in TOTALS},'metric_coverage':cov})
    traits=[]
    for key,group in trait_rows.items():
        kind,band,period,scope,length,question,digit,contrast=key
        sums,cov=_summary(group)
        traits.append({'content_kind':kind,'age_band':band,'publication_band':period,'scope':scope,'title_length_band':length,'question':question,'number':digit,'contrast':contrast,'posts':len(group),'totals':{k:sums[k] for k in TOTALS},'metric_coverage':cov})
    ratios={}
    scopes={r.get('scope') for r in published}
    comparable=bool(published) and len(scopes)==1 and scopes<=SUPPORTED_SCOPES
    for name,numerator,denominator in (('read_rate','reads','impressions'),('like_rate','likes','reads'),('comment_rate','comments','reads'),('favorite_rate','favorites','reads'),('share_rate','shares','reads')):
        ratios[name]=totals[numerator]/totals[denominator] if comparable and totals[numerator] is not None and totals[denominator] is not None and totals[denominator]>0 else None
    capability={k:(capabilities or {}).get(k) if isinstance((capabilities or {}).get(k),bool) else None for k in METRICS}
    observed=[timestamp(r['observed_at']) for r in published]
    as_of={'from':min(observed).isoformat(),'to':max(observed).isoformat()} if observed else None
    return {'date':day,'data_basis':'per_post_display_at_collection','as_of':as_of,'complete':complete is True and not bad,'sample_count':coverage['reads']['known'],'post_count':len(published),'totals':{k:totals[k] for k in TOTALS},'metric_coverage':coverage,'cohorts':cohorts,'traits':traits,'ratios':ratios,'capabilities':capability,'warnings':sorted(warning_codes),'warning_count':len(warning_codes),'quality_alert':False}


def build_report(target_date,rows,complete,capabilities=None,warnings=None):
    """Summarize the target day and separately retain eligible mature snapshots.

    Older snapshots remain grouped by their own publication date. Only validated,
    supported observations younger than 72 hours enter mature comparisons.
    """
    day=date.fromisoformat(target_date).isoformat()
    report=_build_day_report(day,rows,complete,capabilities,warnings)
    earlier=defaultdict(list)
    rejected=False
    for row in rows:
        when=timestamp(row.get('published_at'))
        if when is None or when.date().isoformat()>=day:continue
        observed=timestamp(row.get('observed_at'))
        if observed is None or observed<when:
            rejected=True;continue
        if (observed-when).total_seconds()>=72*3600:continue
        if row.get('scope') not in SUPPORTED_SCOPES:
            rejected=True;continue
        if row.get('status')!='published':
            if row.get('status') not in ('submitted','rejected'):rejected=True
            continue
        metrics=row.get('metrics',{})
        if any(value is not None and not number(value) for key,value in metrics.items() if key in METRICS):
            rejected=True;continue
        earlier[when.date().isoformat()].append(row)
    mature=[_build_day_report(prior,group,complete,capabilities,warnings) for prior,group in sorted(earlier.items())]
    report['mature']=mature
    # Preserve readable cohorts for analysis; complete=False blocks policy action.
    for prior in mature:
        report['cohorts'].extend(prior['cohorts'])
    if rejected or any(not prior['complete'] for prior in mature):
        report['complete']=False
        report['warnings']=sorted(set(report['warnings'])|{'mature_collection_incomplete'})
        report['warning_count']=len(report['warnings'])
    return report


def local_markdown(target_date,rows):
    """Detailed text stays in the local, short-lived report directory."""
    valid=[r for r in rows if timestamp(r.get('published_at')) and timestamp(r['published_at']).date().isoformat()==target_date]
    valid.sort(key=lambda r: r.get('metrics',{}).get('reads') if number(r.get('metrics',{}).get('reads')) else -1,reverse=True)
    headings=['排名','标题','阅读','展现','点赞','评论','收藏','分享','转粉','收益(元)','状态','发布时间','口径','采集时间','平台ID','来源']
    lines=[f'# {target_date} 本地逐帖复盘','','统计值截至各帖的采集时间，不代表目标自然日新增。','', '| '+' | '.join(headings)+' |','| '+' | '.join(['---']*len(headings))+' |']
    def cell(value):
        return str(value if value is not None else '未知').replace('|','\\|').replace('\n',' ').replace('\r',' ')
    for index,r in enumerate(valid,1):
        values=[r.get('metrics',{}).get(k) if number(r.get('metrics',{}).get(k)) else None for k in ('reads','impressions','likes','comments','favorites','shares','follows','revenue')]
        details=[r.get(k) for k in ('status','published_at','scope','observed_at','platform_id','source_url')]
        lines.append('| '+' | '.join(cell(value) for value in [index,r.get('title'),*values,*details])+' |')
    return '\n'.join(lines)+'\n'
