"""
SNIPER ENTRY v3 — Prediction Market Edition
=============================================
Two setups:
  1. Breakout → Pullback → Confirmation (momentum continuation)
  2. Overextension → Reversal setup (fade / mean-reversion entry)
Both require multi-tick confirmation + per-market cooldown.
"""
import time
from collections import defaultdict, deque

_cooldowns   = defaultdict(float)
_sig_hist    = defaultdict(lambda: deque(maxlen=8))
COOLDOWN_SEC = 90
CONFIRM_N    = 2


def _register(mid, val):
    _sig_hist[mid].append((time.time(), val))


def _is_confirmed(mid):
    h = list(_sig_hist[mid])
    return len(h) >= CONFIRM_N and all(v for _, v in h[-CONFIRM_N:])


def sniper_entry(prices: list, market_id: str = "default") -> bool:
    if len(prices) < 10:
        _register(market_id, False); return False
    if time.time() - _cooldowns[market_id] < COOLDOWN_SEC:
        return False

    r    = prices[-10:]
    hi5  = max(r[:5]);  lo5 = min(r[:5])
    rng  = hi5 - lo5
    cur  = prices[-1];  p1 = prices[-2]; p2 = prices[-3]

    ok = False

    if rng > 0.006:
        # ---- Setup 1: breakout → pullback → bounce ----
        hi8  = max(r[5:8])
        broke = hi8 > hi5 * 1.005
        pb_d  = (hi8 - p2) / (hi8 - lo5 + 1e-9)
        bounce = p1 > p2 and cur > p1
        mom_ok = (cur - min(r[-5:])) / (max(r[-5:]) - min(r[-5:]) + 1e-9) > 0.45
        ok = broke and 0.15 <= pb_d <= 0.75 and bounce and mom_ok

        # ---- Setup 2: overextension fade (reversal entry) ----
        if not ok:
            mu    = sum(r) / len(r)
            sd    = (sum((x - mu)**2 for x in r) / len(r)) ** 0.5
            z     = (cur - mu) / sd if sd > 0.001 else 0
            # Price overextended down → reversal entry
            ok = z < -1.8 and cur > p1   # bouncing from oversold

    _register(market_id, ok)
    if _is_confirmed(market_id):
        _cooldowns[market_id] = time.time()
        return True
    return False


def reset_cooldown(mid):
    _cooldowns[mid] = 0
