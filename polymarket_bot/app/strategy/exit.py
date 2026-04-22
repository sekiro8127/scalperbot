"""
EXIT STRATEGY v2 — Resolution-timing-aware
===========================================
Thresholds tighten automatically as market approaches resolution.
Partial exits enabled for MID/HIGH/WHALE tiers (snapshotted at entry).
"""
import time


def exit_rule(
    market_id:   str,
    pos:         dict,
    price:       float,
    prices:      list,
    ob:          dict,
    fv_result:   dict,
) -> tuple:
    """
    Returns (signal, reason).
    Signals: SL | TP | TRAIL | PARTIAL | REVERT | MOMENTUM | TIMEOUT | HOLD

    Uses TP/SL/trail snapshotted at open time (tier-appropriate).
    Tightens near resolution if end_ts is available.
    """
    entry      = float(pos.get("entry_price", pos.get("entry", price)))
    open_ts    = pos.get("open_ts", time.time())
    highest    = pos.get("highest_price", pos.get("max_price", price))
    hold_hrs   = (time.time() - open_ts) / 3600
    gain_pct   = (price - entry) / max(entry, 1e-9)

    # Pull tier-snapshotted thresholds (set at open time)
    sl_pct         = pos.get("stop_loss",    0.06)
    tp_pct         = pos.get("take_profit",  0.15)
    trail_pct      = pos.get("trail_pct",    0.030)
    max_hold       = pos.get("max_hold_hrs", 24.0)
    partial_ok     = pos.get("partial_exit", True) and not pos.get("partial_closed", False)
    partial_trigger = tp_pct * 0.50   # partial at 50% of full TP target

    # Resolution-timing: tighten as expiry approaches
    end_ts   = pos.get("end_ts")
    hrs_left = None
    if end_ts:
        hrs_left = (end_ts - time.time()) / 3600
        if hrs_left < 1:
            sl_pct    = sl_pct    * 0.50
            trail_pct = trail_pct * 0.50
            tp_pct    = min(tp_pct, 0.06)
        elif hrs_left < 3:
            sl_pct    = sl_pct    * 0.70
            trail_pct = trail_pct * 0.70
            tp_pct    = min(tp_pct, 0.10)
        elif hrs_left < 6:
            sl_pct    = sl_pct    * 0.85
            trail_pct = trail_pct * 0.85

    # 1. Hard stop loss
    if gain_pct <= -sl_pct:
        return "SL", f"stop={gain_pct:.1%} limit=-{sl_pct:.1%}"

    # 2. Timeout
    if hold_hrs > max_hold:
        return "TIMEOUT", f"held {hold_hrs:.1f}h > {max_hold:.0f}h"

    # 3. Partial TP (MID/HIGH/WHALE)
    if partial_ok and gain_pct >= partial_trigger:
        return "PARTIAL", f"+{gain_pct:.1%} >= partial={partial_trigger:.1%}"

    # 4. Fair value reversion — trade worked, exit cleanly
    fv  = fv_result.get("fv")
    dev = fv_result.get("deviation", 0.0)
    if fv is not None and gain_pct > 0.01 and abs(dev) < 0.012:
        return "REVERT", f"FV_reached dev={dev:.3f} gain={gain_pct:.1%}"

    # 5. Orderbook flipped against us while in profit
    imb = ob.get("imbalance", 0.0)
    if gain_pct > 0.015 and imb < -0.30:
        return "MOMENTUM", f"OB_flipped imb={imb:.2f} gain={gain_pct:.1%}"

    # 6. Trailing stop from high
    gain_from_high = (highest - price) / highest if highest > 0 else 0.0
    if gain_from_high >= trail_pct:
        return "TRAIL", f"trail={trail_pct:.1%} Δhigh={gain_from_high:.1%}"

    # 7. Full TP
    if gain_pct >= tp_pct:
        return "TP", f"+{gain_pct:.1%} >= tp={tp_pct:.1%}"

    return "HOLD", ""
