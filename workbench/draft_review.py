"""Application-generated evidence approvals, never executable source instructions."""
import hashlib
import json
from copy import deepcopy
from datetime import datetime, timedelta

FORMAT_CONCERNS={'正文需要连续编号的功能或步骤','标题数字与实际有效条数不符',
                 '标题、条目、段落和标签之间需要空一行','结尾必须只保留3～5个相关话题标签',
                 '单段超过180字，请缩短或按语义分段','标题过长，请精简到52字以内'}


def _hash(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def draft_hash(draft):
    return _hash({k:draft.get(k) for k in ('title','body','sources','verify')})


def source_hash(sources):
    fields=('url','kind','title','author','published_at','body')
    return _hash(sorted(({k:s.get(k) for k in fields} for s in sources),key=lambda s:s['url']))


def resolve_concerns(draft,sources,result):
    concerns=draft.get('verify') or []
    if not concerns:return []
    rows=result.get('resolutions')
    if not isinstance(rows,list) or len(rows)!=len(concerns):raise ValueError('核实项未逐项裁决')
    evidence={s['url']:s.get('body','') for s in sources}
    seen=set()
    for row in rows:
        if not isinstance(row,dict):raise ValueError('裁决证据格式错误')
        index=row.get('issue_index')
        if type(index) is not int or not 0<=index<len(concerns) or index in seen:raise ValueError('裁决索引无效')
        seen.add(index)
        kind=row.get('kind')
        if kind=='format_corrected' and concerns[index] not in FORMAT_CONCERNS:
            raise ValueError('事实疑点不能作为排版问题解除')
        quote=row.get('body_quote','')
        if (row.get('decision')!='resolved' or kind not in {'supported_by_source','not_claimed','forecast_or_plan','format_corrected'}
                or not isinstance(row.get('reason'),str) or len(row['reason'].strip())<4
                or not isinstance(quote,str) or len(quote.strip())<6 or quote not in draft.get('body','')):
            raise ValueError('核实项尚未解决或正文引文无效')
        if kind!='format_corrected':
            source_quote=row.get('source_quote','')
            if (row.get('source_url') not in evidence or not isinstance(source_quote,str)
                    or len(source_quote.strip())<6 or source_quote not in evidence[row['source_url']]):
                raise ValueError('核实项缺少当前来源的逐字证据')
    return deepcopy(rows)


def approve(draft,sources,now,policy):
    if draft.get('verify') or not sources:raise ValueError('不能批准待核实或无来源稿件')
    result=deepcopy(draft)
    result['source_snapshots']=deepcopy(sources)
    result['review_approval']={'version':1,'draft_hash':draft_hash(draft),'source_hash':source_hash(sources),
                               'at':now.isoformat(),'policy':policy}
    return result


def approval_matches(draft,sources,now,policy):
    proof=draft.get('review_approval') or {}
    try:
        age=now-datetime.fromisoformat(proof['at'])
        return bool(not draft.get('verify') and proof.get('version')==1 and proof.get('policy')==policy
                    and timedelta(0)<=age<=timedelta(hours=3) and proof.get('draft_hash')==draft_hash(draft)
                    and proof.get('source_hash')==source_hash(sources))
    except (KeyError,ValueError,TypeError):return False
