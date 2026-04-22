"""
FEATURE ENGINEERING
Rich feature set for the probability model.
"""
import statistics


def build_features(sentiment, prices, imbalance, whale_pressure=0.0, spread=0.05):
    n = len(prices)
    if n < 5:
        return {
            "sentiment": sentiment, "momentum": 0, "volume": 0,
            "volatility": 0, "whale_pressure": whale_pressure,
            "spread_penalty": spread, "mean_reversion": 0,
        }

    # Momentum: short and medium term
    mom_short  = (prices[-1] - prices[-3]) / prices[-3] if prices[-3] else 0
    mom_medium = (prices[-1] - prices[-5]) / prices[-5] if prices[-5] else 0

    # Volatility: std dev of last 10
    sample = prices[-10:] if n >= 10 else prices
    try:
        vol = statistics.stdev(sample) if len(sample) > 1 else 0
    except Exception:
        vol = 0

    # Mean reversion signal: distance from 20-period mean
    sample20 = prices[-20:] if n >= 20 else prices
    mean20   = sum(sample20) / len(sample20)
    mean_rev = (mean20 - prices[-1]) / mean20 if mean20 else 0

    # Orderbook as volume proxy (normalised)
    ob_norm = (imbalance + 1) / 2

    return {
        "sentiment":      sentiment,
        "momentum":       mom_medium,
        "mom_short":      mom_short,
        "volume":         ob_norm,
        "volatility":     vol,
        "whale_pressure": whale_pressure,
        "spread_penalty": min(spread / 0.10, 1.0),
        "mean_reversion": mean_rev,
    }
