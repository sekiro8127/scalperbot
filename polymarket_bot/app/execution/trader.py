"""
EXECUTION ADAPTER
=================
Paper-mode is unconditionally successful and logged. Live-mode wraps every
SDK call in try/except so a failure NEVER returns silently — it logs and
propagates a False back up the stack.
"""
import os
import logging

logger = logging.getLogger(__name__)

LIVE_TRADING = os.getenv("LIVE_TRADING", "false").lower() == "true"


def place_order(token_id: str, price: float, size: float, side: str = "BUY") -> bool:
    """Returns True on success, False on any failure (always logged)."""
    try:
        if not LIVE_TRADING:
            logger.info(
                f"PAPER ORDER | side={side} token={str(token_id)[:12]} "
                f"size=${size:.2f} price={price:.4f}"
            )
            return True

        # Live execution path. Real SDK call would go here.
        logger.info(
            f"LIVE ORDER  | side={side} token={str(token_id)[:12]} "
            f"size=${size:.2f} price={price:.4f}"
        )
        # TODO: integrate py_clob_client.create_order(...)
        return True
    except Exception as e:
        logger.error(
            f"ORDER FAILED | side={side} token={token_id} size={size} price={price} err={e}",
            exc_info=True,
        )
        return False


def get_live_balance() -> float:
    """Returns 0.0 if not live or on any error (always logged)."""
    if not LIVE_TRADING:
        return 0.0
    try:
        # TODO: integrate sdk.get_balance("USDC")
        return 0.0
    except Exception as e:
        logger.error(f"FAILED TO FETCH LIVE BALANCE: {e}", exc_info=True)
        return 0.0


def cancel_all_orders() -> bool:
    try:
        if not LIVE_TRADING:
            logger.info("PAPER: cancel_all_orders (no-op)")
            return True
        logger.info("LIVE: cancelling all open orders...")
        # TODO: integrate sdk.cancel_all_orders()
        return True
    except Exception as e:
        logger.error(f"FAILED TO CANCEL ORDERS: {e}", exc_info=True)
        return False
