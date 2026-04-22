"""
Real-time Polymarket websocket engine.
Maintains a shared live price store and an event queue for consumers.
"""
import json
import queue
import ssl
import threading
import time
from collections import defaultdict, deque
from typing import Dict, List, Optional, Tuple

from websocket import WebSocketApp

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# Global live state used by orchestrator/strategy.
price_store: Dict[str, float] = {}
price_stream = price_store  # backwards compatibility alias
price_events: "queue.Queue[Tuple[str, float, float]]" = queue.Queue(maxsize=5000)
price_history = defaultdict(lambda: deque(maxlen=60))

_message_count = 0
_last_msg_ts = 0.0
_last_live_log_count = -1
_subscribed_ids: List[str] = []
_desired_ids: List[str] = []
_state_lock = threading.Lock()
_active_ws: Optional[WebSocketApp] = None


def _extract_updates(event: dict) -> List[Tuple[str, float]]:
    updates: List[Tuple[str, float]] = []
    if not isinstance(event, dict):
        return updates

    event_type = event.get("event_type", "")
    asset_id = event.get("asset_id") or event.get("assetId")

    if event_type == "price_change":
        for change in event.get("changes", []):
            aid = change.get("asset_id") or change.get("assetId") or asset_id
            raw = change.get("price")
            if aid is None or raw is None:
                continue
            try:
                updates.append((str(aid), float(raw)))
            except (TypeError, ValueError):
                continue
        return updates

    if event_type == "book":
        bids = event.get("bids", [])
        asks = event.get("asks", [])
        if asset_id and bids and asks:
            try:
                bid = float(bids[0]["price"])
                ask = float(asks[0]["price"])
                updates.append((str(asset_id), (bid + ask) / 2.0))
            except (KeyError, TypeError, ValueError, IndexError):
                pass
        return updates

    # Some payloads expose top-of-book directly.
    if asset_id and event.get("best_bid") is not None and event.get("best_ask") is not None:
        try:
            bid = float(event.get("best_bid"))
            ask = float(event.get("best_ask"))
            updates.append((str(asset_id), (bid + ask) / 2.0))
            return updates
        except (TypeError, ValueError):
            pass

    # Generic event fallback: captures last_trade_price and similar payloads.
    if asset_id:
        for key in ("price", "midpoint", "last_trade_price", "lastPrice"):
            raw = event.get(key)
            if raw is None:
                continue
            try:
                updates.append((str(asset_id), float(raw)))
                break
            except (TypeError, ValueError):
                continue

    return updates


def on_open(ws):
    global _subscribed_ids
    with _state_lock:
        token_ids = list(_desired_ids) if _desired_ids else list(ws.token_ids or [])
    print(f"WS CONNECTED | subscribing {len(token_ids)} tokens")
    if not token_ids:
        print("WS WARNING | no token ids to subscribe")
        return

    ws.send(json.dumps({"assets_ids": token_ids, "type": "market"}))
    with _state_lock:
        _subscribed_ids = list(token_ids)
    print(f"WS SUBSCRIBED | first tokens: {_subscribed_ids[:3]}")


def on_message(ws, message):
    global _message_count, _last_msg_ts, _last_live_log_count
    _last_msg_ts = time.time()

    try:
        payload = json.loads(message)
    except json.JSONDecodeError:
        return

    events = payload if isinstance(payload, list) else [payload]
    now = time.time()

    for event in events:
        for token_id, price in _extract_updates(event):
            price_store[token_id] = price
            price_history[token_id].append(float(price))
            _message_count += 1
            try:
                price_events.put_nowait((token_id, price, now))
            except queue.Full:
                # Drop oldest and keep the newest updates flowing.
                try:
                    price_events.get_nowait()
                except queue.Empty:
                    pass
                try:
                    price_events.put_nowait((token_id, price, now))
                except queue.Full:
                    pass

    if _message_count and _message_count % 50 == 0 and _message_count != _last_live_log_count:
        _last_live_log_count = _message_count
        print(f"WS LIVE | updates={_message_count} | tracked_tokens={len(price_store)}")


def on_error(ws, error):
    print(f"WS ERROR | {error}")


def on_close(ws, code, msg):
    global _active_ws
    print(f"WS CLOSED | code={code} msg={msg}")
    with _state_lock:
        if _active_ws is ws:
            _active_ws = None


def is_live(timeout_sec: int = 30) -> bool:
    return (time.time() - _last_msg_ts) < timeout_sec


def get_next_price_update(timeout: Optional[float] = None) -> Optional[Tuple[str, float, float]]:
    try:
        return price_events.get(timeout=timeout)
    except queue.Empty:
        return None


def get_live_trade_stats(token_id: str) -> Dict[str, float]:
    """
    Returns movement stats from live websocket history for a token.
    """
    hist = list(price_history.get(str(token_id), []))
    if not hist:
        return {"count": 0, "range": 0.0, "change": 0.0}
    prange = max(hist) - min(hist)
    pchange = hist[-1] - hist[0] if len(hist) >= 2 else 0.0
    return {"count": len(hist), "range": float(prange), "change": float(pchange)}


def update_subscriptions(token_ids: List[str]) -> None:
    global _desired_ids, _subscribed_ids
    normalized = [str(x) for x in token_ids if x is not None]
    with _state_lock:
        if normalized == _desired_ids:
            return
        _desired_ids = normalized
        active_ws = _active_ws
        current = list(_subscribed_ids)

    print(f"WS SUB UPDATE | old={len(current)} new={len(normalized)}")
    if active_ws and normalized:
        try:
            active_ws.send(json.dumps({"assets_ids": normalized, "type": "market"}))
            with _state_lock:
                _subscribed_ids = list(normalized)
            print("WS SUB UPDATE | sent live subscribe update")
        except Exception as exc:
            print(f"WS SUB UPDATE ERROR | {exc}")


def run_ws(token_ids: Optional[List[str]] = None, reconnect_delay: int = 5):
    global _desired_ids, _active_ws
    with _state_lock:
        _desired_ids = [str(x) for x in (token_ids or []) if x is not None]

    while True:
        ws = WebSocketApp(
            WS_URL,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )
        ws.token_ids = list(_desired_ids)
        with _state_lock:
            _active_ws = ws
        try:
            ws.run_forever(
                ping_interval=25,
                ping_timeout=10,
                sslopt={"cert_reqs": ssl.CERT_NONE},
            )
        except Exception as exc:
            print(f"WS RUN ERROR | {exc}")
        print(f"WS RECONNECT | sleeping {reconnect_delay}s")
        time.sleep(reconnect_delay)
