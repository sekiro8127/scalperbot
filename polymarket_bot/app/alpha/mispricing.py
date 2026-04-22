"""
MISPRICING SCORER — unchanged from v1 (already good)
Scores: FV deviation, Z-score, OB, flow divergence, failed breakout, whale, spoof
"""
from collections import defaultdict, deque

_velocity_hist = defaultdict(lambda: deque(maxlen=20))


def _velocity(prices, n=3):
    if len(prices) < n + 1: return 0.0
    return (prices[-1] - prices[-(n + 1)]) / n


def _detect_failed_breakout(market_id, prices):
    if len(prices) < 8: return False
    recent = prices[-8:]
    high = max(recent[:5]); low = min(recent[:5])
    curr = prices[-1]; prev = prices[-2]
    if prev > high * 1.008 and curr < high: return True
    if prev < low  * 0.992 and curr > low:  return True
    return False


def _flow_divergence(imbalance, absorption, prices, whale_sig):
    if len(prices) < 4: return 0
    price_dir = 1 if prices[-1] > prices[-3] else (-1 if prices[-1] < prices[-3] else 0)
    ob_dir = 1 if imbalance > 0.15 else (-1 if imbalance < -0.15 else 0)
    if absorption == -1: ob_dir = -1
    elif absorption == 1: ob_dir = 1
    if price_dir == 0 or ob_dir == 0: return 0
    if price_dir == 1 and ob_dir == -1: return -1
    if price_dir == -1 and ob_dir == 1: return 1
    return 0


def score_mispricing(market_id, token_id, current_price, fv_result, prices, ob, flow):
    score = 0; reasons = []

    fv        = fv_result.get("fv",           current_price)
    deviation = fv_result.get("deviation",    0.0)
    z_score   = fv_result.get("z_score",      0.0)
    fv_conf   = fv_result.get("confidence",   0.0)
    overext   = fv_result.get("overextended", False)
    fv_dir    = fv_result.get("direction",    0)
    hist_len  = fv_result.get("history_len",  0)

    imbalance  = ob.get("imbalance",         0.0)
    absorption = ob.get("absorption_signal", 0)
    spoof_bid  = ob.get("spoof_bid",         False)
    spoof_ask  = ob.get("spoof_ask",         False)
    ob_valid   = ob.get("valid",             False)
    thin       = ob.get("liquidity_thin",    False)

    whale_sig = flow.get("signal",   0)
    whale_prs = flow.get("pressure", 0.0)
    edge      = fv - current_price

    # A. FV deviation
    if abs(deviation) > 0.05:
        score += fv_dir * 4; reasons.append(f"FV_dev+4({deviation:+.3f})")
    elif abs(deviation) > 0.03:
        score += fv_dir * 3; reasons.append("FV_dev+3")
    elif abs(deviation) > 0.02:
        score += fv_dir * 2; reasons.append("FV_dev+2")
    elif abs(deviation) > 0.01:
        score += fv_dir * 1; reasons.append("FV_dev+1")

    # B. Z-score overextension
    if hist_len >= 10 and overext:
        if z_score > 2.2:    score -= 4; reasons.append(f"z_OVX_dn({z_score:.1f})")
        elif z_score > 1.8:  score -= 2; reasons.append("z_dn")
        elif z_score < -2.2: score += 4; reasons.append("z_OVX_up")
        elif z_score < -1.8: score += 2; reasons.append("z_up")

    # C. Orderbook
    if ob_valid and not thin:
        if imbalance > 0.25:   score += 3; reasons.append("OB_bid+3")
        elif imbalance > 0.12: score += 2; reasons.append("OB_bid+2")
        elif imbalance > 0.05: score += 1; reasons.append("OB_bid+1")
        elif imbalance < -0.12: score -= 2; reasons.append("OB_ask-2")
        if absorption == 1:    score += 2; reasons.append("ABSORP+2")
        elif absorption == -1: score -= 2; reasons.append("ABSORP-2")

    # D. Flow divergence
    div = _flow_divergence(imbalance, absorption, prices, whale_sig)
    if div == 1:   score += 2; reasons.append("FLOW_DIV_bull")
    elif div == -1: score -= 2; reasons.append("FLOW_DIV_bear")

    # E. Failed breakout
    if _detect_failed_breakout(market_id, prices):
        vel = _velocity(prices, 3)
        if vel > 0: score -= 2; reasons.append("FAIL_BREAK_dn")
        else:       score += 2; reasons.append("FAIL_BREAK_up")

    # F. Whale flow
    if whale_sig == 1:
        boost = 2 + (1 if whale_prs > 0.4 else 0)
        score += boost; reasons.append(f"WHALE+{boost}")
    elif whale_sig == -1:
        score -= 1; reasons.append("WHALE-1")

    # G. Spoof adjustments
    if spoof_bid: score -= 1; reasons.append("SPOOF_bid")
    if spoof_ask: score += 1; reasons.append("SPOOF_ask")

    # H. Mean Reversion Adjustment (New)
    # If Z-score is neutral but price is far from FV, add minor boost
    if abs(z_score) < 1.0 and abs(deviation) > 0.02:
        score += fv_dir * 1; reasons.append("MEAN_REV+1")

    # I. Extreme price guard
    if current_price > 0.90 or current_price < 0.10:
        score = int(score * 0.4); reasons.append("EXTREME")

    action = "STRONG_BUY" if score >= 7 else "BUY" if score >= 3 else "HOLD"
    confidence = min(1.0, fv_conf * 0.4 + min(len(reasons), 6) / 12)

    return {
        "score":      score,
        "action":     action,
        "edge":       round(edge,       4),
        "fv":         round(fv,         4),
        "deviation":  round(deviation,  4),
        "z_score":    round(z_score,    3),
        "confidence": round(confidence, 3),
        "reasons":    reasons,
    }
