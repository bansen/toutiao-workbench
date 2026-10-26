import re
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Hong_Kong')


def now_iso():
    return datetime.now(TZ).isoformat(timespec='seconds')


def canonical_url(url):
    for _ in range(5):
        p = urlsplit(url)
        if p.scheme not in ('http', 'https') or p.hostname not in ('www.toutiao.com', 'toutiao.com', 'so.toutiao.com', 'sou.toutiao.com') or p.username or p.password or p.port not in (None, 80, 443):
            raise ValueError('仅支持头条文章或微头条链接')
        jump = parse_qs(p.query).get('url')
        if p.path == '/search/jump' and jump:
            url = jump[0]
            continue
        m = re.fullmatch(r'/(article/|w/|a)(\d{10,25})/?', p.path)
        if not m:
            raise ValueError('链接不是可识别的头条内容地址')
        kind, content_id = m.groups()
        # Legacy a-links for articles refer to the same platform content ID.
        if kind == 'a':
            kind = 'article/' if len(content_id) >= 19 else 'w/'
        return f'https://www.toutiao.com/{kind}{content_id}/'
    raise ValueError('链接重定向层级过多')


def parse_count(text):
    if text is None:
        return None
    m = re.fullmatch(r'\s*(\d+(?:[,.]\d+)*)(万|亿)?\s*', str(text))
    if not m:
        return None
    return int(float(m[1].replace(',', '')) * {'万':10000, '亿':100000000, None:1}[m[2]])


def parse_time(text, now=None):
    now = now or datetime.now(TZ)
    text = text.strip()
    full = re.search(r'(20\d{2})[-年/](\d{1,2})[-月/](\d{1,2})日?\s+(\d{1,2}):(\d{2})', text)
    try:
        if full:
            return datetime(*map(int, full.groups()), tzinfo=TZ).isoformat()
        m = re.search(r'(\d+)\s*(分钟|小时|天)前', text)
        if m:
            return (now-timedelta(**{dict(分钟='minutes', 小时='hours', 天='days')[m[2]]:int(m[1])})).isoformat()
        m = re.search(r'(今天|昨天|前天)\s*(\d{1,2}):(\d{2})', text)
        if m:
            return (now-timedelta(days={'今天':0,'昨天':1,'前天':2}[m[1]])).replace(hour=int(m[2]), minute=int(m[3]),second=0,microsecond=0).isoformat()
    except ValueError:
        return None
    return None


def select_items(items, options, now=None):
    now = now or datetime.now(TZ)
    result, unknown = [], 0
    for item in items:
        if options.get('kinds') and item.get('kind') not in options['kinds']:
            continue
        if not options.get('kinds') and options.get('kind') and item.get('kind') != options['kind']:
            continue
        if options.get('query') and options['query'].lower() not in (item.get('title','')+' '+item.get('body','')).lower():
            continue
        reads = item.get('reads')
        if options.get('read_min') is not None or options.get('read_max') is not None:
            if reads is None:
                unknown += 1
                continue
            if options.get('read_min') is not None and reads < options['read_min']:
                continue
            if options.get('read_max') is not None and reads >= options['read_max']:
                continue
        if options.get('hours'):
            when = item.get('published_at')
            if not when:
                unknown += 1
                continue
            age = (now-datetime.fromisoformat(when)).total_seconds()
            if age < 0 or age > options['hours']*3600:
                continue
        result.append(item)
    key = options.get('sort','published_at')
    if key not in ('reads','comments','likes','published_at'):
        key = 'published_at'
    result.sort(key=lambda x:(x.get(key) is not None, x.get(key) or 0), reverse=True)
    return result, unknown
