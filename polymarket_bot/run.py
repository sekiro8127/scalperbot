"""
ENTRY POINT — Polymarket Bot v2
"""
import os
import socket
import threading
import time
import logging
import uvicorn

from app.dashboard.dashboard import app as dash_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

logger = logging.getLogger("entrypoint")

DASHBOARD_PORT = int(os.getenv("PORT", "5000"))


def _start_engine():
    """Run the trading orchestrator in a background thread.

    Imported lazily so the dashboard can come up even if the engine
    can't reach external services (e.g. no network/credentials).
    """
    try:
        from app.orchestrator import run
        run()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logger.error(f"ENGINE CRASH: {e}", exc_info=True)
        try:
            from app.trader import shutdown
            shutdown()
        except Exception:
            pass


if __name__ == "__main__":
    live    = os.getenv("LIVE_TRADING", "false").lower() == "true"
    balance = float(os.getenv("START_BALANCE", "10"))

    print("=" * 60)
    if live:
        print("  [LIVE]  LIVE TRADING — REAL USDC ON POLYGON")
        print("  Ctrl+C to stop. Open orders will be cancelled.")
    else:
        print("  [PAPER]  PAPER TRADING MODE")
        print("  Set LIVE_TRADING=true in .env when ready to go live.")
    print(f"  Starting balance: ${balance:.2f}")
    print(f"  Dashboard -> http://0.0.0.0:{DASHBOARD_PORT}")
    print("=" * 60)

    # Start the trading engine in the background. The dashboard runs
    # in the main thread so the UI stays available even if the engine
    # can't reach Polymarket.
    threading.Thread(target=_start_engine, daemon=True, name="engine").start()

    try:
        uvicorn.run(
            dash_app,
            host="0.0.0.0",
            port=DASHBOARD_PORT,
            log_level="info",
        )
    except KeyboardInterrupt:
        print("\nShutting down...")
        try:
            from app.trader import shutdown
            shutdown()
        except Exception:
            pass
        print("Done.")
