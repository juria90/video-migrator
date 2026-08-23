#!/usr/bin/env python3
"""Tests for the notch that cancels an aliased comb."""

import pytest

from video_migrator.media.artifacts import (
    DEINTERLACE_FILTER,
    MAX_PASSBAND_GAIN,
    NULL_TOLERANCE,
    comb_filter,
    integer_kernel,
    notch_kernel,
    repair_filter,
    response,
)


@pytest.mark.parametrize("period", [3.0, 3.5, 4.0, 4.5, 5.0])
def test_the_derived_kernel_cancels_the_comb_and_keeps_everything_else(period) -> None:
    """
    The four conditions the kernel is derived from hold at every period.

    :param period: Comb period in rows
    """
    kernel = notch_kernel(period)
    assert abs(response(kernel, 1 / period)) < 1e-9
    assert response(kernel, 0.0) == pytest.approx(1.0)
    assert response(kernel, 0.5) == pytest.approx(1.0)
    assert response(kernel, 0.5 / period) == pytest.approx(1.0)


@pytest.mark.parametrize("period", [3.0, 4.0, 5.0])
def test_rounding_to_integers_does_not_lose_the_null(period) -> None:
    """
    ffmpeg needs integer weights, and rounding must not reopen the stopband.

    Cancelling the comb constrains only two of the four distinct taps, so a
    kernel can null perfectly while the other two are rounded badly enough to
    blur the detail around it. Both are checked.

    :param period: Comb period in rows
    """
    taps, scale = integer_kernel(period)
    assert all(isinstance(tap, int) for tap in taps)
    assert scale == round(response(taps, 0.0))
    assert abs(response(taps, 1 / period)) / scale < NULL_TOLERANCE
    assert response(taps, 0.5) / scale == pytest.approx(1.0, abs=0.02)


def test_the_period_this_archive_combs_at_gives_the_kernel_it_was_repaired_with() -> None:
    """The four-row comb these masters carry derives its own known kernel."""
    assert integer_kernel(4) == ([-5, 7, 5, 14, 5, 7, -5], 28)


def test_a_period_rows_cannot_hold_is_refused() -> None:
    """Below two rows there is no pattern, and far above it there is no comb."""
    for impossible in (1.0, 2.0, 128.0):
        with pytest.raises(ValueError):
            notch_kernel(impossible)


def test_the_filter_places_the_kernel_down_the_centre_column() -> None:
    """
    A vertical notch is a column of taps, one row apart, and zeroes elsewhere.

    Spacing them two rows apart — which a 7x7 matrix invites — would notch half
    the frequency intended and leave the comb untouched.
    """
    argument = comb_filter(4)
    matrix = argument.split("'")[1].split()
    assert len(matrix) == 49
    centre = [matrix[row * 7 + 3] for row in range(7)]
    assert centre == ["-5", "7", "5", "14", "5", "7", "-5"]
    assert {matrix[row * 7 + column] for row in range(7) for column in range(7) if column != 3} == {"0"}


def test_the_filter_leaves_chroma_alone() -> None:
    """
    Chroma is subsampled vertically, so a luma kernel is the wrong one for it.

    On this archive chroma carries no comb worth filtering either, measuring at
    1.1x its own noise floor.
    """
    argument = comb_filter(4)
    assert "1rdiv=1" in argument and "2rdiv=1" in argument
    assert argument.count("0m=") == 1 and "1m=" not in argument


def test_a_comb_every_other_row_is_deinterlaced_not_notched() -> None:
    """
    Period two is interlacing itself, and both fields are still there.

    A notch cannot address it in any case — two rows is the finest pattern rows
    can carry, so there is no stopband to put anywhere.
    """
    assert repair_filter(2.0, 0.94) == DEINTERLACE_FILTER
    with pytest.raises(ValueError):
        notch_kernel(2.0)


def test_a_wider_comb_is_notched_because_no_field_survived_it() -> None:
    """A comb scaled off its own frequency has aliased, and cannot be rebuilt."""
    assert repair_filter(4.0, 0.75).startswith("convolution=")


def test_a_recording_that_does_not_comb_is_left_alone() -> None:
    """
    Filtering a clean recording costs real detail and buys nothing.

    A comb found in a handful of frames is something that passed through the
    shot, not something the recording does — which is the distinction a maximum
    could not draw and agreement can.
    """
    assert repair_filter(4.0, 0.0625) is None
    assert repair_filter(2.0, 0.0625) is None
    assert repair_filter(0.0, 0.0) is None


def test_a_comb_found_at_all_is_repaired_however_faintly() -> None:
    """
    How strongly a comb shows measures the sample, not the recording.

    The faintest recording in a 29-strong survey agreed in two frames of
    sixteen, and combs plainly when watched. Gating on strength was tried twice
    and declined to repair recordings that visibly needed it, so the period is
    what decides and severity is not asked.
    """
    assert repair_filter(4.0, 0.125).startswith("convolution=")
    assert repair_filter(2.0, 0.125) == DEINTERLACE_FILTER


def test_a_notch_too_low_to_place_safely_is_refused_rather_than_returned() -> None:
    """
    Cancelling the comb is only half the job; the rest of the picture matters too.

    Seven taps cannot hold unity at DC, at Nyquist and at the midpoint while
    notching a low frequency. The solution that satisfies those conditions at a
    period of eight rows reaches a gain of nine and inverts on the way, which
    wrecks far more than the comb it removes.
    """
    for wide in (6.0, 8.0, 12.0):
        with pytest.raises(ValueError, match="gain"):
            notch_kernel(wide)


@pytest.mark.parametrize("period", [3.0, 4.0, 5.0])
def test_a_usable_kernel_never_amplifies(period) -> None:
    """
    Whatever a kernel does to the comb, it must not make anything else louder.

    :param period: Comb period in rows
    """
    kernel = notch_kernel(period)
    assert max(abs(response(kernel, step / 400)) for step in range(201)) <= MAX_PASSBAND_GAIN


def test_an_unfixable_comb_is_left_alone_rather_than_made_worse() -> None:
    """A measured comb no kernel can safely address yields no filter at all."""
    assert repair_filter(8.0, 0.95) is None
    assert repair_filter(6.0, 0.95) is None
