"""
FLOW DETECTION — dynamic threshold, directional pressure.
Tries CLOB first, falls back to Gamma. 25s cache.
"""
import time, requests

SESSION   = requests.Session()
_cache    = {}
CACHE_TTL = 25


def get_flow(token_id: str, limit: int = 50) -> dict:
    now = time.time()
    if token_id in _cache and (now - _cache[token_id]["ts"]) < CACHE_TTL:
        return _cache[token_id]

    blank = {"signal": 0, "pressure": 0.0, "buy_vol": 0,
             "sell_vol": 0, "large_buys": 0, "large_sells": 0, "ts": now}

    trades = []
    for url in [
        f"https://clob.polymarket.com/trades?token_id={token_id}&limit={limit}",
        f"https://gamma-api.polymarket.com/trades?market={token_id}&limit={limit}",
    ]:
        try:
            r = SESSION.get(url, timeout=7); r.raise_for_status()
            raw = r.json()
            trades = raw if isinstance(raw, list) else raw.get("data", [])
            if isinstance(trades, list) and trades: break
        except Exception:
            pass

    if not trades:
        _cache[token_id] = blank; return blank

    sizes = []
    for t in trades:
        v = t.get("usdcSize") or t.get("size") or t.get("amount") or 0
        try: sizes.append(float(v))
        except Exception: pass

    if not sizes:
        _cache[token_id] = blank; return blank

    sizes_s   = sorted(sizes)
    p75       = sizes_s[max(0, int(len(sizes_s) * 0.75) - 1)]
    threshold = max(150, p75 * 1.5)
    bv = sv = 0.0; lb = ls = 0

    for t in trades:
        v    = t.get("usdcSize") or t.get("size") or t.get("amount") or 0
        side = (t.get("side") or t.get("maker_side") or "").upper()
        try: sz = float(v)
        except Exception: continue
        is_buy = "BUY" in side or "YES" in side
        if is_buy:
            bv += sz
            if sz >= threshold: lb += 1
        else:
            sv += sz
            if sz >= threshold: ls += 1

    tot    = bv + sv
    prs    = abs(bv - sv) / tot if tot else 0.0
    sig    = 1 if lb > ls else (-1 if ls > lb else 0)
    result = {"signal": sig, "pressure": round(prs, 3),
              "buy_vol": bv, "sell_vol": sv,
              "large_buys": lb, "large_sells": ls, "ts": now}
    _cache[token_id] = result
    return result


def whale_signal(token_id: str) -> int:
    return get_flow(token_id)["signal"]
