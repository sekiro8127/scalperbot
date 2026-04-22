def detect_spike(prices):
    if len(prices) < 5:
        return False

    prev = prices[-5]
    current = prices[-1]

    if prev == 0:
        return False

    change = abs(current - prev) / prev

    return change > 0.08
