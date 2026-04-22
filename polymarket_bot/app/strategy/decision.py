"""
TRADE DECISION ENGINE v2 — Confluence Required
================================================
Entry requires 3 of 5 independent signals to align.
This eliminates most false positives vs single-signal entry.

SIGNAL 1: Edge exists  (FV - price > min_edge)
SIGNAL 2: Orderbook confirms  (imbalance or absorption)
SIGNAL 3: Whale flow aligns  (large trades in same direction)
SIGNAL 4: Momentum not against us  (no active downtrend)
SIGNAL 5: Market quality passes  (spread, not dead zone, liquidity ok)

Exit logic is resolution-timing-aware: stops tighten as market approaches resolution.
"""
from __future__ import annotations

import time
from statistics import mean
from typing import Dict, List, Optional, Tuple

# Dead zone: price near 50/50 has no resolution signal
DEAD_ZONE_LOW  = 0.495
DEAD_ZONE_HIGH = 0.505


def _signal_edge(
    price: float,
    fv_result: dict,
    min_edge: float,
) -> Tuple[bool, str]:
    edge      = fv_result.get("fv", price) - price
    deviation = fv_result.get("deviation", 0.0)
    direction = fv_result.get("direction", 0)
    if direction > 0 and abs(edge) >= min_edge:
        return True, f"edge={edge:+.3f}"
    return False, f"edge={edge:+.3f}<{min_edge:.3f}"


def _signal_orderbook(ob: dict) -> Tuple[bool, str]:
    imb        = ob.get("imbalance", 0.0)
    absorption = ob.get("absorption_signal", 0)
    agg_buy    = ob.get("aggressive_buy", False)
    thin       = ob.get("liquidity_thin", True)
    ob_valid   = ob.get("valid", False)

    if not ob_valid or thin:
        return False, "OB_invalid_or_thin"
    if imb > 0.20:
        return True, f"OB_imb={imb:.2f}"
    if absorption == 1:
        return True, "OB_absorption_bull"
    if agg_buy:
        return True, "OB_aggressive_buy"
    return False, f"OB_imb={imb:.2f}<0.20"


def _signal_whale(flow: dict) -> Tuple[bool, str]:
    sig = flow.get("signal", 0)
    prs = flow.get("pressure", 0.0)
    if sig == 1 and prs >= 0.25:
        return True, f"whale_bull prs={prs:.2f}"
    if sig == 1:
        return True, "whale_bull_weak"
    return False, f"whale_sig={sig}"


def _signal_momentum(prices: List[float]) -> Tuple[bool, str]:
    """True if momentum is NOT actively against us (neutral or positive)."""
    if len(prices) < 6:
        return True, "no_history_neutral"
    window    = prices[-6:]
    delta_pct = (window[-1] - window[0]) / max(window[0], 1e-9)
    if delta_pct >= -0.005:   # not falling more than 0.5%
        return True, f"mom={delta_pct:+.3f}"
    return False, f"falling_knife mom={delta_pct:+.3f}"


def _signal_quality(
    price:  float,
    spread: float,
    depth:  float,
    min_depth: float = 300,
) -> Tuple[bool, str]:
    if DEAD_ZONE_LOW < price < DEAD_ZONE_HIGH:
        return False, f"dead_zone price={price:.3f}"
    if spread > 0.10:
        return False, f"spread_too_wide={spread:.3f}"
    if depth < min_depth:
        return False, f"thin_book depth={depth:.0f}"
    return True, f"quality_ok spread={spread:.3f}"


def decide_entry(
    price:      float,
    fv_result:  dict,
    ob:         dict,
    flow:       dict,
    prices:     List[float],
    min_score:  int   = 3,
    min_edge:   float = 0.03,
) -> Tuple[bool, str, int]:
    """
    Returns (should_open, reason, signals_met).
    Requires min_score out of 5 signals.
    """
    # Hard gate: dead zone
    if DEAD_ZONE_LOW < price < DEAD_ZONE_HIGH:
        return False, "dead_zone", 0

    spread = ob.get("spread", 0.10)
    depth  = ob.get("depth",  0.0) or (ob.get("wt_bid_vol", 0) + ob.get("wt_ask_vol", 0))

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
    return False, f"only_{signals_met}/5 need_{min_score} fails=[{','.join(fails)}]", signals_met


def decide_exit(
    market_id:  str,
    pos:        dict,
    price:      float,
    prices:     List[float],
    ob:         dict,
    fv_result:  dict,
    tier_params = None,   # TierParams from capital.py
) -> Tuple[str, str]:
    """
    Returns (signal, reason).
    Signals: SL | TP | TRAIL | PARTIAL | REVERT | MOMENTUM | TIMEOUT | HOLD

    Resolution-timing-aware: exits tighten as market approaches resolution.
    """
    entry      = float(pos.get("entry_price", pos.get("entry", price)))
    open_ts    = pos.get("open_ts", time.time())
    highest    = pos.get("highest_price", pos.get("max_price", price))
    partial_ok = pos.get("partial_closed", False) is False
    hold_hrs   = (time.time() - open_ts) / 3600
    gain_pct   = (price - entry) / max(entry, 1e-9)

    # Get tier-specific thresholds (fall back to sensible defaults)
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

    # Tighten exits as resolution approaches
    end_ts  = pos.get("end_ts") or fv_result.get("end_ts")
    hrs_left = None
    if end_ts:
        hrs_left = (end_ts - time.time()) / 3600
        if hrs_left < 2:
            # Near resolution: tighten everything
            sl_pct    = sl_pct * 0.6
            trail_pct = trail_pct * 0.6
            tp_pct    = min(tp_pct, 0.08)   # take profit faster
        elif hrs_left < 6:
            sl_pct    = sl_pct * 0.8
            trail_pct = trail_pct * 0.8

    # 1. Hard stop loss
    if gain_pct <= -sl_pct:
        return "SL", f"stop={gain_pct:.1%} limit=-{sl_pct:.1%}"

    # 2. Timeout (Scalping focus)
    if hold_hrs > max_hold:
        return "TIMEOUT", f"held {hold_hrs*60:.1f}m > {max_hold*60:.1f}m"

    # 3. Partial TP (1.5% target for scalping)
    if partial and partial_ok and gain_pct >= 0.015:
        return "PARTIAL", f"+{gain_pct:.1%} >= scalp_partial=1.5%"

    # 4. Full TP (2.5% target for scalping)
    if gain_pct >= 0.025:
        return "TP", f"+{gain_pct:.1%} >= scalp_tp=2.5%"

    # 5. Trailing stop (tight)
    gain_from_high = (highest - price) / highest if highest > 0 else 0.0
    if gain_from_high >= trail_pct:
        return "TRAIL", f"trail={trail_pct:.1%} Δhigh={gain_from_high:.1%}"

    # 6. Fair value reversion
    fv  = fv_result.get("fv")
    dev = fv_result.get("deviation", 0.0)
    if fv is not None and gain_pct > 0.005 and abs(dev) < 0.010:
        return "REVERT", f"FV_reached dev={dev:.3f} gain={gain_pct:.1%}"

    # 7. Adverse orderbook flip
    imb = ob.get("imbalance", 0.0)
    if gain_pct > 0.01 and imb < -0.25:
        return "MOMENTUM", f"OB_flipped imb={imb:.2f} gain={gain_pct:.1%}"

    return "HOLD", ""
