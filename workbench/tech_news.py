"""Bounded discovery of recent technology news using rendered search cards."""
from datetime import datetime, timedelta

from .ai_tools import NEWS_KEYWORDS
from .parsing import canonical_url


class TechNewsResearch:
    def __init__(self, store, search):
        self.store, self.search = store, search

    async def discover(self, exclude, now, limit=10):
        cursor = int(self.store.setting('tech_news_cursor', 0)) % len(NEWS_KEYWORDS)
        found, seen = [], set(exclude)
        for offset in range(len(NEWS_KEYWORDS)):
            if len(found) >= limit:
                break
            index = (cursor+offset) % len(NEWS_KEYWORDS)
            word = NEWS_KEYWORDS[index]
            self.store.set_setting('tech_news_cursor', (index+1) % len(NEWS_KEYWORDS))
            try:
                cards = await self.search(word, 'article', min(2, limit-len(found)), sorted(seen), now-timedelta(hours=24))
            except ValueError:
                continue
            for card in cards[:2]:
                if len(found) >= limit:
                    break
                try:
                    url = canonical_url(card.get('url'))
                except ValueError:
                    continue
                if url in seen:
                    continue
                # Unknown list timestamps must be verified on the detail page.
                raw = card.get('published_at')
                if raw:
                    try:
                        age = now-datetime.fromisoformat(raw)
                        if not timedelta(0) <= age <= timedelta(hours=24):
                            continue
                    except (ValueError, TypeError):
                        continue
                seen.add(url)
                found.append(('科技', 'article', {**card, 'url':url}))
        return found


def mix_candidates(guides, news):
    """Prefer guides, interleave news; use either when the other is unavailable."""
    guides, news = list(guides), list(news)
    result = []
    while guides or news:
        if news and (not guides or len(result) % 3 == 1):
            result.append(news.pop(0))
        elif guides:
            result.append(guides.pop(0))
        else:
            result.append(news.pop(0))
    return result
