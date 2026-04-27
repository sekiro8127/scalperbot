"""
ORCHESTRATOR v2.1 — All systems wired, with verbose decision logging
=====================================================================
Wires: FV (with external resolution prob) → mispricing score →
       confluence gate → tier-aware sizing →
       resolution-timing-aware exits → SQLite trade log

This version surfaces *why* trades are or are not being taken via:
  - per-tick decision summary (rate-limited per market)
  - periodic engine snapshot (top markets, scores, edges, skip reasons)
  - explicit logging of every can_open() rejection
"""
import threading
import time
import logging
from collections import deque, defaultdict
from typing import Deque, Dict, List, Tuple

from app.core.market_api     import get_markets
from app.core.orderbook      import analyze_book
from app.core.websocket      import (
    get_next_price_update, is_live, run_ws, update_subscriptions,
)
from app.alpha.fair_value    import record_price, record_trade, compute_fair_value
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
    can_open,
    balance as _balance_ref,
)

logger = logging.getLogger(__name__)

MARKET_REFRESH_SEC = 120
MARKETS_TOP_N      = 30
SENTIMENT_TTL      = 300
SKIP_LOG_INTERVAL  = 60      # one rejection log per market per 60s
SNAPSHOT_INTERVAL  = 30      # engine snapshot every 30s

signals: List[dict] = []
_sentiment_cache: Dict[str, Tuple[float, float]] = {}
_flow_cache:      Dict[str, dict] = {}
_last_ingest_ts:  Dict[str, float] = {}
_last_skip_log:   Dict[str, float] = defaultdict(float)
_skip_counter:    Dict[str, int]   = defaultdict(int)
_tick_counter:    int   = 0
_last_trade_ts:   float = 0.0           # set when engine starts
_last_snapshot:   float = 0.0
INGEST_MIN_INTERVAL = 10  # seconds


def _update_ingestion_background(token_id: str, market_id: str, question: str):
    """Background worker to fetch slow ingestion data without blocking main loop."""
    try:
        flow = get_flow(token_id)
        _flow_cache[token_id] = flow

        now = time.time()
        c = _sentiment_cache.get(market_id)
        if not c or (now - c[0]) > SENTIMENT_TTL:
            score = get_sentiment(question)
            _sentiment_cache[market_id] = (now, score)
    except Exception as e:
        logger.error(f"Ingestion error for {token_id}: {e}", exc_info=True)


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


def _maybe_log_skip(market_id: str, msg: str) -> None:
    """Rate-limited INFO-level logging of why an entry was rejected."""
    now = time.time()
    _skip_counter[market_id] += 1
    if (now - _last_skip_log[market_id]) >= SKIP_LOG_INTERVAL:
        _last_skip_log[market_id] = now
        n = _skip_counter[market_id]
        _skip_counter[market_id] = 0
        logger.info(f"SKIP[{n}x last {SKIP_LOG_INTERVAL}s] | mkt={market_id[:16]} | {msg}")


def _engine_snapshot(token_to_market):
    """Periodic high-signal log line so the user knows the engine is healthy."""
    from app.trader import balance
    n_markets = len(token_to_market)
    n_pos     = len(get_state().get("positions", []))
    age_min   = (time.time() - _last_trade_ts) / 60 if _last_trade_ts else 0
    logger.info(
        f"ENGINE SNAPSHOT | ticks={_tick_counter} markets={n_markets} "
        f"open_pos={n_pos} bal=${balance:.2f} idle={age_min:.1f}m "
        f"WS={'LIVE' if is_live(30) else 'IDLE'}"
    )


def run():
    global _last_trade_ts, _tick_counter, _last_snapshot
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger.info("ENGINE STARTING")
    _last_trade_ts = time.time()  # don't immediately trigger "idle bonus" logic

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
        try:
            update = get_next_price_update(timeout=1.0)
        except Exception as e:
            logger.error(f"price_update_error: {e}", exc_info=True)
            update = None
        now = time.time()

        # Market refresh
        if now - last_refresh >= MARKET_REFRESH_SEC:
            try:
                refreshed = get_markets(top_n=MARKETS_TOP_N)
                if refreshed:
                    markets         = refreshed
                    token_to_market = _build_market_map(markets)
                    update_subscriptions(list(token_to_market.keys()))
                    logger.info(f"MARKETS REFRESHED | {len(token_to_market)}")
            except Exception as e:
                logger.error(f"market_refresh_error: {e}", exc_info=True)
            last_refresh = now

        if now - last_status >= 20:
            _log_status()
            last_status = now

        if now - _last_snapshot >= SNAPSHOT_INTERVAL:
            _engine_snapshot(token_to_market)
            _last_snapshot = now

        if update is None:
            continue

        try:
            token_id, price, ts = update
            market = token_to_market.get(str(token_id))
            if not market:
                continue

            market_id = str(market["id"])
            price     = float(price)
            _tick_counter += 1

            # Price history
            if market_id not in price_history:
                price_history[market_id] = deque(maxlen=60)
            price_history[market_id].append(price)
            ph = list(price_history[market_id])

            record_price(token_id, price, ts)
            # Treat each WS price update as a synthetic trade so the informed
            # VWAP source can populate even without a separate trade feed.
            record_trade(token_id, price, 1.0, ts)

            # Trigger background ingestion update
            if now - _last_ingest_ts.get(token_id, 0) > INGEST_MIN_INTERVAL:
                _last_ingest_ts[token_id] = now
                threading.Thread(
                    target=_update_ingestion_background,
                    args=(token_id, market_id, market.get("question", "")),
                    daemon=True
                ).start()

            # Alpha pipeline
            ob        = analyze_book(token_id)
            ob_mid    = ob.get("mid") or price
            fv_result = compute_fair_value(
                token_id      = token_id,
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
                from app.trader import balance
                tier_params = get_tier_params(balance, alpha["edge"], alpha["confidence"])

                if market.get("end_ts") and not position.get("end_ts"):
                    position["end_ts"] = market["end_ts"]

                exit_sig, exit_why = decide_exit(
                    market_id   = market_id,
                    pos         = position,
                    price       = price,
                    prices      = ph,
                    ob          = ob,
                    fv_result   = fv_result,
                    tier_params = tier_params,
                )

                if exit_sig == "PARTIAL":
                    if close_partial_position(market_id, price, exit_why):
                        position["partial_closed"] = True

                elif exit_sig != "HOLD":
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

                # Adaptive minimum score: relax after long idle periods
                idle_min = (now - _last_trade_ts) / 60
                effective_min_score = tier_params.min_score
                if idle_min > 20:
                    effective_min_score = max(1, tier_params.min_score - 2)
                elif idle_min > 10:
                    effective_min_score = max(1, tier_params.min_score - 1)

                effective_min_edge = tier_params.min_edge
                if idle_min > 15:
                    # Halve the edge requirement after 15 min of no trades
                    effective_min_edge = max(0.003, tier_params.min_edge * 0.5)

                should_open, entry_reason, signals_met = decide_entry(
                    price     = price,
                    fv_result = fv_result,
                    ob        = ob,
                    flow      = flow,
                    prices    = ph,
                    min_score = effective_min_score,
                    min_edge  = effective_min_edge,
                )

                # Fallback: weak FV deviation + tight spread + positive momentum
                if not should_open and alpha["score"] >= 1:
                    spread = ob.get("spread", 1.0)
                    vel    = _velocity(ph, 3)
                    if spread < 0.05 and vel > 0 and alpha["edge"] >= 0.003:
                        should_open  = True
                        entry_reason = (
                            f"FALLBACK_MOMENTUM score={alpha['score']} "
                            f"edge={alpha['edge']:+.3f} vel={vel:+.4f}"
                        )
                        signals_met  = max(signals_met, 1)

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
                        _skip_counter.pop(market_id, None)
                        logger.info(
                            f"ENTRY  | tier={tier_params.tier} "
                            f"mkt={market_id[:16]} price={price:.4f} "
                            f"fv={fv_result['fv']:.4f} "
                            f"edge={alpha['edge']:+.4f} "
                            f"conf={alpha['confidence']:.2f} "
                            f"score={alpha['score']} "
                            f"confluence={signals_met}/{effective_min_score} "
                            f"reason={entry_reason}"
                        )
                    else:
                        # open_position returned None — log why
                        ok, why = can_open(
                            market_id, alpha["score"], alpha["edge"],
                            alpha["confidence"]
                        )
                        _maybe_log_skip(
                            market_id,
                            f"OPEN_BLOCKED price={price:.4f} fv={fv_result['fv']:.4f} "
                            f"edge={alpha['edge']:+.4f} score={alpha['score']} "
                            f"reason={why} entry_reason={entry_reason}"
                        )
                else:
                    _maybe_log_skip(
                        market_id,
                        f"NO_CONFLUENCE price={price:.4f} fv={fv_result['fv']:.4f} "
                        f"edge={alpha['edge']:+.4f} score={alpha['score']} "
                        f"min_score={effective_min_score} min_edge={effective_min_edge:.3f} "
                        f"detail={entry_reason}"
                    )

            # Rich signal feed for dashboard
            signals.append({
                "market_id":  market_id,
                "market":     market.get("question", "")[:60],
                "price":      round(price, 4),
                "action":     alpha["action"],
                "score":      alpha["score"],
                "edge":       alpha["edge"],
                "fv":         fv_result.get("fv", price),
                "z_score":    fv_result.get("z_score", 0.0),
                "imbalance":  ob.get("imbalance", 0.0),
                "sentiment":  round(sentiment, 4),
                "res_prob":   fv_result.get("res_prob", 0.5),
                "has_ext":    fv_result.get("has_ext_data", False),
                "confidence": alpha["confidence"],
                "source":     "WS" if is_live(30) else "POLL",
                "timestamp":  ts,
            })
            if len(signals) > 1000:
                signals.pop(0)

        except Exception as e:
            # Never let a single tick crash the engine
            logger.error(f"tick_error: {e}", exc_info=True)
