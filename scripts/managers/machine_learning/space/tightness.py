"""Acquisition space-tightness — the ``t ∈ [0,1]`` knob that demand-aware acquisition rides.

``t`` rises as free space approaches the ``space_targets`` floor ``T`` (``free_space_limit``, or 25% of
the drive): ``t=0`` while there's comfortable headroom, ``t=1`` at/below the floor, a linear ramp between.
A demand-aware acquisition ranker uses it as ``priority = watchability × demand^t`` — demand is inert when
roomy and dominates at the floor, so a shrinking budget fills with media many users will watch.

This is a SEPARATE, WIDER band from the deletion pressure band (``space_pressure_headroom_ratio`` ≈ 10%):
acquisition should get selective (~30% above the floor) BEFORE deletion territory (~10%). PURE — the caller
supplies free GB (``storage`` disk_free_gb) and the floor (``space_targets`` ``T``); hysteresis takes the
previous ``t`` (persisted by the caller) so the mode can't oscillate as free space hovers at the band edge.
"""
from __future__ import annotations


def tightness(free_gb, floor_gb, *, band: float = 0.30) -> float:
    """``t ∈ [0,1]``: 0 when ``free_gb ≥ floor_gb·(1+band)`` (comfortable), 1 at/below the floor, linear
    between. ``band`` is the acquisition headroom fraction above the floor where tightening begins
    (default 0.30). Returns 0.0 (no tightening — fail to abundance) when the floor or band is non-positive
    or the inputs aren't numeric."""
    try:
        free = float(free_gb)
        floor = float(floor_gb)
        band = float(band)
    except (TypeError, ValueError):
        return 0.0
    if floor <= 0 or band <= 0:
        return 0.0
    top = floor * (1.0 + band)
    if free >= top:
        return 0.0
    if free <= floor:
        return 1.0
    return (top - free) / (top - floor)        # == (top - free) / (floor * band)


def tightness_with_hysteresis(free_gb, floor_gb, prev_t, *, band: float = 0.30,
                              release_margin: float = 0.10) -> float:
    """``tightness`` with directional hysteresis (a Schmitt trigger on the tightening regime): tightening
    ENGAGES when free falls below ``floor·(1+band)``, but only RELEASES to 0 once free recovers past the
    higher ``floor·(1+band+release_margin)``. While engaged (``prev_t > 0``) the wider release band is used,
    so a download finishing right at the edge can't flip the mode back and forth. ``prev_t`` is the caller's
    last persisted ``t`` (0.0 on the first run)."""
    try:
        engaged = float(prev_t) > 0.0
    except (TypeError, ValueError):
        engaged = False
    effective_band = band + max(0.0, release_margin) if engaged else band
    return tightness(free_gb, floor_gb, band=effective_band)
