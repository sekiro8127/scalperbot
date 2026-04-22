"""
DASHBOARD API
==============
Reads _message_count via module reference (not stale import snapshot).
Exposes tier/streak/capital state for the new dashboard.
"""
from fastapi import APIRouter
import app.core.websocket as _ws

from app.orchestrator        import signals
from app.trader              import active_positions, get_state
from app.storage.performance import get_trades, stats, auto_thresholds
from app.core.websocket      import price_stream, is_live
from app.strategy.capital    import get_streak_state

router = APIRouter()


@router.get("/signals")
def get_signals():
    return signals[-50:]


@router.get("/positions")
def get_positions():
    return list(active_positions.values())


@router.get("/stats")
def get_stats():
    rs     = get_state()
    streak = get_streak_state()
    return {
        **rs,
        "ws_messages": _ws._message_count,
        "ws_tokens":   len(price_stream),
        "ws_live":     is_live(30),
        "streak":      streak,
        **stats(),
    }


@router.get("/trades")
def get_trades_api():
    return get_trades()


@router.get("/autotune")
def get_autotune():
    return auto_thresholds()


@router.get("/capital")
def get_capital():
    """Current tier params for the dashboard capital panel."""
    from app.trader import balance
    from app.strategy.capital import get_tier_params
    p = get_tier_params(balance)
    return {
        "tier":           p.tier,
        "balance":        round(balance, 2),
        "next_size":      p.computed_size,
        "risk_pct":       round(p.risk_pct, 3),
        "take_profit":    p.take_profit,
        "stop_loss":      p.stop_loss,
        "max_positions":  p.max_positions,
        "min_score":      p.min_score,
        "partial_exit":   p.partial_exit,
        "scale_in":       p.scale_in,
        "streak":         get_streak_state(),
        "notes":          p.notes,
    }
