"""
FAIR VALUE ENGINE v3.1
========================
FV uses up to FOUR independent sources, weighted by availability:

  SOURCE 1 (35%): Resolution probability from external data (NewsAPI)
  SOURCE 2 (35%): Informed VWAP — VWAP of large trades only
  SOURCE 3 (25%): TWAP — exponentially weighted price history
  SOURCE 4 (15%): Orderbook mid (small anchor only)

Bug fix from v3:
- ob_mid was an over-weighted anchor (25–45 %) which made FV ≈ price and
  deviation ≈ 0 in the common case (no NEWSAPI_KEY, no trade ingest).
  ob_mid is now capped at 15 % weight and TWAP is treated as the primary
  fallback. We also surface a non-zero `direction` from a much smaller
  deviation threshold so the entry-signal layer can actually fire.
"""
import os
import time
import statistics
import logging
import requests
from collections import defaultdict, deque
from textblob import TextBlob

logger      = logging.getLogger(__name__)
NEWSAPI_KEY = os.getenv("NEWSAPI_KEY", "")
SESSION     = requests.Session()

_price_history    = defaultdict(lambda: deque(maxlen=240))   # ~longer for TWAP
_trade_history    = defaultdict(lambda: deque(maxlen=120))
_sentiment_cache  = {}
_fv_cache         = {}
FV_CACHE_TTL      = 6
SENTIMENT_TTL     = 300

# Direction thresholds — tuned much smaller so deviation actually emits a signal.
DIR_DEV_THRESH    = 0.008    # was 0.025  → was unreachable in practice
OVX_Z_THRESH      = 1.6      # was 1.8


# ── Data ingestion ────────────────────────────────────────────────────────────

def record_price(token_id: str, price: float, ts: float = None):
    _price_history[token_id].append((ts or time.time(), float(price)))


def record_trade(token_id: str, price: float, size: float, ts: float = None):
    _trade_history[token_id].append((ts or time.time(), float(price), float(size)))


# ── SOURCE 1: External resolution probability ─────────────────────────────────

def _resolution_probability(question: str) -> float:
    if not NEWSAPI_KEY:
        return 0.5

    key = question[:40]
    now = time.time()
    cached = _sentiment_cache.get(key)
    if cached and (now - cached[0]) < SENTIMENT_TTL:
        return cached[1]

    prob = 0.5
    try:
        url  = (
            f"https://newsapi.org/v2/everything"
            f"?q={question[:60]}&pageSize=8&sortBy=publishedAt"
            f"&apiKey={NEWSAPI_KEY}"
        )
        data = SESSION.get(url, timeout=8).json()
        texts = [
            a["description"]
            for a in data.get("articles", [])
            if a.get("description")
        ]
        if texts:
            scores = [TextBlob(t).sentiment.polarity for t in texts]
            avg    = sum(scores) / len(scores)
            prob   = max(0.15, min(0.85, 0.5 + avg * 0.35))
    except Exception as e:
        logger.debug(f"Sentiment fetch failed: {e}")

    _sentiment_cache[key] = (now, prob)
    return prob


# ── SOURCE 2: Informed VWAP ───────────────────────────────────────────────────

def _informed_vwap(token_id: str, window: int = 600) -> float | None:
    trades = list(_trade_history[token_id])
    if not trades:
        return None

    now    = time.time()
    cutoff = now - window
    recent = [(p, sz) for ts, p, sz in trades if ts >= cutoff]
    if not recent:
        return None

    sizes = sorted(sz for _, sz in recent)
    p75   = sizes[max(0, int(len(sizes) * 0.75) - 1)] if sizes else 0
    large = [(p, sz) for p, sz in recent if sz >= max(p75, 50)]

    use   = large if large else recent
    total_sz = sum(sz for _, sz in use)
    if total_sz < 1e-9:
        return None

    return sum(p * sz for p, sz in use) / total_sz


# ── SOURCE 3: TWAP ────────────────────────────────────────────────────────────

def _twap(token_id: str, window: int = 600) -> float | None:
    """Exponentially weighted moving price. Now the primary anchor when there
    is no external data — must be allowed to drift away from current_price."""
    hist   = _price_history[token_id]
    if not hist:
        return None
    now    = time.time()
    cutoff = now - window
    decay  = 0.97
    ws = ps = 0.0
    n  = 0
    for i, (ts, p) in enumerate(hist):
        if ts < cutoff:
            continue
        w   = decay ** (len(hist) - 1 - i)
        ps += w * p
        ws += w
        n  += 1
    if ws <= 1e-9 or n < 3:
        return None
    return ps / ws


# ── Z-score bands ─────────────────────────────────────────────────────────────

def _mean_bands(token_id: str, n: int = 30):
    hist = list(_price_history[token_id])
    if len(hist) < 3:
        return None, 0.0
    prices = [p for _, p in hist[-n:]]
    mu     = statistics.mean(prices)
    sd     = statistics.stdev(prices) if len(prices) > 1 else 0.0
    z      = (prices[-1] - mu) / sd if sd > 0.002 else 0.0
    return mu, z


# ── Master FV function ────────────────────────────────────────────────────────

def compute_fair_value(
    token_id:      str,
    current_price: float,
    ob_mid:        float | None,
    ob_imbalance:  float = 0.0,
    question:      str   = "",
    end_date_ts:   float | None = None,
) -> dict:

    now    = time.time()
    cached = _fv_cache.get(token_id)
    if cached and (now - cached["ts"]) < FV_CACHE_TTL:
        return cached

    history_len = len(_price_history[token_id])

    # SOURCE 1
    res_prob = _resolution_probability(question) if question else 0.5
    has_ext  = (NEWSAPI_KEY != "" and question != "")

    # SOURCE 2 / 3
    inf_vwap = _informed_vwap(token_id, 600)
    twap     = _twap(token_id, 600)

    # SOURCE 4 — small anchor only (was too dominant in v3)
    ob_anchor = ob_mid if ob_mid is not None else current_price
    ob_adj    = max(0.02, min(0.98, ob_anchor + ob_imbalance * 0.02))

    components, weights = [], []
    if has_ext:
        components.append(res_prob);  weights.append(0.35)
    if inf_vwap is not None:
        components.append(inf_vwap);  weights.append(0.35 if has_ext else 0.55)
    if twap is not None:
        # TWAP is the workhorse when no external data
        w = 0.20 if has_ext else (0.55 if inf_vwap is None else 0.30)
        components.append(twap);      weights.append(w)
    # ob_mid: small anchor (was 25–45 %, now ≤15 %)
    components.append(ob_adj)
    weights.append(0.10 if (twap is not None or inf_vwap is not None or has_ext) else 0.40)

    total_w = sum(weights) or 1.0
    fv      = sum(c * w for c, w in zip(components, weights)) / total_w
    fv      = max(0.02, min(0.98, fv))

    # Resolution bias near expiry
    if end_date_ts:
        hrs = (end_date_ts - now) / 3600
        if 0 < hrs < 24:
            bias = 0.03 * (1 - hrs / 24)
            if   fv > 0.60: fv = min(0.98, fv + bias)
            elif fv < 0.40: fv = max(0.02, fv - bias)

    _, z_score   = _mean_bands(token_id, 30)
    deviation    = current_price - fv
    overextended = abs(z_score) > OVX_Z_THRESH

    # Confidence
    data_conf   = min(1.0, history_len / 10)
    consistency = 1.0
    if len(components) >= 2:
        span        = max(components) - min(components)
        consistency = max(0.0, 1 - span / 0.15)
    confidence  = round(min(1.0, data_conf * consistency), 3)

    # Direction — much smaller deviation gate so the edge signal can fire
    if overextended and deviation > 0:
        direction = -1
    elif overextended and deviation < 0:
        direction = 1
    elif abs(deviation) > DIR_DEV_THRESH:
        direction = -1 if deviation > 0 else 1
    else:
        direction = 0

    result = {
        "fv":            round(fv,          4),
        "confidence":    confidence,
        "deviation":     round(deviation,   4),
        "z_score":       round(z_score,     3),
        "overextended":  overextended,
        "direction":     direction,
        "history_len":   history_len,
        "res_prob":      round(res_prob,    4),
        "inf_vwap":      round(inf_vwap, 4) if inf_vwap else None,
        "twap":          round(twap,     4) if twap     else None,
        "has_ext_data":  has_ext,
        "ts":            now,
    }
    _fv_cache[token_id] = result
    return result
