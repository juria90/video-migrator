#!/usr/bin/env python3
"""
Tests for the measurement that decides whether a recording needs repairing.

Measuring is judged by eye in practice, which is how a metric that measured the
wrong thing survived three rounds of being reported as a finding: the first
version took a maximum over every frame and every strip, so one striped shirt
in one frame spoke for a whole sermon.

These use synthetic frames with a comb put into them deliberately, because that
is the only way to know what the answer should be. Nothing decodes a video.
"""

import pathlib
import sys

import numpy as np
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "tools"))

import measure_artifacts as measure  # noqa: E402


def combed_frames(count: int, period: float, rows: int = 512, columns: int = 1280,
                  amplitude: float = 18.0, combed_frames_count: int | None = None) -> np.ndarray:
    """
    Build frames carrying a comb of a known period.

    Picture content is a smooth vertical gradient plus noise, so that the comb
    is the only periodic thing in them and its period is not in doubt.

    :param count: How many frames
    :param period: Comb period in rows
    :param rows: Frame height
    :param columns: Frame width
    :param amplitude: How strong the comb is, in luma levels
    :param combed_frames_count: How many of the frames carry it, all by default
    :return: The frames
    """
    generator = np.random.default_rng(7)
    down = np.linspace(60, 180, rows)[:, None]
    frames = []
    for index in range(count):
        picture = down + generator.normal(0, 6, (rows, columns))
        if combed_frames_count is None or index < combed_frames_count:
            comb = amplitude * np.cos(2 * np.pi * np.arange(rows) / period)[:, None]
            picture = picture + comb
        frames.append(np.clip(picture, 0, 255))
    return np.array(frames, dtype=np.float32)


@pytest.mark.parametrize(("height", "expected"), [(720, 512), (1080, 512), (480, 256), (240, 128), (100, 64)])
def test_a_frame_is_measured_over_the_rows_it_actually_has(height, expected) -> None:
    """
    This archive is standard definition, 720p and 1080p, and a crop taller than
    the frame does not fail gracefully — ffmpeg refuses the filter outright.

    :param height: The frame's height
    :param expected: Rows to measure it over
    """
    assert measure.sample_rows(height) == expected


@pytest.mark.parametrize("period", [2.0, 4.0, 8.0])
def test_a_comb_is_found_at_the_period_it_was_put_there(period, monkeypatch) -> None:
    """
    The period is the half of this measurement that has held up throughout.

    :param period: The comb period built into the frames
    :param monkeypatch: Fixture for supplying frames instead of decoding a video
    """
    frames = combed_frames(8, period)
    monkeypatch.setattr(measure, "gray_frames", lambda *_a, **_k: frames)
    found, agreement, strength = measure.measure_comb(pathlib.Path("x.mp4"), [0.0] * 8, (1280, 512))
    assert found == period
    assert agreement == 1.0
    assert strength > measure.FRAME_PROMINENCE


def test_a_recording_with_no_comb_finds_nothing_to_agree_on(monkeypatch) -> None:
    """
    Picture content alone must not vote itself into a repair.

    :param monkeypatch: Fixture for supplying frames instead of decoding a video
    """
    generator = np.random.default_rng(3)
    frames = np.clip(np.linspace(60, 180, 512)[None, :, None] + generator.normal(0, 8, (8, 512, 1280)),
                     0, 255).astype(np.float32)
    monkeypatch.setattr(measure, "gray_frames", lambda *_a, **_k: frames)
    _found, agreement, _strength = measure.measure_comb(pathlib.Path("x.mp4"), [0.0] * 8, (1280, 512))
    assert agreement < 0.5


def test_one_loud_frame_cannot_speak_for_a_recording(monkeypatch) -> None:
    """
    The measurement this replaced took a maximum, and so reported the sample.

    A recording measured 11.4 that way on the strength of a single frame, while
    six others from it measured 1.8. Counting frames instead means one object
    passing through the shot carries one vote, not the whole verdict.

    :param monkeypatch: Fixture for supplying frames instead of decoding a video
    """
    # One frame combs ferociously; the other seven are clean.
    frames = combed_frames(8, 4.0, amplitude=60.0, combed_frames_count=1)
    monkeypatch.setattr(measure, "gray_frames", lambda *_a, **_k: frames)
    _found, agreement, _strength = measure.measure_comb(pathlib.Path("x.mp4"), [0.0] * 8, (1280, 512))
    assert agreement <= 0.25, "a single frame must not carry a recording"


def test_frames_that_move_are_the_ones_measured(monkeypatch) -> None:
    """
    A comb only shows where something moved, so sampling on a clock finds a
    still preacher at a still lectern and reports a clean recording.

    :param monkeypatch: Fixture for supplying a decoded sequence
    """
    width, height = measure.MOTION_SIZE
    # Step changes rather than spikes: a frame that differs from its neighbours
    # on both sides is two moments of movement, not one.
    levels = [60] * 5 + [120] * 6 + [180] * 6 + [240] * 3
    frames = [np.full((height, width), level, dtype=np.uint8) for level in levels]
    raw = b"".join(frame.tobytes() for frame in frames)
    monkeypatch.setattr(measure.subprocess, "run",
                        lambda *_a, **_k: type("Done", (), {"stdout": raw})())

    offsets = measure.motion_offsets(pathlib.Path("x.mp4"), 3, duration=60.0)
    # A difference belongs to the later of the two frames it spans, so a step
    # arriving at frame N is movement reported at N.
    assert sorted(offsets) == [index / measure.MOTION_FPS for index in (5, 11, 17)]


def test_a_drifting_band_is_measured_at_the_rate_it_drifts(monkeypatch) -> None:
    """
    A hum bar is one pattern moving steadily, and its rate is what a correction
    would have to be built on — so a rate that is wrong is worse than none.

    :param monkeypatch: Fixture for supplying row profiles
    """
    fps, seconds, velocity = 30.0, 120.0, 0.08
    count = int(fps * seconds)
    rows = np.arange(measure.PROFILE_ROWS)[None, :] / measure.PROFILE_ROWS
    times = (np.arange(count) / fps)[:, None]
    generator = np.random.default_rng(11)
    profiles = (128
                + 6 * np.cos(2 * np.pi * (rows - velocity * times))
                + generator.normal(0, 2, (count, measure.PROFILE_ROWS)))
    monkeypatch.setattr(measure, "row_profiles", lambda *_a, **_k: profiles.astype(np.float32))

    found, amplitude, prominence = measure.measure_hum(pathlib.Path("x.mp4"), 0.0, seconds, fps)
    assert amplitude == pytest.approx(6.0, abs=1.0), "the band's depth is recovered, not just its rate"
    # Half an FFT bin is the floor: 120 seconds at 30 fps resolves 0.0083 Hz,
    # so the peak cannot be placed more finely than that however fine the scan.
    assert found == pytest.approx(velocity, abs=0.01)
    assert amplitude > measure.HUM_AMPLITUDE
    assert prominence > measure.HUM_PROMINENCE


def test_a_recording_is_probed_for_the_things_the_crop_depends_on(monkeypatch) -> None:
    """
    Frame size decides the crop and frame rate decides the hum arithmetic, so a
    misparsed probe is a wrong measurement rather than an error.

    :param monkeypatch: Fixture for supplying ffprobe's answer
    """
    monkeypatch.setattr(measure.subprocess, "run",
                        lambda *_a, **_k: type("Done", (), {"stdout": "1280\n720\n30000/1001\n2619.75\n"})())
    duration, fps, size = measure.probe(pathlib.Path("x.mp4"))
    assert size == (1280, 720)
    assert fps == pytest.approx(29.97, abs=0.01)
    assert duration == pytest.approx(2619.75)


@pytest.mark.parametrize("velocity", [0.03, -0.03])
def test_a_band_is_found_whichever_way_it_drifts(velocity, monkeypatch) -> None:
    """
    Which way a bar moves depends on whether mains runs fast or slow of the
    frame rate, so both happen and neither may be assumed.

    A pattern moving one way has its energy at negative temporal frequency for
    positive spatial harmonics, and the other way about when it moves the other.
    Scanning one sign only measures the noise in the other half-plane — which is
    what this did, at seven millionths of the signal it was looking for.

    :param velocity: The drift built into the profiles, in heights per second
    :param monkeypatch: Fixture for supplying row profiles
    """
    fps, seconds = 30.0, 120.0
    count = int(fps * seconds)
    rows = np.arange(measure.PROFILE_ROWS)[None, :] / measure.PROFILE_ROWS
    times = (np.arange(count) / fps)[:, None]
    generator = np.random.default_rng(5)
    profiles = (128
                + 6 * np.cos(2 * np.pi * (rows - velocity * times))
                + generator.normal(0, 2, (count, measure.PROFILE_ROWS)))
    monkeypatch.setattr(measure, "row_profiles", lambda *_a, **_k: profiles.astype(np.float32))

    found, amplitude, _prominence = measure.measure_hum(pathlib.Path("x.mp4"), 0.0, seconds, fps)
    assert found == pytest.approx(abs(velocity), abs=0.01)
    assert amplitude == pytest.approx(6.0, abs=1.0)


def test_a_recording_with_no_band_reports_a_negligible_one(monkeypatch) -> None:
    """
    Noise alone must not produce an amplitude worth correcting.

    The scan always returns its best candidate, so what separates a real band
    from none is the depth found there, not the fact that something was found.

    :param monkeypatch: Fixture for supplying row profiles
    """
    generator = np.random.default_rng(9)
    profiles = 128 + generator.normal(0, 2, (3600, measure.PROFILE_ROWS))
    monkeypatch.setattr(measure, "row_profiles", lambda *_a, **_k: profiles.astype(np.float32))
    _found, amplitude, _prominence = measure.measure_hum(pathlib.Path("x.mp4"), 0.0, 120.0, 30.0)
    assert amplitude < measure.HUM_AMPLITUDE
