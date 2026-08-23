#!/usr/bin/env python3
"""
Build the filter that takes a capture artifact back out of a recording.

This archive's masters carry combing that no deinterlacer will touch. The
recordings are flagged progressive and ``idet`` agrees — zero top-field, zero
bottom-field and zero repeated fields across the whole file — yet moving
subjects comb visibly. Measuring the vertical spectrum explains why: the comb
sits at a period of four rows, not the two that interlacing produces, and there
is nothing at two rows at all.

Four rows is what a two-row comb becomes when an interlaced frame is weaved and
then scaled down without being deinterlaced first. At the source height the comb
sits above the reduced Nyquist limit, so it aliases, folding onto a frequency
that is a legitimate part of the picture. That is the whole difficulty: the
original comb frequency is gone and a deinterlacer has no fields left to
reconstruct from. What remains is a periodic pattern at one precisely known
frequency, which is what a notch filter is for.

The kernel is derived from the measured period rather than hardcoded, so a
recording whose chain scaled by some other ratio gets the notch its own
measurement calls for. Measuring is :mod:`tools.measure_artifacts`, which needs
an FFT and so is kept out of the package.
"""

import math

#: Taps in the vertical kernel. Three either side of centre is the narrowest
#: filter that leaves the passband usable: the five-tap equivalent cancels the
#: comb just as completely but keeps under a third of the real detail either
#: side of it, where seven keeps over four fifths.
TAPS = 7

#: How exactly an integer-rounded kernel must still cancel the comb, relative to
#: its own passband gain.
NULL_TOLERANCE = 1e-4

#: How far a rounded tap may sit from the derived one. Cancelling the comb
#: constrains only two of the four taps, so a coarse rounding can null perfectly
#: while distorting the passband the other two shape — which is the difference
#: between keeping the detail around the comb and blurring it.
TAP_TOLERANCE = 0.01

#: Integer scales to try when rounding, smallest first, so the kernel stays as
#: small as accuracy allows.
MAX_SCALE = 4096

#: The most a kernel may amplify anything, anywhere in the passband.
#:
#: Cancelling the comb is only half of what a repair has to do. Seven taps
#: cannot put a narrow notch at a low frequency *and* hold unity at DC, at
#: Nyquist and at the midpoint — asked for all four at a period of eight rows,
#: the solution that satisfies them reaches a gain of nine and inverts as it
#: goes, which does far more damage than the comb it removes. Refusing is the
#: right answer: no filter is better than that one.
MAX_PASSBAND_GAIN = 1.5

#: How finely the passband is checked for that.
GAIN_STEPS = 200


def response(kernel: list[float], frequency: float) -> float:
    """
    Gain a symmetric vertical kernel applies at one spatial frequency.

    :param kernel: Taps in order, centre in the middle, of odd length
    :param frequency: Cycles per row, so 0.5 is the finest pattern rows can hold
    :return: The gain, unnormalized

    >>> round(response([1, 2, 1], 0.0), 6)
    4.0
    >>> round(abs(response([1, 2, 1], 0.5)), 6)
    0.0
    """
    middle = len(kernel) // 2
    return sum(
        tap * math.cos(2 * math.pi * frequency * (index - middle))
        for index, tap in enumerate(kernel)
    )


def notch_kernel(period: float) -> list[float]:
    """
    Derive the vertical kernel that cancels a comb of a given period.

    Symmetric, seven taps, fixed by four conditions: no gain at all at the
    comb's own frequency; unity at DC and at the Nyquist limit, so neither flat
    areas nor genuine fine detail are touched; and unity halfway to the comb, so
    the stopband stays narrow rather than taking the band around it with it.

    Unity at both DC and Nyquist forces the outermost pair to cancel the second
    (``c = -a``) and ties the centre tap to the second pair (``d = 1 - 2b``),
    leaving two conditions in two unknowns.

    :param period: Comb period in rows, as measured
    :return: The taps, summing to 1
    :raises ValueError: If the period is outside what rows can carry, or leaves
        the kernel underdetermined

    >>> [round(tap, 4) for tap in notch_kernel(4)]
    [-0.1768, 0.25, 0.1768, 0.5, 0.1768, 0.25, -0.1768]
    >>> round(abs(response(notch_kernel(4), 0.25)), 12)
    0.0
    >>> round(response(notch_kernel(4), 0.0), 6)
    1.0
    >>> round(response(notch_kernel(4), 0.5), 6)
    1.0
    """
    if not 2 < period <= 64:
        raise ValueError(f"a comb of {period} rows is not something a notch can address")

    spatial = 1 / period
    at_comb = lambda multiple: math.cos(2 * math.pi * spatial * multiple)      # noqa: E731
    at_half = lambda multiple: math.cos(math.pi * spatial * multiple)          # noqa: E731

    # b * b_null + a * a_null == -1, and b * b_flat + a * a_flat == 0.
    b_null, a_null = 2 * (at_comb(2) - 1), 2 * (at_comb(3) - at_comb(1))
    b_flat, a_flat = 2 * (at_half(2) - 1), 2 * (at_half(3) - at_half(1))

    determinant = b_null * a_flat - a_null * b_flat
    if abs(determinant) < 1e-12:
        raise ValueError(f"a comb of {period} rows leaves the kernel underdetermined")

    b = -a_flat / determinant
    a = b_flat / determinant
    kernel = [a, b, -a, 1 - 2 * b, -a, b, a]

    worst = max(abs(response(kernel, step / (2 * GAIN_STEPS))) for step in range(GAIN_STEPS + 1))
    if worst > MAX_PASSBAND_GAIN:
        raise ValueError(
            f"a notch at {period} rows would reach a gain of {worst:.1f} elsewhere in the picture; "
            f"seven taps cannot place a stopband that low without one"
        )
    return kernel


def integer_kernel(period: float) -> tuple[list[int], int]:
    """
    Round a derived kernel to the integers ffmpeg's ``convolution`` requires.

    That filter does not merely round a fractional weight, it produces a blank
    frame, so the taps have to be integers and the scaling has to travel
    separately as ``rdiv``. The smallest scale that both cancels the comb and
    keeps every tap near its derived value is taken.

    The scale returned is the kernel's own sum rather than the multiplier that
    produced it: rounding is what decides the gain, and dividing by anything
    else would shift the picture's brightness.

    :param period: Comb period in rows, as measured
    :return: The integer taps and the scale they are divided by
    :raises ValueError: If no scale in range rounds closely enough

    >>> integer_kernel(4)
    ([-5, 7, 5, 14, 5, 7, -5], 28)
    >>> taps, scale = integer_kernel(4)
    >>> round(abs(response(taps, 0.25)) / scale, 9)
    0.0
    """
    exact = notch_kernel(period)
    for scale in range(TAPS, MAX_SCALE + 1):
        taps = [round(tap * scale) for tap in exact]
        gain = response(taps, 0.0)
        if gain <= 0:
            continue
        if max(abs(tap / scale - want) for tap, want in zip(taps, exact, strict=True)) > TAP_TOLERANCE:
            continue
        if abs(response(taps, 1 / period)) / gain < NULL_TOLERANCE:
            return taps, round(gain)
    raise ValueError(f"no integer kernel within {MAX_SCALE} cancels a comb of {period} rows")


def comb_filter(period: float) -> str:
    """
    Write the ffmpeg filter that cancels a comb of a given period.

    Luma only. Chroma is subsampled vertically, so the comb would land at a
    different frequency there and need its own kernel — and on this archive it
    is not present in chroma to begin with, measuring barely above the noise
    floor.

    :param period: Comb period in rows, as measured
    :return: A ``-vf`` argument

    >>> comb_filter(4).split(":")[1]
    '0rdiv=0.0357143'
    """
    taps, scale = integer_kernel(period)
    width = len(taps)
    centre = width // 2
    matrix = " ".join(
        str(taps[row]) if column == centre else "0"
        for row in range(width)
        for column in range(width)
    )
    return f"convolution=0m='{matrix}':0rdiv={1 / scale:g}:1rdiv=1:2rdiv=1"


#: How many of the frames measured must find the same comb before it counts as
#: found at all.
#:
#: Low, because two attempts at gating on strength both failed the same way. How
#: strongly a comb shows measures how much moved in the frames that happened to
#: be sampled, not whether the recording combs: the faintest recording in a
#: 29-strong survey — one frame in eight, weaker than the noise around it — was
#: looked at and combs plainly. What the vote is good for is the *period*, which
#: is unanimous across the archive; what it cannot judge is severity.
#:
#: So this asks only that some frames agreed rather than none. Recordings that
#: do not comb do not land here: a standard-definition recording from this
#: archive votes period 2 in 6% of frames, which is scatter, and is left alone
#: by the period test rather than by this one.
#:
#: One tenth sits between those two observations rather than on either: the
#: faintest recording known to comb agreed in two frames of sixteen, and the
#: clean one in one of sixteen.
COMB_AGREEMENT = 0.1

#: A comb at this period is interlacing itself: alternate rows, from two moments
#: woven into one frame. Both fields survive, so a deinterlacer can rebuild the
#: picture from them, and nothing else should be attempted.
INTERLACE_PERIOD = 2.0

#: How far a measured period may sit from a whole number of rows and still be
#: read as that period.
PERIOD_TOLERANCE = 0.15

#: Deinterlacing this archive's genuinely interlaced recordings. ``send_frame``
#: keeps the frame rate; ``send_field`` would double it and restore the motion
#: the fields were captured at, which is a decision about the result rather than
#: about the fault. ``parity=auto`` because field order is not consistent here —
#: one 2024 recording measured 190 top-first against 224 bottom-first.
DEINTERLACE_FILTER = "bwdif=mode=send_frame:parity=auto:deint=all"


def repair_filter(period: float, agreement: float) -> str | None:
    """
    Choose what, if anything, to do about a recording's combing.

    Three outcomes, because this archive holds three faults and they are not
    interchangeable:

    * A comb every two rows is interlacing that was never deinterlaced. Both
      fields are intact, so :data:`DEINTERLACE_FILTER` reconstructs from them —
      and a notch could not help anyway, two rows being the finest pattern rows
      can carry.
    * A comb at some wider period is interlacing that was woven and *then*
      scaled down, folding the comb onto a frequency the picture also uses.
      No field survives that, so the notch is the most that can be done. It is
      applied wherever the period is found, without asking how strongly: a comb
      that shows faintly in the frames sampled is not a fainter comb, only a
      stiller passage.
    * Anything not standing clear of the picture is left alone. Filtering a
      clean recording is not free — it costs real vertical detail for nothing.

    :param period: Comb period in rows, as measured
    :param agreement: The fraction of measured frames that found it
    :return: A ``-vf`` argument, or None to pass the recording through untouched

    >>> repair_filter(2.0, 0.94) == DEINTERLACE_FILTER
    True
    >>> repair_filter(4.0, 0.75).startswith("convolution=")
    True
    >>> repair_filter(4.0, 0.125).startswith("convolution=")
    True
    >>> repair_filter(4.0, 0.0625) is None
    True
    >>> repair_filter(0.0, 0.0) is None
    True

    A comb no kernel can address without wrecking the picture is left in place:

    >>> repair_filter(8.0, 0.9) is None
    True
    """
    if agreement < COMB_AGREEMENT:
        return None
    if abs(period - INTERLACE_PERIOD) <= PERIOD_TOLERANCE:
        return DEINTERLACE_FILTER
    rounded = round(period)
    if abs(period - rounded) > PERIOD_TOLERANCE or rounded <= INTERLACE_PERIOD:
        return None
    try:
        return comb_filter(rounded)
    except ValueError:
        return None
