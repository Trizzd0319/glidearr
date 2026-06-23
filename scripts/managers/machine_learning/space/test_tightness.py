"""Tests for the acquisition space-tightness signal — the band-edge ramp and the anti-oscillation
hysteresis."""
from __future__ import annotations

from scripts.managers.machine_learning.space.tightness import (
    tightness,
    tightness_with_hysteresis,
)


def test_zero_above_the_band_and_one_at_the_floor():
    floor = 100.0                                       # band 0.30 → tightening starts at 130 GB free
    assert tightness(200, floor) == 0.0                 # comfortable
    assert tightness(130, floor) == 0.0                 # exactly at the top of the band
    assert tightness(100, floor) == 1.0                 # at the floor
    assert tightness(80, floor) == 1.0                  # below the floor → fully tight


def test_linear_ramp_between_floor_and_band_top():
    floor = 100.0                                       # ramp over [100, 130]
    assert tightness(115, floor) == 0.5                 # midpoint
    assert round(tightness(124, floor), 3) == 0.2       # (130-124)/30


def test_custom_band_moves_the_engage_point():
    assert tightness(150, 100, band=0.50) == 0.0        # band top = 150
    assert tightness(149, 100, band=0.50) > 0.0


def test_no_tightening_without_a_meaningful_floor_or_band():
    assert tightness(50, 0) == 0.0                      # no floor → abundance
    assert tightness(50, -10) == 0.0
    assert tightness(50, 100, band=0) == 0.0
    assert tightness(None, 100) == 0.0                  # non-numeric → 0
    assert tightness(50, "x") == 0.0


def test_hysteresis_holds_tight_until_extra_recovery():
    floor = 100.0                                       # engage < 130; release only past 140 (margin 0.10)
    # Not engaged yet: 135 GB is above the 130 engage point → still 0.
    assert tightness_with_hysteresis(135, floor, prev_t=0.0) == 0.0
    # Drop into the band → engages.
    engaged = tightness_with_hysteresis(125, floor, prev_t=0.0)
    assert engaged > 0.0
    # Recover to 135 WHILE engaged: normal band would release (0), but hysteresis holds it positive.
    assert tightness_with_hysteresis(135, floor, prev_t=engaged) > 0.0
    # Recover past the wider release mark (>140) → finally releases.
    assert tightness_with_hysteresis(141, floor, prev_t=engaged) == 0.0


def test_hysteresis_matches_plain_tightness_when_never_engaged():
    floor = 100.0
    for free in (90, 110, 125, 135, 200):
        assert tightness_with_hysteresis(free, floor, prev_t=0.0) == tightness(free, floor)


def test_no_oscillation_over_a_hovering_sequence():
    # Free space hovers around the band edge (135 ↔ 128). WITHOUT hysteresis this flips
    # released→engaged→released every step (grab→fill→delete→grab). WITH it, once engaged at 128 it
    # stays engaged at 135 (below the 140 release mark) — a stable fixed point, not a limit cycle.
    floor, t, engaged = 100.0, 0.0, []
    for free in (135, 128, 135, 128, 135):
        t = tightness_with_hysteresis(free, floor, t)
        engaged.append(t > 0)
    assert engaged == [False, True, True, True, True]    # engages once, never flips back

    # Sanity: the SAME sequence with plain tightness DOES oscillate (proves the test isn't vacuous).
    plain = [tightness(free, floor) > 0 for free in (135, 128, 135, 128, 135)]
    assert plain == [False, True, False, True, False]
