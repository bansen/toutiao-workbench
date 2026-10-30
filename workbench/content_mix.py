"""Persisted editorial allocation; never changes a schedule or submits a post."""
from copy import deepcopy

NEWS = 'news'
GUIDE = 'tool-guide'


def content_kind(draft):
    return NEWS if any(s.get('kind') in {'article', 'micro'} for s in draft.get('sources', [])) else GUIDE


def assign_content_mix(plan, fixed=None):
    """Reserve ceil(N/2) news slots; preserve occupied snapshots on upgrade."""
    if plan.get('content_mix', {}).get('version') == 1:
        return deepcopy(plan)
    result = deepcopy(plan)
    fixed = fixed or {}
    remaining = {NEWS: (plan['target']+1)//2, GUIDE: plan['target']//2}
    for slot in result['slots']:
        kind = fixed.get(slot['id'])
        if kind in remaining:
            slot['content_type'] = kind
            remaining[kind] -= 1
    for slot in result['slots']:
        if slot['id'] in fixed:
            continue
        kind = NEWS if remaining[NEWS] >= remaining[GUIDE] else GUIDE
        slot['content_type'] = kind
        remaining[kind] -= 1
    result['content_mix'] = {'version': 1, 'news_ratio': .5,
                            NEWS: sum(s['content_type'] == NEWS for s in result['slots']),
                            GUIDE: sum(s['content_type'] == GUIDE for s in result['slots'])}
    return result
