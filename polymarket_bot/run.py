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
from app.orchestrator import run
from app.trader import shutdown

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("localhost", port)) == 0


def _start_dashboard():
    if _port_in_use(8000):
        print("WARNING: Port 8000 in use. Dashboard skipped.")
        return
    try:
        uvicorn.run(dash_app, host="0.0.0.0", port=8000, log_level="error")
    except Exception as e:
        print(f"DASHBOARD ERROR: {e}")


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
    print("=" * 60)

    threading.Thread(target=_start_dashboard, daemon=True, name="dash").start()
    time.sleep(1)
    print("Dashboard -> http://localhost:8000\n")

    try:
        run()
    except KeyboardInterrupt:
        print("\nShutting down...")
        shutdown()
        print("Done.")
    except Exception as e:
        logging.critical(f"ENGINE CRASH: {e}", exc_info=True)
        shutdown()
