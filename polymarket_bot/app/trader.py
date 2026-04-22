"""
EXECUTION ENGINE v2 — Tier-aware, auto-scaling
================================================
- Trade size comes from capital.py TierParams (Kelly or % based on tier)
- TP/SL snapshotted at entry from current tier
- Partial closes properly tracked (no double-count)
- Streak outcomes reported to capital.py after every close
- Live balance synced from Polymarket on startup when LIVE_TRADING=true
"""
import os
import time
import logging
from typing import Dict, List, Optional, Tuple

from app.execution.trader import place_order, get_live_balance, cancel_all_orders
from app.strategy.capital  import (
    get_tier_params, record_outcome, is_in_cooldown,
    cooldown_remaining, get_streak_state,
)
from app.strategy.risk import record_pnl, lifetime_drawdown_breached, daily_loss_breached

logger = logging.getLogger(__name__)

LIVE_TRADING = os.getenv("LIVE_TRADING", "false").lower() == "true"
START_BALANCE = float(os.getenv("START_BALANCE", "10"))


def _init_balance() -> float:
    if LIVE_TRADING:
        live = get_live_balance()
        if live > 0:
            logger.info(f"Live USDC balance: ${live:.2f}")
            return live
        logger.warning("Could not fetch live balance — using START_BALANCE")
    return START_BALANCE


balance:          float           = _init_balance()
active_positions: Dict[str, dict] = {}
closed_trades:    List[dict]      = []

_perf = {
    "total_trades":       0,
    "wins":               0,
    "losses":             0,
    "total_pnl":          0.0,
    "max_drawdown":       0.0,
    "consecutive_losses": 0,
    "last_loss_time":     0.0,
    "peak_balance":       balance,
}


def _now() -> float:
    return time.time()


def get_performance_stats() -> dict:
    wr = _perf["wins"] / _perf["total_trades"] if _perf["total_trades"] else 0
    aw = (sum(t["pnl"] for t in closed_trades if t["pnl"] > 0) / _perf["wins"]
          if _perf["wins"] else 0)
    al = (sum(t["pnl"] for t in closed_trades if t["pnl"] < 0) / _perf["losses"]
          if _perf["losses"] else 0)
    return {
        **_perf,
        "win_rate":         round(wr, 4),
        "avg_win":          round(aw, 2),
        "avg_loss":         round(al, 2),
        "current_drawdown": round(
            (_perf["peak_balance"] - balance) / max(_perf["peak_balance"], 1), 4
        ),
    }


def can_open(market_id: str, alpha_score: int = 0, edge: float = 0.0,
             confidence: float = 0.5) -> Tuple[bool, str]:
    """Full gate: balance, cooldown, tier params, signal quality."""
    if market_id in active_positions:
        return False, "already_in_position"

    if balance < 10.0:
        return False, f"balance_${balance:.2f}_below_$10_minimum"

    if is_in_cooldown():
        rem = cooldown_remaining()
        return False, f"loss_cooldown_{rem:.0f}s_remaining"

    # Get tier params
    params = get_tier_params(balance, edge, confidence)

    if params.tier == "HALTED":
        return False, f"halted_balance_${balance:.2f}"

    if len(active_positions) >= params.max_positions:
        return False, f"max_positions_{params.max_positions}_reached"

    if alpha_score < params.min_score:
        return False, f"score_{alpha_score}<{params.min_score}_for_{params.tier}"

    if abs(edge) < params.min_edge:
        return False, f"edge_{edge:.3f}<{params.min_edge:.3f}_for_{params.tier}"

    if params.computed_size < 1.0:
        return False, f"computed_size_${params.computed_size:.2f}_too_small"

    # Drawdown circuit breaker
    dd = (_perf["peak_balance"] - balance) / max(_perf["peak_balance"], 1)
    if dd >= 0.20:
        return False, f"circuit_breaker_drawdown_{dd:.1%}"

    return True, "ok"


def open_position(
    market:     dict,
    price:      float,
    reason:     Optional[str] = None,
    alpha_score: int  = 0,
    edge:        float = 0.0,
    confidence:  float = 0.5,
    end_ts:      Optional[float] = None,
) -> Optional[dict]:
    global balance

    market_id = str(market["id"])
    token_id  = str(market["token_id"])

    ok, why = can_open(market_id, alpha_score, edge, confidence)
    if not ok:
        return None

    if daily_loss_breached(START_BALANCE):
        return None

    params = get_tier_params(balance, edge, confidence)
    size   = params.computed_size

    if balance < size:
        size = round(balance * 0.90, 2)

    if size < 1.0:
        return None

    if not place_order(token_id=token_id, price=price, size=size, side="BUY"):
        logger.error(f"Order failed | market={market_id}")
        return None

    position = {
        "market_id":      market_id,
        "token_id":       token_id,
        "question":       market.get("question", ""),
        "entry_price":    float(price),
        "entry":          float(price),
        "max_price":      float(price),
        "size":           size,
        "initial_size":   size,
        "partial_closed": False,
        "partial_pnl":    0.0,
        "timestamp":      _now(),
        "open_ts":        _now(),
        # Snapshot tier params at entry time
        "take_profit":    params.take_profit,
        "stop_loss":      params.stop_loss,
        "trail_pct":      params.trail_pct,
        "max_hold_hrs":   params.max_hold_hrs,
        "partial_exit":   params.partial_exit,
        "tier":           params.tier,
        "end_ts":         end_ts,
    }
    active_positions[market_id] = position
    balance -= size

    mode = "[LIVE]" if LIVE_TRADING else "[PAPER]"
    logger.info(
        f"[{mode}] OPEN | tier={params.tier} market={market_id[:16]} "
        f"entry={price:.4f} size=${size:.2f} score={alpha_score} "
        f"edge={edge:+.3f} TP={params.take_profit:.0%} SL={params.stop_loss:.0%} "
        f"balance_after=${balance:.2f} | {reason or ''}"
    )
    return position


def update_max_price(market_id: str, current_price: float) -> None:
    pos = active_positions.get(market_id)
    if pos and current_price > pos["max_price"]:
        pos["max_price"]    = current_price
        pos["highest_price"] = current_price


def close_partial_position(market_id: str, price: float, reason: str = "") -> bool:
    """
    Close 40% of position at partial TP.
    Proceeds go to balance immediately.
    Profit stored in partial_pnl for final reporting (NOT added again on full close).
    """
    global balance
    position = active_positions.get(market_id)
    if not position or position.get("partial_closed"):
        return False

    close_size = round(position["size"] * 0.50, 2)
    if close_size < 0.50:
        return False

    if not place_order(token_id=position["token_id"], price=price,
                       size=close_size, side="SELL"):
        return False

    entry   = float(position["entry_price"])
    pnl_pct = (price - entry) / max(entry, 1e-9)
    pnl     = pnl_pct * close_size

    balance += close_size + pnl
    position["size"]           -= close_size
    position["partial_closed"]  = True
    position["partial_pnl"]     = round(pnl, 4)

    mode = "[LIVE]" if LIVE_TRADING else "[PAPER]"
    hold_sec = time.time() - position["open_ts"]
    logger.info(
        f"[{mode}] PARTIAL (50%) | market={market_id[:16]} price={price:.4f} "
        f"pnl={pnl_pct:+.2%} dur={hold_sec:.1f}s | {reason}"
    )
    return True


def close_position(market: dict, price: float,
                   reason: Optional[str] = None) -> Optional[dict]:
    """
    Close remaining position.
    PnL calculated on REMAINING size only (partial already banked).
    Total = partial_pnl + remaining_pnl.
    """
    global balance
    market_id = str(market["id"])
    position  = active_positions.get(market_id)
    if not position:
        return None

    remaining = position["size"]

    if not place_order(token_id=position["token_id"], price=price,
                       size=remaining, side="SELL"):
        logger.error(f"Close order failed | market={market_id}")
        return None

    entry         = float(position["entry_price"])
    pnl_pct       = (price - entry) / max(entry, 1e-9)
    remaining_pnl = pnl_pct * remaining

    balance += remaining + remaining_pnl

    # Total PnL = partial already banked + this close
    total_pnl = position.get("partial_pnl", 0.0) + remaining_pnl

    # Update performance
    _perf["total_trades"] += 1
    if total_pnl > 0:
        _perf["wins"]               += 1
        _perf["consecutive_losses"]  = 0
    else:
        _perf["losses"]             += 1
        _perf["consecutive_losses"] += 1
        _perf["last_loss_time"]      = _now()

    _perf["total_pnl"] += total_pnl
    if balance > _perf["peak_balance"]:
        _perf["peak_balance"] = balance

    dd = (_perf["peak_balance"] - balance) / max(_perf["peak_balance"], 1)
    if dd > _perf["max_drawdown"]:
        _perf["max_drawdown"] = dd

    # Report to capital engine for streak tracking
    record_outcome(total_pnl)
    record_pnl(total_pnl, balance)

    if lifetime_drawdown_breached(balance):
        logger.critical("LIFETIME DRAWDOWN BREACHED. Emergency shutdown initiated.")
        os._exit(1)  # Force exit the entire process

    trade = {
        "market_id":   market_id,
        "token_id":    position["token_id"],
        "entry_price": entry,
        "exit_price":  float(price),
        "size":        position["initial_size"],
        "timestamp":   _now(),
        "pnl":         round(total_pnl,  4),
        "pnl_pct":     round(pnl_pct,    4),
        "reason":      reason or "",
        "tier":        position.get("tier", "?"),
    }
    closed_trades.append(trade)
    del active_positions[market_id]

    streak = get_streak_state()
    mode   = "[LIVE]" if LIVE_TRADING else "[PAPER]"
    hold_sec = time.time() - position["open_ts"]
    
    logger.info(
        f"[{mode}] CLOSE | market={market_id[:16]} "
        f"entry={entry:.4f} exit={price:.4f} "
        f"pnl={pnl_pct:+.2%} total_pnl=${total_pnl:+.2f} "
        f"dur={hold_sec:.1f}s bal=${balance:.2f} | {reason}"
    )
    return trade


def has_position(market_id: str) -> bool:
    return str(market_id) in active_positions


def get_position(market_id: str) -> Optional[dict]:
    return active_positions.get(str(market_id))


def get_state() -> dict:
    params = get_tier_params(balance)
    streak = get_streak_state()
    return {
        "balance":          round(balance, 2),
        "open_positions":   len(active_positions),
        "positions":        list(active_positions.values()),
        "tier":             params.tier,
        "next_trade_size":  params.computed_size,
        "take_profit":      params.take_profit,
        "stop_loss":        params.stop_loss,
        "min_score":        params.min_score,
        "max_positions":    params.max_positions,
        "streak":           streak,
        "stats":            get_performance_stats(),
        "live_mode":        LIVE_TRADING,
    }


def shutdown() -> None:
    if LIVE_TRADING and active_positions:
        logger.warning(f"Shutdown: {len(active_positions)} open — cancelling orders")
        cancel_all_orders()
