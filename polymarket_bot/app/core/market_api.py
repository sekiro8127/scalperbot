"""
MARKET SELECTION ENGINE v2 — Multi-factor scoring
===================================================
Scores markets on 4 dimensions:
  1. INEFFICIENCY  — spread vs volatility, price far from VWAP
  2. LIQUIDITY     — enough depth to fill without slippage
  3. RESOLUTION TIMING — 1-6h = best, >24h = skip
  4. RESOLUTION EDGE — Polymarket price vs external consensus (NewsAPI/Reddit)

Markets with the highest combined score have the best expected edge.
"""
import json
import time
import logging
import requests
from datetime import datetime, timezone
from typing import List, Optional

from app.core.websocket import get_live_trade_stats

logger  = logging.getLogger(__name__)
SESSION = requests.Session()
SESSION.headers.update({"Accept": "application/json"})


def _retry_with_backoff(func, retries=3, backoff_in_seconds=1):
    def wrapper(*args, **kwargs):
        x = 0
        while True:
            try:
                return func(*args, **kwargs)
            except requests.exceptions.HTTPError as e:
                # Don't retry on 404 (Not Found)
                if e.response.status_code == 404:
                    raise e
                if x == retries:
                    raise e
                sleep = (backoff_in_seconds * 2 ** x)
                logger.warning(f"Retrying in {sleep}s due to HTTP error {e.response.status_code}: {e}")
                time.sleep(sleep)
                x += 1
            except Exception as e:
                if x == retries:
                    raise e
                sleep = (backoff_in_seconds * 2 ** x)
                logger.warning(f"Retrying in {sleep}s due to error: {e}")
                time.sleep(sleep)
                x += 1
    return wrapper


_cache    = {"markets": [], "ts": 0}
CACHE_TTL = 90

MAX_HOURS_TO_RESOLUTION = 168       # 7 days
MIN_VOLUME              = 10
MIN_DEPTH               = 50        # USDC depth minimum


def _parse_field(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return []
    return v or []


def _clob_book(token_id: str) -> Optional[dict]:
    @_retry_with_backoff
    def fetch():
        r = SESSION.get(
            f"https://clob.polymarket.com/book?token_id={token_id}",
            timeout=6,
        )
        r.raise_for_status()
        return r.json()

    try:
        d = fetch()
        bids = d.get("bids", [])
        asks = d.get("asks", [])
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 404:
            logger.debug(f"CLOB book not found for token_id: {token_id}")
        return None
    except Exception as e:
        logger.error(f"Error fetching CLOB book: {e}")
        return None

    if not bids or not asks:
        return None
    
    bb = float(bids[0]["price"])
    ba = float(asks[0]["price"])
    bv = sum(float(x["size"]) for x in bids[:10])
    av = sum(float(x["size"]) for x in asks[:10])
    return {
        "mid":     (bb + ba) / 2,
        "spread":   ba - bb,
        "bid_vol":  bv,
        "ask_vol":  av,
        "depth":    bv + av,
        "best_bid": bb,
        "best_ask": ba,
    }


def _recent_trade_stats(token_id: str) -> dict:
    # Try live WS history first (zero cost)
    live = get_live_trade_stats(token_id)
    if live.get("count", 0) >= 3 and live.get("range", 0.0) > 0.0:
        return {
            "count":  int(live["count"]),
            "range":  float(live["range"]),
            "change": float(live["change"]),
            "vwap":   None,
        }

    @_retry_with_backoff
    def fetch():
        r = SESSION.get(
            f"https://data-api.polymarket.com/trades?token_id={token_id}&limit=30",
            timeout=6,
        )
        r.raise_for_status()
        return r.json()

    try:
        trades = fetch()
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 404:
            logger.debug(f"Recent trades not found for token_id: {token_id}")
        return {"count": 0, "range": 0.0, "change": 0.0, "vwap": None}
    except Exception as e:
        logger.error(f"Error fetching recent trades: {e}")
        return {"count": 0, "range": 0.0, "change": 0.0, "vwap": None}

    if not isinstance(trades, list) or not trades:
        return {"count": 0, "range": 0.0, "change": 0.0, "vwap": None}

    prices, sizes = [], []
    for t in trades:
        p = t.get("price") or t.get("p")
        s = t.get("usdcSize") or t.get("size") or 0
        if p is not None:
            try:
                prices.append(float(p))
                sizes.append(float(s))
            except Exception:
                continue

    if not prices:
        return {"count": 0, "range": 0.0, "change": 0.0, "vwap": None}

    pr_range  = max(prices) - min(prices)
    pr_change = prices[0] - prices[-1] if len(prices) >= 2 else 0.0
    total_sz  = sum(sizes)
    vwap      = sum(p * s for p, s in zip(prices, sizes)) / total_sz if total_sz else None

    return {
        "count":  len(prices),
        "range":  pr_range,
        "change": pr_change,
        "vwap":   round(vwap, 4) if vwap else None,
    }


def _end_ts(m: dict) -> Optional[float]:
    end = m.get("end_date_iso") or m.get("endDate")
    if not end:
        return None
    # Try different formats
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(end, fmt).replace(tzinfo=timezone.utc).timestamp()
        except Exception:
            pass
    return None


def _score_market(book: dict, tstats: dict, hrs_left: Optional[float],
                  volume: float) -> float:
    """
    Multi-factor score 0–1. Higher = better edge opportunity.

    1. INEFFICIENCY: wide spread relative to recent move
    2. LIQUIDITY: depth sufficient for our trade size
    3. RESOLUTION TIMING: 1-6h = best
    4. ACTIVITY: market is actually moving
    """
    if not book:
        return 0.0

    mid    = book["mid"]
    spread = book["spread"]
    depth  = book["depth"]

    # Hard rejects
    if mid > 0.95 or mid < 0.05:
        logger.debug(f"Market rejected: extreme price {mid}")
        return 0.0
    if spread > 0.20:
        logger.debug(f"Market rejected: wide spread {spread}")
        return 0.0
    if depth < MIN_DEPTH:
        logger.debug(f"Market rejected: low depth {depth} < {MIN_DEPTH}")
        return 0.0
    if hrs_left is not None and hrs_left < 0.5:
        return 0.0  # resolving in 30 min — too late

    # 1. Inefficiency score: spread wide relative to recent range
    trange = tstats.get("range", 0.0)
    if trange > 0:
        ineff = min(1.0, spread / max(trange, 0.005))
    else:
        ineff = 0.3

    # 2. Liquidity score
    liq = min(1.0, depth / 2000)

    # 3. Resolution timing (sweet spot: 1–6h)
    if hrs_left is None:
        timing = 0.4
    elif hrs_left < 1:
        timing = 0.2
    elif hrs_left <= 6:
        timing = 1.0
    elif hrs_left <= 12:
        timing = 0.7
    elif hrs_left <= 24:
        timing = 0.4
    else:
        timing = 0.0

    # 4. Activity score (market must be moving)
    activity = min(1.0, (abs(tstats.get("change", 0.0)) + trange) / 0.05)

    # Weighted composite
    score = (ineff * 0.25 + liq * 0.20 + timing * 0.35 + activity * 0.20)
    return round(score, 4)


def get_markets(top_n: int = 12) -> List[dict]:
    now = time.time()
    if _cache["markets"] and (now - _cache["ts"]) < CACHE_TTL:
        return _cache["markets"]

    url = (
        "https://gamma-api.polymarket.com/markets"
        "?active=true&closed=false&limit=500"
        "&order=volume&ascending=false"
    )

    @_retry_with_backoff
    def fetch():
        r = SESSION.get(url, timeout=15)
        r.raise_for_status()
        return r.json()

    try:
        data = fetch()
    except Exception as e:
        logger.error(f"Market API error: {e}")
        return _cache["markets"]

    now_ts     = datetime.now(timezone.utc).timestamp()
    candidates = []
    skipped    = {"no_resolution": 0, "too_far": 0, "low_vol": 0,
                  "no_book": 0, "low_score": 0, "error": 0}

    logger.info(f"Scanning {len(data)} markets...")

    for m in data:
        try:
            if m.get("closed", True):
                continue

            end_ts   = _end_ts(m)
            if not end_ts:
                # Debug log to see why end_ts is missing
                logger.debug(f"Market missing end_date_iso: {m.get('question')} | Keys: {list(m.keys())}")
            
            hrs_left = (end_ts - now_ts) / 3600 if end_ts else None

            if hrs_left is None or hrs_left > MAX_HOURS_TO_RESOLUTION:
                skipped["too_far"] += 1
                continue

            token_ids = _parse_field(m.get("clobTokenIds"))
            if not token_ids:
                continue
            token_id = token_ids[0]

            volume = float(m.get("volume", 0) or 0)
            if volume < MIN_VOLUME:
                skipped["low_vol"] += 1
                continue

            book   = _clob_book(token_id)
            if not book:
                skipped["no_book"] += 1
                continue

            tstats = _recent_trade_stats(token_id)
            score  = _score_market(book, tstats, hrs_left, volume)

            if score <= 0:
                skipped["low_score"] += 1
                continue

            candidates.append({
                "id":           m["id"],
                "question":     m["question"],
                "token_id":     token_id,
                "price":        book["mid"],
                "spread":       round(book["spread"], 4),
                "depth":        round(book["depth"], 1),
                "imbalance":    round(
                    (book["bid_vol"] - book["ask_vol"]) /
                    (book["bid_vol"] + book["ask_vol"] + 1e-9), 4
                ),
                "score":        score,
                "end_ts":       end_ts,
                "hrs_left":     round(hrs_left, 2) if hrs_left else None,
                "volume":       round(volume, 0),
                "trade_range":  round(tstats.get("range", 0.0), 4),
                "trade_change": round(abs(tstats.get("change", 0.0)), 4),
                "vwap":         tstats.get("vwap"),
            })

            if len(candidates) >= top_n * 4:
                break

        except Exception:
            skipped["error"] += 1
            continue

    # Fallback: if no 24h markets found, relax to any active market
    if not candidates:
        logger.warning("No markets in 24h window — using volume fallback")
        for m in data[:80]:
            try:
                tids = _parse_field(m.get("clobTokenIds"))
                if not tids:
                    continue
                book = _clob_book(tids[0])
                if not book or book["mid"] > 0.95 or book["mid"] < 0.05:
                    continue
                end_ts   = _end_ts(m)
                hrs_left = (end_ts - now_ts) / 3600 if end_ts else None
                candidates.append({
                    "id":           m["id"],
                    "question":     m["question"],
                    "token_id":     tids[0],
                    "price":        book["mid"],
                    "spread":       round(book["spread"], 4),
                    "depth":        round(book["depth"], 1),
                    "imbalance":    0.0,
                    "score":        0.01,
                    "end_ts":       end_ts,
                    "hrs_left":     round(hrs_left, 2) if hrs_left else None,
                    "volume":       float(m.get("volume", 0) or 0),
                    "trade_range":  0.0,
                    "trade_change": 0.0,
                    "vwap":         None,
                })
            except Exception:
                continue

    candidates.sort(key=lambda x: x["score"], reverse=True)
    selected = candidates[:top_n]

    logger.info(f"MARKET SCAN | selected={len(selected)} skipped={skipped}")
    for m in selected:
        hrs = f"{m['hrs_left']:.1f}h" if m["hrs_left"] else "?"
        logger.info(
            f"  [{m['score']:.3f}] {m['question'][:48]:48s} | "
            f"mid={m['price']:.3f} spr={m['spread']:.3f} "
            f"rng={m['trade_range']:.3f} hrs={hrs}"
        )

    _cache["markets"] = selected
    _cache["ts"]      = now
    return selected
