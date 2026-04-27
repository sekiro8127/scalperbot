"""
TRADE DECISION ENGINE v2.1 — Confluence (loosened for Polymarket microstructure)
==================================================================================
Polymarket books are sparse. v2's strict imbalance/depth gates blocked virtually
every entry. This version:

  * Uses tighter, realistic depth/imbalance thresholds.
  * Treats a thin/invalid book as a NEUTRAL signal, not an automatic fail.
  * Lowers the deviation needed for the edge signal so FV is actionable.
  * Returns rich diagnostics so the orchestrator can log skip reasons.

SIGNAL 1: Edge exists  (FV - price > min_edge, direction agrees)
SIGNAL 2: Orderbook supports direction (imbalance / absorption / aggressive)
SIGNAL 3: Whale flow aligns
SIGNAL 4: Momentum not against us
SIGNAL 5: Market quality passes (spread, not dead-zone)
"""
from __future__ import annotations

import time
from typing import Dict, List, Tuple

# Dead zone: price near 50/50 has no resolution signal
DEAD_ZONE_LOW  = 0.495
DEAD_ZONE_HIGH = 0.505


def _signal_edge(price: float, fv_result: dict, min_edge: float) -> Tuple[bool, str]:
    edge      = fv_result.get("fv", price) - price
    direction = fv_result.get("direction", 0)
    # Buy signal if FV is above price by at least min_edge
    if direction > 0 and edge >= min_edge:
        return True, f"edge=+{edge:.3f}>={min_edge:.3f}"
    return False, f"edge={edge:+.3f} dir={direction}"


def _signal_orderbook(ob: dict) -> Tuple[bool, str]:
    """Loosened: thin/invalid book is NEUTRAL (False but not a hard fail)."""
    imb        = ob.get("imbalance", 0.0)
    absorption = ob.get("absorption_signal", 0)
    agg_buy    = ob.get("aggressive_buy", False)
    thin       = ob.get("liquidity_thin", True)
    ob_valid   = ob.get("valid", False)

    if not ob_valid:
        return False, "OB_invalid"
    # In thin books we still allow a strong imbalance to count as a signal.
    if imb > 0.10:
        return True, f"OB_imb=+{imb:.2f}"
    if absorption == 1:
        return True, "OB_absorption_bull"
    if agg_buy:
        return True, "OB_aggressive_buy"
    if thin:
        return False, f"OB_thin imb={imb:+.2f}"
    return False, f"OB_imb={imb:+.2f}<0.10"


def _signal_whale(flow: dict) -> Tuple[bool, str]:
    sig = flow.get("signal", 0)
    prs = flow.get("pressure", 0.0)
    if sig == 1:
        return True, f"whale_bull prs={prs:.2f}"
    return False, f"whale_sig={sig}"


def _signal_momentum(prices: List[float]) -> Tuple[bool, str]:
    """True if momentum is NOT actively against us (neutral or positive)."""
    if len(prices) < 4:
        return True, "no_history_neutral"
    window    = prices[-6:] if len(prices) >= 6 else prices
    delta_pct = (window[-1] - window[0]) / max(window[0], 1e-9)
    if delta_pct >= -0.005:
        return True, f"mom={delta_pct:+.3f}"
    return False, f"falling_knife mom={delta_pct:+.3f}"


def _signal_quality(price: float, spread: float, depth: float,
                    min_depth: float = 100) -> Tuple[bool, str]:
    if DEAD_ZONE_LOW < price < DEAD_ZONE_HIGH:
        return False, f"dead_zone price={price:.3f}"
    if spread > 0.10:
        return False, f"spread_too_wide={spread:.3f}"
    if depth < min_depth:
        return False, f"thin_book depth={depth:.0f}<{min_depth:.0f}"
    return True, f"quality_ok spread={spread:.3f} depth={depth:.0f}"


def decide_entry(
    price:      float,
    fv_result:  dict,
    ob:         dict,
    flow:       dict,
    prices:     List[float],
    min_score:  int   = 2,
    min_edge:   float = 0.01,
) -> Tuple[bool, str, int]:
    """
    Returns (should_open, reason, signals_met).
    Requires min_score out of 5 signals.
    """
    if DEAD_ZONE_LOW < price < DEAD_ZONE_HIGH:
        return False, "dead_zone", 0

    spread = ob.get("spread", 0.10)
    depth  = ob.get("depth",  0.0) or (
        ob.get("wt_bid_vol", 0) + ob.get("wt_ask_vol", 0)
    )

    s1, r1 = _signal_edge(price, fv_result, min_edge)
    s2, r2 = _signal_orderbook(ob)
    s3, r3 = _signal_whale(flow)
    s4, r4 = _signal_momentum(prices)
    s5, r5 = _signal_quality(price, spread, depth)

    signals_met = sum([s1, s2, s3, s4, s5])
    reasons     = [r for s, r in [(s1,r1),(s2,r2),(s3,r3),(s4,r4),(s5,r5)] if s]
    fails       = [r for s, r in [(s1,r1),(s2,r2),(s3,r3),(s4,r4),(s5,r5)] if not s]

    if signals_met >= min_score:
        return True, f"confluence_{signals_met}/5 [{','.join(reasons)}]", signals_met
    return (
        False,
        f"only_{signals_met}/5 need_{min_score} pass=[{','.join(reasons)}] fail=[{','.join(fails)}]",
        signals_met,
    )


def decide_exit(
    market_id:  str,
    pos:        dict,
    price:      float,
    prices:     List[float],
    ob:         dict,
    fv_result:  dict,
    tier_params = None,
) -> Tuple[str, str]:
    """Exit logic — unchanged behaviour, slightly more readable."""
    entry      = float(pos.get("entry_price", pos.get("entry", price)))
    open_ts    = pos.get("open_ts", time.time())
    highest    = pos.get("highest_price", pos.get("max_price", price))
    partial_ok = pos.get("partial_closed", False) is False
    hold_hrs   = (time.time() - open_ts) / 3600
    gain_pct   = (price - entry) / max(entry, 1e-9)

    if tier_params:
        sl_pct    = tier_params.stop_loss
        tp_pct    = tier_params.take_profit
        trail_pct = tier_params.trail_pct
        max_hold  = tier_params.max_hold_hrs
        partial   = tier_params.partial_exit
    else:
        sl_pct    = 0.06
        tp_pct    = 0.15
        trail_pct = 0.030
        max_hold  = 24.0
        partial   = True

    end_ts  = pos.get("end_ts") or fv_result.get("end_ts")
    hrs_left = None
    if end_ts:
        hrs_left = (end_ts - time.time()) / 3600
        if hrs_left < 2:
            sl_pct    = sl_pct * 0.6
            trail_pct = trail_pct * 0.6
            tp_pct    = min(tp_pct, 0.08)
        elif hrs_left < 6:
            sl_pct    = sl_pct * 0.8
            trail_pct = trail_pct * 0.8

    if gain_pct <= -sl_pct:
        return "SL", f"stop={gain_pct:.1%} limit=-{sl_pct:.1%}"

    if hold_hrs > max_hold:
        return "TIMEOUT", f"held {hold_hrs*60:.1f}m > {max_hold*60:.1f}m"

    if partial and partial_ok and gain_pct >= 0.015:
        return "PARTIAL", f"+{gain_pct:.1%} >= scalp_partial=1.5%"

    if gain_pct >= max(0.025, tp_pct * 0.5):
        return "TP", f"+{gain_pct:.1%} >= tp={tp_pct:.1%}"

    gain_from_high = (highest - price) / highest if highest > 0 else 0.0
    if gain_from_high >= trail_pct:
        return "TRAIL", f"trail={trail_pct:.1%} Δhigh={gain_from_high:.1%}"

    fv  = fv_result.get("fv")
    dev = fv_result.get("deviation", 0.0)
    if fv is not None and gain_pct > 0.005 and abs(dev) < 0.010:
        return "REVERT", f"FV_reached dev={dev:.3f} gain={gain_pct:.1%}"

    imb = ob.get("imbalance", 0.0)
    if gain_pct > 0.01 and imb < -0.25:
        return "MOMENTUM", f"OB_flipped imb={imb:.2f} gain={gain_pct:.1%}"

    return "HOLD", ""
