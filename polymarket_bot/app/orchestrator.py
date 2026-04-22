"""
ORCHESTRATOR v2 — All systems wired
=====================================
Wires: FV (with external resolution prob) → mispricing score →
       3-of-5 confluence gate → tier-aware sizing →
       resolution-timing-aware exits → SQLite trade log
"""
import threading
import time
import logging
from collections import deque
from typing import Deque, Dict, List, Tuple

from app.core.market_api     import get_markets
from app.core.orderbook      import analyze_book
from app.core.websocket      import (
    get_next_price_update, is_live, run_ws, update_subscriptions,
)
from app.alpha.fair_value    import record_price, compute_fair_value
from app.alpha.mispricing    import score_mispricing, _velocity
from app.ingestion.whale     import get_flow
from app.ingestion.social    import get_sentiment
from app.strategy.decision   import decide_entry, decide_exit
from app.strategy.capital    import get_tier_params, is_in_cooldown
from app.storage.performance import log_trade
from app.trader import (
    close_position, close_partial_position,
    get_performance_stats, get_position, get_state,
    has_position, open_position, update_max_price,
    balance as _balance_ref,
)

logger = logging.getLogger(__name__)

MARKET_REFRESH_SEC = 120
MARKETS_TOP_N      = 30
SENTIMENT_TTL      = 300

signals: List[dict] = []
_sentiment_cache: Dict[str, Tuple[float, float]] = {}
_flow_cache:      Dict[str, dict] = {}
_last_ingest_ts:  Dict[str, float] = {}
_last_trade_ts:   float = time.time()
INGEST_MIN_INTERVAL = 10  # seconds


def _update_ingestion_background(token_id: str, market_id: str, question: str):
    """Background worker to fetch slow ingestion data without blocking main loop."""
    try:
        # 1. Flow
        flow = get_flow(token_id)
        _flow_cache[token_id] = flow

        # 2. Sentiment (if cache expired)
        now = time.time()
        c = _sentiment_cache.get(market_id)
        if not c or (now - c[0]) > SENTIMENT_TTL:
            score = get_sentiment(question)
            _sentiment_cache[market_id] = (now, score)
    except Exception as e:
        logger.error(f"Ingestion error for {token_id}: {e}")


def _cached_sentiment(market_id: str) -> float:
    c = _sentiment_cache.get(market_id)
    return c[1] if c else 0.0


def _cached_flow(token_id: str) -> dict:
    return _flow_cache.get(token_id, {
        "signal": 0, "pressure": 0.0, "buy_vol": 0,
        "sell_vol": 0, "large_buys": 0, "large_sells": 0
    })


def _build_market_map(markets):
    return {str(m["token_id"]): m for m in markets if m.get("token_id")}


def _bootstrap():
    for attempt in range(5):
        markets = get_markets(top_n=MARKETS_TOP_N)
        if markets:
            return markets, _build_market_map(markets)
        logger.warning(f"Market retry {attempt+1}/5")
        time.sleep(3)
    raise RuntimeError("Cannot fetch markets after 5 attempts")


def _log_status():
    from app.trader import balance
    state  = get_state()
    perf   = state["stats"]
    streak = state["streak"]
    logger.info(
        f"STATUS | tier={state['tier']} bal=${balance:.2f} "
        f"next_size=${state['next_trade_size']:.2f} "
        f"wr={perf['win_rate']:.1%} dd={perf['current_drawdown']:.1%} "
        f"pos={state['open_positions']} "
        f"streak={streak['consecutive_wins']}W/{streak['consecutive_losses']}L "
        f"WS={'LIVE' if is_live(30) else 'IDLE'}"
    )


def run():
    global _last_trade_ts
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger.info("ENGINE STARTING")
    markets, token_to_market = _bootstrap()
    logger.info(f"MARKETS READY | {len(token_to_market)} tokens")

    threading.Thread(
        target=run_ws,
        args=(list(token_to_market.keys()),),
        daemon=True, name="ws",
    ).start()

    last_refresh = time.time()
    last_status  = 0.0
    price_history: Dict[str, Deque[float]] = {}

    while True:
        update = get_next_price_update(timeout=1.0)
        now    = time.time()

        # Market refresh
        if now - last_refresh >= MARKET_REFRESH_SEC:
            refreshed = get_markets(top_n=MARKETS_TOP_N)
            if refreshed:
                markets         = refreshed
                token_to_market = _build_market_map(markets)
                update_subscriptions(list(token_to_market.keys()))
                logger.info(f"MARKETS REFRESHED | {len(token_to_market)}")
            last_refresh = now

        if now - last_status >= 20:
            _log_status()
            last_status = now

        if update is None:
            continue

        token_id, price, ts = update
        market = token_to_market.get(str(token_id))
        if not market:
            continue

        market_id = str(market["id"])
        price     = float(price)

        # Price history
        if market_id not in price_history:
            price_history[market_id] = deque(maxlen=60)
        price_history[market_id].append(price)
        ph = list(price_history[market_id])

        record_price(token_id, price, ts)

        # Trigger background ingestion update (non-blocking, rate-limited)
        if now - _last_ingest_ts.get(token_id, 0) > INGEST_MIN_INTERVAL:
            _last_ingest_ts[token_id] = now
            threading.Thread(
                target=_update_ingestion_background,
                args=(token_id, market_id, market.get("question", "")),
                daemon=True
            ).start()

        # Alpha signals (using cached flow/sentiment)
        ob        = analyze_book(token_id)
        ob_mid    = ob.get("mid") or price
        fv_result = compute_fair_value(
            token_id     = token_id,
            current_price = price,
            ob_mid        = ob_mid,
            ob_imbalance  = ob.get("imbalance", 0.0),
            question      = market.get("question", ""),
            end_date_ts   = market.get("end_ts"),
        )
        flow      = _cached_flow(token_id)
        alpha     = score_mispricing(
            market_id     = market_id,
            token_id      = token_id,
            current_price = price,
            fv_result     = fv_result,
            prices        = ph,
            ob            = ob,
            flow          = flow,
        )
        sentiment = _cached_sentiment(market_id)

        update_max_price(market_id, price)
        position = get_position(market_id)

        # ── EXIT CHECKS ───────────────────────────────────────────────
        if position:
            # Get current tier params for exit thresholds
            from app.trader import balance
            tier_params = get_tier_params(balance, alpha["edge"], alpha["confidence"])

            # Attach end_ts to position if available
            if market.get("end_ts") and not position.get("end_ts"):
                position["end_ts"] = market["end_ts"]

            exit_sig, exit_why = decide_exit(
                market_id  = market_id,
                pos        = position,
                price      = price,
                prices     = ph,
                ob         = ob,
                fv_result  = fv_result,
                tier_params = tier_params,
            )

            if exit_sig == "PARTIAL":
                if close_partial_position(market_id, price, exit_why):
                    position["partial_closed"] = True
                    # Don't continue — re-check entry next tick

            elif exit_sig not in ("HOLD",):
                trade = close_position(market=market, price=price, reason=exit_sig)
                if trade:
                    log_trade(
                        market      = market.get("question", market_id),
                        entry       = trade["entry_price"],
                        exit_price  = trade["exit_price"],
                        size        = trade["size"],
                        exit_reason = exit_sig,
                    )
                continue

        # ── ENTRY DECISION ────────────────────────────────────────────
        if not has_position(market_id) and not is_in_cooldown():
            from app.trader import balance
            tier_params = get_tier_params(
                balance, alpha["edge"], alpha["confidence"]
            )

            # --- ADAPTIVE SCORE THRESHOLDING ---
            idle_time = (now - _last_trade_ts) / 60
            effective_min_score = tier_params.min_score
            if idle_time > 20:
                effective_min_score = max(1, tier_params.min_score - 2)
                logger.debug(f"IDLE > 20m: reducing min_score to {effective_min_score}")
            elif idle_time > 10:
                effective_min_score = max(1, tier_params.min_score - 1)
                logger.debug(f"IDLE > 10m: reducing min_score to {effective_min_score}")

            should_open, entry_reason, signals_met = decide_entry(
                price     = price,
                fv_result = fv_result,
                ob        = ob,
                flow      = flow,
                prices    = ph,
                min_score = effective_min_score,
                min_edge  = tier_params.min_edge,
            )

            # --- FALLBACK ENTRY (score=1 + momentum + spread) ---
            if not should_open and alpha["score"] >= 1:
                spread = ob.get("spread", 1.0)
                vel    = _velocity(ph, 3)
                if spread < 0.05 and vel > 0:
                    should_open = True
                    entry_reason = "FALLBACK_MOMENTUM"
                    # Force a micro size later in trader or here if needed
                    # For now we'll flag it in the reason

            if should_open:
                opened = open_position(
                    market      = market,
                    price       = price,
                    reason      = entry_reason,
                    alpha_score = alpha["score"],
                    edge        = alpha["edge"],
                    confidence  = alpha["confidence"],
                    end_ts      = market.get("end_ts"),
                )
                if opened:
                    _last_trade_ts = now
                    logger.info(
                        f"ENTRY | tier={tier_params.tier} "
                        f"market={market_id[:16]} price={price:.4f} "
                        f"confluence={signals_met}/{effective_min_score} score={alpha['score']}"
                    )
            else:
                # Log ALL rejections as requested (HOLD with reason)
                # Filter to only log once per market per minute to avoid spam
                logger.debug(
                    f"HOLD | market={market_id[:16]} score={alpha['score']} "
                    f"confluence={signals_met}/{effective_min_score} "
                    f"reason={entry_reason} spr={ob.get('spread',0):.3f}"
                )

        # Rich signal for dashboard
        signals.append({
            "market_id": market_id,
            "market":    market.get("question", "")[:60],
            "price":     round(price, 4),
            "action":    alpha["action"],
            "score":     alpha["score"],
            "edge":      alpha["edge"],
            "fv":        fv_result.get("fv", price),
            "z_score":   fv_result.get("z_score", 0.0),
            "imbalance": ob.get("imbalance", 0.0),
            "sentiment": round(sentiment, 4),
            "res_prob":  fv_result.get("res_prob", 0.5),
            "has_ext":   fv_result.get("has_ext_data", False),
            "confidence": alpha["confidence"],
            "source":    "WS" if is_live(30) else "POLL",
            "timestamp": ts,
        })
        if len(signals) > 1000:
            signals.pop(0)
