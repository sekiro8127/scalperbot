# Polymarket Bot v2

An auto-scaling Polymarket trading bot with a live FastAPI dashboard. Imported from GitHub and configured to run on Replit.

## Project Layout

```
polymarket_bot/
├── run.py                 # Entry point — starts dashboard + trading engine
├── requirements.txt       # Python dependencies
├── setup_keys.py          # One-time helper for generating CLOB API keys
├── performance.db         # SQLite trade history
└── app/
    ├── orchestrator.py    # Main trading loop wiring all systems together
    ├── trader.py          # Position management, sizing, P&L tracking
    ├── dashboard/         # FastAPI dashboard (HTML + JSON API)
    │   ├── dashboard.py   # FastAPI app
    │   ├── api.py         # JSON endpoints (/stats, /signals, /positions, …)
    │   └── index.html     # Single-page dashboard UI
    ├── core/              # Market API, orderbook, websocket, sniper, spike
    ├── alpha/             # Fair-value & mispricing models
    ├── ingestion/         # Whale flow + social sentiment
    ├── strategy/          # Decision, capital tiers, risk, exits, optimizer
    ├── execution/         # Order placement (paper / CLOB live)
    ├── features/          # Feature engineering
    ├── model/             # Probability model
    └── storage/           # Performance/trade SQLite store
```

## How It Runs on Replit

`polymarket_bot/run.py` was adapted slightly from the original:

- The dashboard binds to `0.0.0.0:5000` (driven by the `PORT` env var, default 5000) so it shows up in the Replit preview / works behind the proxy.
- The dashboard runs on the **main thread** (uvicorn) and the trading engine runs in a **background daemon thread**. This keeps the UI available even if the engine can't reach Polymarket from the sandbox.

The single workflow `Start application` runs `cd polymarket_bot && python run.py`.

## Environment Variables

Optional — the bot runs in paper mode out of the box:

| Var | Default | Purpose |
|---|---|---|
| `LIVE_TRADING` | `false` | Set `true` to trade real USDC on Polygon (requires CLOB keys + funded wallet) |
| `START_BALANCE` | `10` | Starting paper balance (USD) |
| `PORT` | `5000` | Dashboard port |
| `PRIVATE_KEY` | – | Polygon wallet private key (only needed for live trading) |
| `CLOB_API_KEY` / `CLOB_API_SECRET` / `CLOB_API_PASSPHRASE` | – | Polymarket CLOB API credentials. Generate with `python polymarket_bot/setup_keys.py` after setting `PRIVATE_KEY`. |

## Dashboard

Open the preview to see live KPIs (balance, win rate, drawdown), the current capital tier, open positions, live signals, trade history, and auto-tune output. Endpoints exposed:

- `GET /` – HTML dashboard
- `GET /stats` – core KPIs + websocket health
- `GET /capital` – current tier params
- `GET /positions` – open positions
- `GET /signals` – last 50 alpha signals
- `GET /trades` – historical trade log
- `GET /autotune` – auto-tuned thresholds

## Deployment

Configured as a **VM** deployment (always-on, in-memory state for positions and websocket connections). Run command: `python polymarket_bot/run.py`.

## Notes

- Outbound network access to `gamma-api.polymarket.com` is required for the trading engine to fetch markets. In the dev sandbox without DNS for that host, the engine will retry and the dashboard will still render with whatever historical data exists in `performance.db`.
- `performance.db` is a SQLite file in `polymarket_bot/`; it persists trade history across restarts.
