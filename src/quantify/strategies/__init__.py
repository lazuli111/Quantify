from . import gap_reversal, in_play_orb, late_momentum, orb, vwap_pullback

STRATEGIES = {
    "orb": orb,
    "in_play_orb": in_play_orb,
    "vwap_pullback": vwap_pullback,
    "gap_reversal": gap_reversal,
    "late_momentum": late_momentum,
}
