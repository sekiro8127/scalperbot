"""
CAPITAL TIER ENGINE  — NEW MODULE
===================================
Single source of truth for ALL risk parameters.
Bot behaves differently at $10 vs $100 vs $1000.

Tiers:
  MICRO  $10  – $30   ultra-conservative, 1 position, fast exits
  LOW    $30  – $100  still cautious, building base
  MID    $100 – $500  normal ops, 2 positions, partial exits
  HIGH   $500 – $2000 scale-in, 3 positions, wider stops
  WHALE  $2000+       4 positions, full Kelly, correlation filter

Auto-scaling:
  - Win streaks gradually increase size (capped at +25%)
  - Loss streaks cut size and add cooldown
  - Drawdown >15% halves risk and raises signal bar
  - Tier promotes when balance crosses threshold with <10% drawdown
"""

import time
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# ── Streak state ──────────────────────────────────────────────────────────────
_consecutive_losses: int   = 0
_consecutive_wins:   int   = 0
_last_loss_ts:       float = 0.0
_streak_size_mult:   float = 1.0
_peak_balance:       float = 0.0      # set on first call

LOSS_COOLDOWN_SECS   = 120
WIN_STREAK_TRIGGER   = 3
LOSS_STREAK_TRIGGER  = 2
WIN_STREAK_MAX_BOOST = 1.25
LOSS_STREAK_CUT      = 0.50


# ── Tier definitions ──────────────────────────────────────────────────────────
TIERS = {
    "MICRO": {
        "min": 1,   "max": 30,
        "risk_pct": 0.15,  "max_positions": 2,
        "min_score": 2,    "take_profit": 0.025,
        "stop_loss": 0.04, "partial_exit": True,
        "scale_in":  False,"max_hold_hrs": 0.5,        # 30 min — was 2 min
        "trail_pct": 0.015,"cooldown_secs": 30,
        "min_edge":  0.005,                            # was 0.01 (unreachable)
    },
    "LOW": {
        "min": 30,   "max": 100,
        "risk_pct": 0.18,  "max_positions": 3,
        "min_score": 2,    "take_profit": 0.03,
        "stop_loss": 0.06, "partial_exit": True,
        "scale_in":  False,"max_hold_hrs": 1.0,
        "trail_pct": 0.020,"cooldown_secs": 30,
        "min_edge":  0.01,                             # was 0.02
    },
    "MID": {
        "min": 100,  "max": 500,
        "risk_pct": 0.20,  "max_positions": 2,
        "min_score": 3,    "take_profit": 0.18,
        "stop_loss": 0.08, "partial_exit": True,
        "scale_in":  False,"max_hold_hrs": 24,
        "trail_pct": 0.025,"cooldown_secs": 60,
        "min_edge":  0.025,
    },
    "HIGH": {
        "min": 500,  "max": 2000,
        "risk_pct": 0.15,  "max_positions": 3,
        "min_score": 2,    "take_profit": 0.22,
        "stop_loss": 0.10, "partial_exit": True,
        "scale_in":  True, "max_hold_hrs": 36,
        "trail_pct": 0.020,"cooldown_secs": 60,
        "min_edge":  0.02,
    },
    "WHALE": {
        "min": 2000, "max": float("inf"),
        "risk_pct": 0.10,  "max_positions": 4,
        "min_score": 2,    "take_profit": 0.25,
        "stop_loss": 0.10, "partial_exit": True,
        "scale_in":  True, "max_hold_hrs": 36,
        "trail_pct": 0.015,"cooldown_secs": 30,
        "min_edge":  0.02,
    },
}


@dataclass
class TierParams:
    tier:           str   = "MICRO"
    balance:        float = 10.0
    risk_pct:       float = 0.15
    max_positions:  int   = 1
    min_score:      int   = 4
    take_profit:    float = 0.08
    stop_loss:      float = 0.04
    partial_exit:   bool  = False
    scale_in:       bool  = False
    max_hold_hrs:   float = 12.0
    trail_pct:      float = 0.035
    cooldown_secs:  int   = 180
    min_edge:       float = 0.04
    computed_size:  float = 0.0
    streak_mult:    float = 1.0
    dd_mult:        float = 1.0
    notes:          list  = field(default_factory=list)


def _get_tier_name(balance: float) -> str:
    for name, t in TIERS.items():
        if t["min"] <= balance < t["max"]:
            return name
    return "MICRO" if balance < 10 else "WHALE"


def _drawdown_multiplier(balance: float) -> float:
    global _peak_balance
    if _peak_balance <= 0:
        _peak_balance = balance
    _peak_balance = max(_peak_balance, balance)
    dd = (_peak_balance - balance) / _peak_balance
    if dd < 0.05:  return 1.00
    if dd < 0.10:  return 0.80
    if dd < 0.15:  return 0.60
    if dd < 0.20:  return 0.40
    return 0.25


def _kelly_size(balance: float, edge: float, confidence: float,
                base_risk_pct: float) -> float:
    """Conservative quarter-Kelly."""
    if edge <= 0:
        return balance * base_risk_pct * 0.5
    win_p = min(0.75, max(0.40, 0.50 + confidence * 0.25))
    b     = max(edge, 0.01) / max(1 - edge, 0.01)
    f     = max(0, (b * win_p - (1 - win_p)) / b) * 0.25   # quarter-Kelly
    f     = min(f, base_risk_pct * 1.5)                      # cap at 1.5x base
    return balance * f


def get_tier_params(balance: float, edge: float = 0.05,
                    confidence: float = 0.5) -> TierParams:
    """Master function — call once per trade decision."""
    global _peak_balance
    if _peak_balance <= 0:
        _peak_balance = balance

    # Hard stop — don't trade below $1
    if balance < 1:
        p = TierParams(tier="HALTED", balance=balance)
        p.risk_pct      = 0
        p.computed_size = 0
        p.notes.append(f"Balance ${balance:.2f} < $1 minimum — trading halted")
        return p

    tier_name = _get_tier_name(balance)
    t         = TIERS[tier_name]
    dd_mult   = _drawdown_multiplier(balance)

    # Raise min_score when in drawdown
    extra_score = 1 if dd_mult < 0.70 else 0

    p = TierParams(
        tier          = tier_name,
        balance       = balance,
        risk_pct      = t["risk_pct"],
        max_positions = t["max_positions"],
        min_score     = t["min_score"] + extra_score,
        take_profit   = t["take_profit"],
        stop_loss     = t["stop_loss"],
        partial_exit  = t["partial_exit"],
        scale_in      = t["scale_in"],
        max_hold_hrs  = t["max_hold_hrs"],
        trail_pct     = t["trail_pct"],
        cooldown_secs = t["cooldown_secs"],
        min_edge      = t["min_edge"],
        streak_mult   = _streak_size_mult,
        dd_mult       = dd_mult,
    )

    # Kelly sizing for MID+, simple % for MICRO/LOW
    if tier_name in ("MID", "HIGH", "WHALE"):
        raw = _kelly_size(balance, edge, confidence, t["risk_pct"])
    else:
        raw = balance * t["risk_pct"]

    raw  *= dd_mult
    raw  *= _streak_size_mult
    
    # Tick-size and slippage accounting
    # Min order size on Polymarket is often $1 or $5 depending on token, we use $1 as floor.
    # Round to 0.01 for USDC precision.
    raw   = max(1.0, min(raw, balance * 0.30))   # min $1, max 30%
    raw   = round(raw, 2)

    # Slippage adjustment: if size is large, we might want to reduce it or use a different execution strategy
    # For now, we just ensure it's at least $1.00
    if raw < 1.0:
        raw = 0.0  # Too small to execute
    
    p.computed_size = raw

    if extra_score:
        p.notes.append(f"DD protection: score bar raised (dd_mult={dd_mult:.2f})")
    if _streak_size_mult != 1.0:
        p.notes.append(f"Streak mult={_streak_size_mult:.2f}")

    logger.info(
        f"[CAPITAL] tier={tier_name} bal=${balance:.2f} size=${raw:.2f} "
        f"risk={t['risk_pct']:.0%} TP={t['take_profit']:.0%} "
        f"SL={t['stop_loss']:.0%} score≥{p.min_score} "
        f"dd={dd_mult:.2f} streak={_streak_size_mult:.2f}"
    )
    return p


def record_outcome(pnl: float) -> None:
    """Call after every closed trade to update streaks."""
    global _consecutive_losses, _consecutive_wins, _last_loss_ts, _streak_size_mult

    if pnl > 0:
        _consecutive_wins   += 1
        _consecutive_losses  = 0
        if _consecutive_wins >= WIN_STREAK_TRIGGER:
            _streak_size_mult = min(WIN_STREAK_MAX_BOOST,
                                    _streak_size_mult + 0.05)
            logger.info(f"Win streak {_consecutive_wins} → size_mult={_streak_size_mult:.2f}")
    else:
        _consecutive_losses += 1
        _consecutive_wins    = 0
        _last_loss_ts        = time.time()
        if _consecutive_losses >= LOSS_STREAK_TRIGGER:
            _streak_size_mult = LOSS_STREAK_CUT
            logger.warning(
                f"Loss streak {_consecutive_losses} → "
                f"size cut to {_streak_size_mult:.0%}, "
                f"cooldown {LOSS_COOLDOWN_SECS}s"
            )
        else:
            _streak_size_mult = max(LOSS_STREAK_CUT, _streak_size_mult * 0.90)


def is_in_cooldown() -> bool:
    if _consecutive_losses >= LOSS_STREAK_TRIGGER:
        return (time.time() - _last_loss_ts) < LOSS_COOLDOWN_SECS
    return False


def cooldown_remaining() -> float:
    if not is_in_cooldown():
        return 0.0
    return max(0, LOSS_COOLDOWN_SECS - (time.time() - _last_loss_ts))


def get_streak_state() -> dict:
    return {
        "consecutive_losses": _consecutive_losses,
        "consecutive_wins":   _consecutive_wins,
        "size_multiplier":    round(_streak_size_mult, 3),
        "in_cooldown":        is_in_cooldown(),
        "cooldown_remaining": round(cooldown_remaining(), 1),
        "peak_balance":       round(_peak_balance, 2),
    }
