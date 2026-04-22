"""
FAIR VALUE ENGINE v3
======================
FV now uses THREE independent sources:

  SOURCE 1 (40%): Resolution probability from external data
    - NewsAPI sentiment scored against the market question
    - Adjusts base 50% probability based on bullish/bearish news
    - Only active when NEWSAPI_KEY is set

  SOURCE 2 (35%): Informed VWAP
    - VWAP of large trades only (size > p75 threshold)
    - Large trades = informed flow, small trades = noise
    - Falls back to full VWAP if no large trades

  SOURCE 3 (25%): Orderbook mid + imbalance adjustment
    - (best_bid + best_ask) / 2
    - Shifted by orderbook imbalance signal

FIX from v2: ob_mid is no longer the PRIMARY component.
It had 50% weight before — meaning FV ≈ price and deviation ≈ 0 always.
Now ob_mid is only a minor anchor in SOURCE 3.
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

_price_history    = defaultdict(lambda: deque(maxlen=120))
_trade_history    = defaultdict(lambda: deque(maxlen=60))
_sentiment_cache  = {}
_fv_cache         = {}
FV_CACHE_TTL      = 6
SENTIMENT_TTL     = 300


# ── Data ingestion ────────────────────────────────────────────────────────────

def record_price(token_id: str, price: float, ts: float = None):
    _price_history[token_id].append((ts or time.time(), float(price)))


def record_trade(token_id: str, price: float, size: float, ts: float = None):
    _trade_history[token_id].append((ts or time.time(), float(price), float(size)))


# ── SOURCE 1: External resolution probability ─────────────────────────────────

def _resolution_probability(question: str) -> float:
    """
    Uses NewsAPI to estimate external consensus probability.
    Returns a value in [0.05, 0.95] representing YES probability.
    Returns 0.5 (neutral) if no API key or no data.
    """
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
            # Map sentiment [-1, 1] → probability [0.15, 0.85]
            prob   = max(0.15, min(0.85, 0.5 + avg * 0.35))
    except Exception as e:
        logger.debug(f"Sentiment fetch failed: {e}")

    _sentiment_cache[key] = (now, prob)
    return prob


# ── SOURCE 2: Informed VWAP ───────────────────────────────────────────────────

def _informed_vwap(token_id: str, window: int = 600) -> float | None:
    """VWAP of large trades only — filters out noise from small retail trades."""
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

def _twap(token_id: str, window: int = 300) -> float | None:
    hist   = _price_history[token_id]
    if not hist:
        return None
    now    = time.time()
    cutoff = now - window
    decay  = 0.97
    ws = ps = 0.0
    for i, (ts, p) in enumerate(hist):
        if ts < cutoff:
            continue
        w   = decay ** (len(hist) - 1 - i)
        ps += w * p
        ws += w
    return ps / ws if ws > 1e-9 else None


# ── Z-score bands ─────────────────────────────────────────────────────────────

def _mean_bands(token_id: str, n: int = 20):
    hist = list(_price_history[token_id])
    if len(hist) < 3:
        return None, 0.0
    prices = [p for _, p in hist[-n:]]
    mu     = statistics.mean(prices)
    sd     = statistics.stdev(prices) if len(prices) > 1 else 0.0
    z      = (prices[-1] - mu) / sd if sd > 0.003 else 0.0
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

    # SOURCE 1: External resolution probability
    res_prob   = _resolution_probability(question) if question else 0.5
    has_ext    = (NEWSAPI_KEY != "" and question != "")

    # SOURCE 2: Informed VWAP
    inf_vwap   = _informed_vwap(token_id, 600)
    twap       = _twap(token_id, 300)

    # SOURCE 3: Adjusted OB mid
    ob_anchor  = ob_mid if ob_mid is not None else current_price
    # Shift mid by imbalance: strong buy pressure → price should be slightly higher
    ob_adj     = max(0.02, min(0.98, ob_anchor + ob_imbalance * 0.02))

    # Build weighted FV
    components   = []
    weights_used = []

    if has_ext:
        components.append(res_prob);  weights_used.append(0.40)

    if inf_vwap is not None:
        w = 0.35 if has_ext else 0.55
        components.append(inf_vwap); weights_used.append(w)
    elif twap is not None:
        w = 0.30 if has_ext else 0.50
        components.append(twap);     weights_used.append(w)

    ob_w = 0.25 if has_ext else (0.45 if (inf_vwap is None and twap is None) else 0.20)
    components.append(ob_adj);       weights_used.append(ob_w)

    total_w = sum(weights_used)
    fv      = sum(c * w for c, w in zip(components, weights_used)) / total_w
    fv      = max(0.02, min(0.98, fv))

    # Resolution bias: near-expiry markets polarize toward 0 or 1
    if end_date_ts:
        hrs = (end_date_ts - now) / 3600
        if 0 < hrs < 24:
            bias = 0.03 * (1 - hrs / 24)
            if fv > 0.60:   fv = min(0.98, fv + bias)
            elif fv < 0.40: fv = max(0.02, fv - bias)

    _, z_score   = _mean_bands(token_id, 20)
    deviation    = current_price - fv
    overextended = abs(z_score) > 1.8

    # Confidence: rises with history + signal agreement
    data_conf   = min(1.0, history_len / 10)
    consistency = 1.0
    if len(components) >= 2:
        span        = max(components) - min(components)
        consistency = max(0.0, 1 - span / 0.15)
    confidence  = round(min(1.0, data_conf * consistency), 3)

    if overextended and deviation > 0:
        direction = -1
    elif overextended and deviation < 0:
        direction = 1
    elif abs(deviation) > 0.025:
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
