"""
SIZE OPTIMIZER
===============
Scale-in logic for HIGH/WHALE tiers.
Adds to a winning position when conditions remain strong.
"""


def scale_in_size(current_size: float, gain_pct: float, alpha_score: int,
                  tier: str) -> float:
    """
    Returns additional size to add to a winning position.
    Only called for HIGH/WHALE tiers with scale_in=True.
    Returns 0 if conditions not met.
    """
    if tier not in ("HIGH", "WHALE"):
        return 0.0
    if gain_pct < 0.05:        # must be +5% before scaling in
        return 0.0
    if alpha_score < 3:        # signal must still be strong
        return 0.0
    # Add 30% of original size
    return round(current_size * 0.30, 2)


def adjust_for_edge(base_size: float, edge: float) -> float:
    """Small edge boost on top of Kelly sizing."""
    if edge > 0.15:   return base_size * 1.20
    if edge > 0.10:   return base_size * 1.10
    if edge > 0.05:   return base_size * 1.00
    return base_size * 0.80
