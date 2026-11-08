"""Offline daily plans; no timers, persistence, or publication side effects.

A date requests a complete day. An aware datetime also supplies the clock used
for marking elapsed slots skipped. Callers persist the returned JSON-ready plan
and must not regenerate it during the day.
"""

from copy import deepcopy
from datetime import date, datetime, time, timedelta
from random import Random
from zoneinfo import ZoneInfo
from .content_mix import assign_content_mix


TIMEZONE = ZoneInfo('Asia/Shanghai')
MIN_DAILY_TARGET=10
MAX_DAILY_TARGET=15
PUBLISH_START_HOUR=7
PUBLISH_END_HOUR=23


def add_catchup_slots(plan, now, blocked_times=(), rng=None, *, protected_ids=()):
    """Append durable same-day replacement slots; never revive an old queue."""
    clock=_local_clock(now)
    result=deepcopy(plan)
    if result['date']!=clock.date().isoformat() or clock.hour>=PUBLISH_END_HOUR:return result
    from random import SystemRandom
    rng=rng or SystemRandom()
    blocked_times=tuple(_local_clock(t) for t in blocked_times)
    protected_ids=set(protected_ids)
    separation=timedelta(minutes=16)  # one-minute allowance for actual submit latency
    end=datetime.combine(clock.date(),time(PUBLISH_END_HOUR),tzinfo=TIMEZONE)
    earliest=max(clock+timedelta(minutes=10),datetime.combine(clock.date(),time(PUBLISH_START_HOUR),tzinfo=TIMEZONE))
    active={'pending','queued','awaiting_confirmation','publishing','submitted','published','completed','uncertain','rejected'}
    fixed=[_slot_time(s) for s in result['slots'] if s['status'] in active]
    used=sum(s['status'] in active for s in result['slots'])
    available=max(0,result['target']-used)
    occupied=sorted(set(fixed+list(blocked_times)))
    candidates=[s for s in result['slots'] if s['status']=='skipped' and _slot_time(s)<clock
        and s['id'] not in protected_ids and not s.get('catchup_slot_id') and not any(v in s.get('reason','') for v in
            ('人工拒绝','旧内容定位','旧定位','旧稿不再发布','草稿已编辑'))]
    def fit(times,count=1):
        cursor=earliest
        for boundary in [*times,end+separation-timedelta(microseconds=1)]:
            upper=min(boundary-separation,end-timedelta(microseconds=1))
            slack=(upper-cursor-(count-1)*separation).total_seconds()
            if slack>=0:
                start=cursor+timedelta(seconds=rng.uniform(0,min(120,slack)))
                return [start+i*separation for i in range(count)]
            cursor=max(cursor,boundary+separation)
        return []
    def append_child(parent,at,adjusted=False):
        child={'id':parent['id']+':catchup','scheduled_at':at.isoformat(),'status':'pending',
               'content_type':parent.get('content_type'),'catchup_for':parent['id'],
               'catchup_root':parent.get('catchup_root',parent['id'])}
        if parent.get('quota_increase'):child['quota_increase']=True
        missed=not parent.get('quota_increase') and (not adjusted or parent.get('catchup_missed')
            or (parent.get('catchup_for') and not parent.get('adjusted_for_catchup')))
        if missed:child['catchup_missed']=True
        if adjusted:
            parent.update(status='skipped',reason='为补发调整未备稿排期')
            if not missed:child['adjusted_for_catchup']=True
        parent['catchup_slot_id']=child['id']
        result['slots'].append(child)
        return child
    for parent in sorted(candidates,key=lambda s:s['scheduled_at']):
        if available<=0:break
        positions=fit(occupied)
        if not positions:
            movable=[s for s in sorted(result['slots'],key=lambda s:s['scheduled_at'])
                if s['status']=='pending' and not s.get('queue_id') and s['id'] not in protected_ids
                and _slot_time(s)>clock and _slot_time(s) not in blocked_times]
            for size in range(1,len(movable)+1):
                for offset in range(len(movable)-size+1):
                    group=movable[offset:offset+size]
                    removed={_slot_time(s) for s in group}
                    remaining=[t for t in occupied if t not in removed]
                    positions=fit(remaining,size+1)
                    if positions:
                        for original,at in zip(group,positions[1:]):append_child(original,at,adjusted=True)
                        occupied=sorted([*remaining,*positions[1:]])
                        break
                if positions:break
        if not positions:break
        append_child(parent,positions[0])
        occupied=sorted([*occupied,positions[0]]);available-=1
    result['slots'].sort(key=lambda s:s['scheduled_at'])
    return result


def _local_clock(now: datetime) -> datetime:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('The scheduling clock must be timezone-aware')
    return now.astimezone(TIMEZONE)


def _slot_time(slot: dict) -> datetime:
    return _local_clock(datetime.fromisoformat(slot['scheduled_at']))


def generate_daily_plan(day_or_now: date | datetime, rng: Random) -> dict:
    """Draw 10–15 slots between 07:00 and 23:00 in Asia/Shanghai.

    Each equal stratum draws within its inner 10%. Even at 45 slots,
    adjacent extreme draws remain at least 57.6 minutes apart.
    Passing an explicit RNG makes the plan reproducible without global state.
    """
    now = _local_clock(day_or_now) if isinstance(day_or_now, datetime) else None
    day = now.date() if now is not None else day_or_now
    target = rng.randint(MIN_DAILY_TARGET, MAX_DAILY_TARGET)
    start = datetime.combine(day, time(PUBLISH_START_HOUR), tzinfo=TIMEZONE)
    width = timedelta(hours=PUBLISH_END_HOUR-PUBLISH_START_HOUR) / target
    slots = []
    for index in range(target):
        scheduled = start + width * (index + rng.uniform(.45, .55))
        slots.append({
            'id': f'{day.isoformat()}:{index + 1:02d}',
            'scheduled_at': scheduled.isoformat(),
            'status': 'skipped' if now is not None and scheduled < now else 'pending',
        })
    return assign_content_mix({'date': day.isoformat(), 'timezone': TIMEZONE.key, 'target': target, 'slots': slots})


def expire_missed_slots(plan: dict, now: datetime) -> dict:
    """Return a copy with elapsed pending slots skipped, never rescheduled.

    A slot exactly at ``now`` remains eligible. Already skipped slots stay
    skipped even if the wall clock moves backward.
    """
    clock = _local_clock(now)
    updated = deepcopy(plan)
    for slot in updated['slots']:
        if slot['status'] == 'pending' and _slot_time(slot) < clock:
            slot['status'] = 'skipped'
    return updated


def next_scheduled_at(plan: dict, now: datetime) -> datetime | None:
    """Return the next pending time at or after now; never catch up past slots.

    This read-only helper does not change statuses. Persist the result of
    ``expire_missed_slots`` when advancing the plan to make expiry permanent.
    """
    clock = _local_clock(now)
    return min((scheduled for slot in plan['slots']
                if slot['status'] == 'pending'
                and (scheduled := _slot_time(slot)) >= clock), default=None)
