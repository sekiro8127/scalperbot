"""
PORTFOLIO RISK ENGINE
======================
Portfolio-level daily loss cap and drawdown guard.
Position sizing is fully delegated to capital.py.
"""
import time
from datetime import datetime, timezone

_daily_pnl          = 0.0
_day_start          = datetime.now(timezone.utc).date()
DAILY_LOSS_LIMIT_PCT = 0.08   # halt if daily loss > 8% of start balance
LIFETIME_DD_LIMIT_PCT = 0.25  # hard-kill if lifetime drawdown > 25%

_max_balance = 0.0


def _reset_day() -> None:
    global _daily_pnl, _day_start
    today = datetime.now(timezone.utc).date()
    if today != _day_start:
        _daily_pnl = 0.0
        _day_start = today


def record_pnl(pnl_value: float, current_balance: float) -> None:
    _reset_day()
    global _daily_pnl, _max_balance
    _daily_pnl += pnl_value
    _max_balance = max(_max_balance, current_balance)


def lifetime_drawdown_breached(current_balance: float) -> bool:
    global _max_balance
    if _max_balance <= 0:
        _max_balance = current_balance
        return False
    
    dd = (_max_balance - current_balance) / _max_balance
    if dd > LIFETIME_DD_LIMIT_PCT:
        print(f"🛑 FATAL: Lifetime drawdown limit reached ({dd:.1%}) — shutting down")
        return True
    return False


def daily_loss_breached(start_balance: float) -> bool:
    _reset_day()
    if _daily_pnl < -start_balance * DAILY_LOSS_LIMIT_PCT:
        print("🚨 Daily loss limit reached — trading paused until tomorrow")
        return True
    return False


def get_daily_pnl() -> float:
    _reset_day()
    return round(_daily_pnl, 2)
