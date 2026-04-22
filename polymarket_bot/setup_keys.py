"""
Run ONCE to generate your Polymarket CLOB API credentials.
Usage: python setup_keys.py
"""
import os
from dotenv import load_dotenv
load_dotenv()

PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
if not PRIVATE_KEY or "your_polygon" in PRIVATE_KEY:
    print("ERROR: Set a real PRIVATE_KEY in .env first")
    raise SystemExit(1)

try:
    from py_clob_client.client import ClobClient
    from py_clob_client.constants import POLYGON
except ImportError:
    print("ERROR: pip install py-clob-client")
    raise SystemExit(1)

client = ClobClient(host="https://clob.polymarket.com", key=PRIVATE_KEY, chain_id=POLYGON)
try:
    creds = client.create_api_key()
    print("\n✅  API keys generated — paste into .env:\n")
    print(f"CLOB_API_KEY={creds.api_key}")
    print(f"CLOB_API_SECRET={creds.api_secret}")
    print(f"CLOB_API_PASSPHRASE={creds.api_passphrase}")
    print("\nKeep LIVE_TRADING=false and paper-test first.")
except Exception as e:
    print(f"ERROR: {e}")
