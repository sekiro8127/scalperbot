"""
ORDERBOOK INTELLIGENCE v3 — Polymarket specific
=================================================
Polymarket orderbooks are sparse and thin.
Key signals:
  - Absorption (large size not moving price)
  - Spoofing (huge wall that will likely cancel)
  - Weighted imbalance (proximity-weighted depth)
  - Liquidity thinning (breakout warning)
  - Aggressive vs passive flow
"""
import time
import requests
from collections import defaultdict, deque

SESSION   = requests.Session()
_ob_hist  = defaultdict(lambda: deque(maxlen=30))   # token → [(ts, mid, bv, av)]
_ob_cache = {}   # token → {data, ts}
CACHE_TTL = 5    # seconds


def _fetch_raw(token_id: str) -> dict | None:
    now = time.time()
    c   = _ob_cache.get(token_id)
    if c and (now - c["ts"]) < CACHE_TTL:
        return c["data"]
    try:
        r = SESSION.get(
            f"https://clob.polymarket.com/book?token_id={token_id}",
            timeout=8)
        r.raise_for_status()
        data = r.json()
        _ob_cache[token_id] = {"data": data, "ts": now}
        return data
    except:
        return None


def _wt_vol(levels: list, mid: float, decay: float = 0.72) -> float:
    """
    Proximity-weighted volume. Filters likely spoof orders.
    """
    if not levels:
        return 0.0
    sizes = sorted(float(l["size"]) for l in levels)
    med   = sizes[len(sizes) // 2] if sizes else 0
    spoof_thresh = max(med * 7, 200) if med else float("inf")
    total = 0.0
    for i, l in enumerate(levels[:15]):
        sz   = float(l["size"])
        dist = abs(float(l["price"]) - mid) + 1e-9
        if sz > spoof_thresh and dist > 0.04:
            continue   # likely spoof — exclude
        total += (decay ** i) * sz / dist
    return total


def analyze_book(token_id: str) -> dict:
    raw = _fetch_raw(token_id)
    if not raw:
        return _empty()

    bids = raw.get("bids", [])
    asks = raw.get("asks", [])
    if not bids or not asks:
        return _empty()

    bb  = float(bids[0]["price"])
    ba  = float(asks[0]["price"])
    mid = (bb + ba) / 2
    spr = ba - bb

    wt_bid = _wt_vol(bids, mid)
    wt_ask = _wt_vol(asks, mid)
    tot    = wt_bid + wt_ask
    imb    = (wt_bid - wt_ask) / tot if tot else 0.0

    raw_bv = sum(float(b["size"]) for b in bids[:5])
    raw_av = sum(float(a["size"]) for a in asks[:5])

    # ---- Absorption detection ----
    ts   = time.time()
    hist = _ob_hist[token_id]
    hist.append((ts, mid, raw_bv, raw_av))

    absorption = 0
    if len(hist) >= 5:
        old_mid = hist[-5][1]
        old_bv  = hist[-5][2]
        old_av  = hist[-5][3]
        dp      = mid - old_mid
        dbv     = raw_bv - old_bv
        dav     = raw_av - old_av
        # Bid volume surged but price didn't rise → bids absorbed (bearish)
        if dbv > 150 and dp < 0.003:
            absorption = -1
        # Ask volume surged but price didn't fall → asks absorbed (bullish)
        elif dav > 150 and dp > -0.003:
            absorption = 1

    # ---- Spoofing ----
    def _is_spoof(levels):
        if len(levels) < 3:
            return False
        top  = float(levels[0]["size"])
        rest = [float(l["size"]) for l in levels[1:4]]
        return rest and top > 6 * max(rest)

    # ---- Thin book ----
    thin = (raw_bv + raw_av) < 400

    # ---- Aggressive flow: are market orders hitting bids/asks? ----
    # Approximated by comparing this snapshot vs previous top-of-book size
    aggressive_buy = False
    if len(hist) >= 2:
        prev_av = hist[-2][3]
        if prev_av > 0 and raw_av < prev_av * 0.6:
            aggressive_buy = True   # asks got eaten → buyers hitting asks

    return {
        "mid":               mid,
        "spread":            spr,
        "best_bid":          bb,
        "best_ask":          ba,
        "imbalance":         round(imb, 4),
        "wt_bid_vol":        round(wt_bid, 1),
        "wt_ask_vol":        round(wt_ask, 1),
        "absorption_signal": absorption,
        "spoof_bid":         _is_spoof(bids),
        "spoof_ask":         _is_spoof(asks),
        "liquidity_thin":    thin,
        "aggressive_buy":    aggressive_buy,
        "valid":             True,
    }


def _empty() -> dict:
    return {
        "mid": None, "spread": 0.10, "best_bid": None, "best_ask": None,
        "imbalance": 0.0, "wt_bid_vol": 0, "wt_ask_vol": 0,
        "absorption_signal": 0, "spoof_bid": False, "spoof_ask": False,
        "liquidity_thin": True, "aggressive_buy": False, "valid": False,
    }


def get_imbalance(token_id: str) -> float:
    return analyze_book(token_id)["imbalance"]
