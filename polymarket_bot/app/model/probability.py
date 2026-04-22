"""
PROBABILITY MODEL
Weighted logistic model with mean-reversion awareness.
"""
import math

WEIGHTS = {
    "sentiment":      0.20,
    "momentum":       0.28,
    "mom_short":      0.15,
    "volume":         0.18,
    "volatility":    -0.10,
    "whale_pressure": 0.12,
    "spread_penalty":-0.08,
    "mean_reversion": 0.10,
}


def predict(features):
    score = sum(WEIGHTS.get(k, 0) * features.get(k, 0) for k in WEIGHTS)
    return 1 / (1 + math.exp(-score * 4))
