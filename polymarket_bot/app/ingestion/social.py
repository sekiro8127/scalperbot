"""
SENTIMENT — cached, non-blocking, graceful degradation.
NewsAPI first, Reddit JSON fallback.
"""
import os, time, requests
from textblob import TextBlob

NEWSAPI_KEY = os.getenv("NEWSAPI_KEY", "")
_cache      = {}
CACHE_TTL   = 300


def get_sentiment(query: str) -> float:
    if not query:
        return 0.0
    key = query[:40]
    now = time.time()
    if key in _cache and (now - _cache[key][0]) < CACHE_TTL:
        return _cache[key][1]

    texts = []
    if NEWSAPI_KEY:
        try:
            url  = (f"https://newsapi.org/v2/everything?q={query}"
                    f"&pageSize=5&sortBy=publishedAt&apiKey={NEWSAPI_KEY}")
            data = requests.get(url, timeout=8).json()
            texts = [a["description"] for a in data.get("articles", [])
                     if a.get("description")]
        except Exception:
            pass

    if not texts:
        try:
            url     = f"https://www.reddit.com/search.json?q={query}&sort=new&limit=8"
            headers = {"User-Agent": "polymarket-bot/2.0"}
            data    = requests.get(url, headers=headers, timeout=8).json()
            for post in data.get("data", {}).get("children", []):
                t = post["data"].get("title", "")
                if t:
                    texts.append(t)
        except Exception:
            pass

    score = round(sum(TextBlob(t).sentiment.polarity for t in texts) / len(texts), 4) if texts else 0.0
    _cache[key] = (now, score)
    return score
