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


def _local_clock(now: datetime) -> datetime:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('The scheduling clock must be timezone-aware')
    return now.astimezone(TIMEZONE)


def _slot_time(slot: dict) -> datetime:
    return _local_clock(datetime.fromisoformat(slot['scheduled_at']))


def generate_daily_plan(day_or_now: date | datetime, rng: Random) -> dict:
    """Draw 24–30 slots between 08:00 and 22:00 in Asia/Shanghai.

    Each equal stratum draws within its inner 40%. The shortest stratum is
    28 minutes, so even adjacent extreme draws are at least 16.8 minutes apart.
    Passing an explicit RNG makes the plan reproducible without global state.
    """
    now = _local_clock(day_or_now) if isinstance(day_or_now, datetime) else None
    day = now.date() if now is not None else day_or_now
    target = rng.randint(24, 30)
    start = datetime.combine(day, time(8), tzinfo=TIMEZONE)
    width = timedelta(hours=14) / target
    slots = []
    for index in range(target):
        scheduled = start + width * (index + rng.uniform(.3, .7))
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
