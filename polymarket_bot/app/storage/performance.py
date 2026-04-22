"""
PERFORMANCE ENGINE v2
======================
SQLite-backed — trade history survives restarts.
Loads existing trades on first call.
Auto-tune recommendations after 10+ trades.
"""
import os
import sqlite3
import time
from collections import defaultdict

DB_PATH   = os.getenv("PERFORMANCE_DB", "performance.db")
START_BAL = float(os.getenv("START_BALANCE", "10"))

trades       = []
_equity      = [START_BAL]
_exit_counts = defaultdict(int)
_ready       = False


def _init() -> None:
    global _ready
    if _ready:
        return
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          REAL,
            market      TEXT,
            entry       REAL,
            exit_price  REAL,
            size        REAL,
            pnl_pct     REAL,
            pnl_value   REAL,
            exit_reason TEXT,
            tier        TEXT
        )
    """)
    conn.commit()
    rows = conn.execute(
        "SELECT market,entry,exit_price,size,pnl_pct,pnl_value,exit_reason,ts,tier "
        "FROM trades ORDER BY id"
    ).fetchall()
    conn.close()
    for r in rows:
        t = {
            "market": r[0], "entry": r[1], "exit": r[2],
            "size": r[3], "pnl_pct": r[4], "pnl_value": r[5],
            "exit_reason": r[6], "ts": r[7], "tier": r[8] or "?",
        }
        trades.append(t)
        _equity.append(_equity[-1] + t["pnl_value"])
        _exit_counts[t["exit_reason"]] += 1
    _ready = True
    print(f"Performance DB | {len(trades)} historical trades loaded")


def log_trade(market: str, entry: float, exit_price: float,
              size: float, exit_reason: str = "", tier: str = "?") -> dict:
    _init()
    pnl_pct = (exit_price - entry) / max(entry, 1e-9)
    pnl_val = pnl_pct * size
    ts      = time.time()
    t = {
        "market":      market[:60],
        "entry":       entry,
        "exit":        exit_price,
        "size":        size,
        "pnl_pct":     round(pnl_pct, 4),
        "pnl_value":   round(pnl_val, 2),
        "exit_reason": exit_reason,
        "ts":          ts,
        "tier":        tier,
    }
    trades.append(t)
    _equity.append(_equity[-1] + pnl_val)
    _exit_counts[exit_reason] += 1
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT INTO trades "
            "(ts,market,entry,exit_price,size,pnl_pct,pnl_value,exit_reason,tier) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (ts, t["market"], entry, exit_price, size,
             t["pnl_pct"], t["pnl_value"], exit_reason, tier),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"DB write error: {e}")
    print(f"TRADE | pnl=${pnl_val:+.2f} ({pnl_pct:+.1%}) tier={tier} reason={exit_reason}")
    return t


def get_trades() -> list:
    _init()
    return trades


def _max_dd() -> float:
    if len(_equity) < 2:
        return 0.0
    peak = _equity[0]; mdd = 0.0
    for e in _equity:
        peak = max(peak, e)
        mdd  = max(mdd, (peak - e) / max(peak, 1e-9))
    return round(mdd, 4)


def _expectancy(sample: list) -> float:
    if not sample: return 0.0
    ws = [t["pnl_pct"] for t in sample if t["pnl_pct"] > 0]
    ls = [t["pnl_pct"] for t in sample if t["pnl_pct"] <= 0]
    wr = len(ws) / len(sample)
    aw = sum(ws) / len(ws) if ws else 0
    al = sum(ls) / len(ls) if ls else 0
    return round(wr * aw + (1 - wr) * al, 4)


def stats() -> dict:
    _init()
    if not trades:
        return {
            "total_trades": 0, "wins": 0, "losses": 0,
            "winrate": 0, "avg_return": 0, "total_pnl": 0,
            "max_drawdown": 0, "expectancy": 0,
            "recent_winrate": 0, "recent_expectancy": 0,
            "exit_breakdown": {}, "tier_breakdown": {},
        }
    wins   = [t for t in trades if t["pnl_pct"] > 0]
    recent = trades[-20:]
    rw     = [t for t in recent if t["pnl_pct"] > 0]

    # Tier breakdown
    tier_pnl = defaultdict(float)
    for t in trades:
        tier_pnl[t.get("tier", "?")] += t["pnl_value"]

    return {
        "total_trades":      len(trades),
        "wins":              len(wins),
        "losses":            len(trades) - len(wins),
        "winrate":           round(len(wins) / len(trades), 3),
        "avg_return":        round(sum(t["pnl_pct"] for t in trades) / len(trades), 4),
        "total_pnl":         round(sum(t["pnl_value"] for t in trades), 2),
        "max_drawdown":      _max_dd(),
        "expectancy":        _expectancy(trades),
        "recent_winrate":    round(len(rw) / len(recent), 3) if recent else 0,
        "recent_expectancy": _expectancy(recent),
        "exit_breakdown":    dict(_exit_counts),
        "tier_breakdown":    {k: round(v, 2) for k, v in tier_pnl.items()},
    }


def auto_thresholds() -> dict:
    s = stats()
    if s["total_trades"] < 10:
        return {"note": "Need 10+ trades for auto-tuning", **s}
    recs = []
    if s["recent_winrate"] < 0.45:
        recs.append("⚠️  Win rate low — raise confluence gate to 4/5 signals")
    if s["recent_winrate"] > 0.72:
        recs.append("✅  Win rate high — can lower gate to 2/5, trade more often")
    if s["max_drawdown"] > 0.15:
        recs.append("⚠️  Drawdown >15% — tighten trailing stop or reduce Kelly fraction")
    if s["expectancy"] < 0:
        recs.append("🚨  Negative expectancy — external FV data may be noisy, check NEWSAPI_KEY")
    sl = _exit_counts.get("SL", 0)
    tp = _exit_counts.get("TP", 0) + _exit_counts.get("PARTIAL", 0) + _exit_counts.get("REVERT", 0)
    if sl > tp * 2:
        recs.append("⚠️  Too many SL exits — entries may be too early; wait for more confluence")
    if _exit_counts.get("TRAIL", 0) > len(trades) * 0.4:
        recs.append("ℹ️  Many TRAIL exits — trailing stop may be too tight for this market")
    if _exit_counts.get("TIMEOUT", 0) > len(trades) * 0.3:
        recs.append("ℹ️  Many TIMEOUT exits — consider shorter max_hold_hrs")
    return {"recommendations": recs, **s}
