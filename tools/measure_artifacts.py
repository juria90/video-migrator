#!/usr/bin/env python3
"""
Measure the capture artifacts a recording carries, and say what would repair it.

Two faults run through this archive, and they are unrelated. Combing aliased
down to a four-row period by a downscale that skipped deinterlacing, which
:mod:`video_migrator.media.artifacts` can cancel outright. And a hum bar — a
band of slightly altered brightness drifting up the frame, left by mains
interference at capture — which is not a fixed filter at all, since the
correction differs frame by frame.

Both are measured here rather than assumed, because a filter applied to a
recording that does not need it is not free: on a clean, sharp frame the comb
notch costs real vertical detail for nothing.

Needs an FFT, and only this corner of the project does, so it lives in tools/
and is run with the dependency supplied for the one command:

    uv run --with numpy python tools/measure_artifacts.py <file>
"""

import argparse
import collections
import pathlib
import statistics
import subprocess
import sys

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from video_migrator.media.artifacts import repair_filter  # noqa: E402

#: Rows a frame is reduced to when only its vertical structure matters.
PROFILE_ROWS = 720

#: Most rows sampled from a frame for the comb measurement. A power of two, and
#: tall enough that a four-row period is resolved many times over. Frames
#: shorter than this are measured over the largest power of two they hold: this
#: archive is not one format but several, with standard-definition originals in
#: the early years and 720p later, and a crop taller than the frame does not
#: fail gracefully — ffmpeg refuses the filter outright.
COMB_ROWS = 512

#: Width of the strips a frame is measured in. The comb only appears where
#: something moved, so a whole-frame average would bury it; a narrow strip
#: catches the moving subject on its own.
STRIP_WIDTH = 64

#: How far above its neighbourhood a frame's peak must stand before that frame
#: is allowed to vote at all. Low on purpose: a frame either found a periodic
#: row pattern or it did not, and how strongly is the question this measurement
#: deliberately stopped asking.
FRAME_PROMINENCE = 1.5

#: How close to a whole number of rows a frame's peak must be to count as one.
INTEGER_TOLERANCE = 0.15

#: Vertical periods a comb could plausibly occupy, in rows.
COMB_PERIODS = (2.0, 16.0)

#: How far above the surrounding spectrum a drifting band must stand to be hum.
HUM_PROMINENCE = 8.0

#: Peak-to-peak luma below which a hum bar is not worth correcting.
HUM_AMPLITUDE = 1.0

#: How closely the drift rate measured over separate stretches must agree before
#: it can be called a property of the recording. It has to be one rate for a
#: correction to be built from it, and measuring it at one offset alone cannot
#: tell a real bar from picture content that happened to organise itself that
#: way for two minutes.
HUM_AGREEMENT = 0.25

#: Drift rates scanned for, in screen heights per second. Mains against a nearby
#: frame rate beats slowly; anything faster is the picture itself moving.
HUM_VELOCITIES = (0.02, 0.30, 0.0005)


#: Frames a second to inspect when looking for where a recording moves. The
#: comb only appears where something moved, so sampling on a clock mostly finds
#: a still preacher at a still lectern and reports a clean recording.
MOTION_FPS = 2

#: Size the picture is reduced to for that pass. Motion is a coarse question and
#: this keeps a whole sermon inside a few tens of megabytes.
MOTION_SIZE = (160, 90)


def motion_offsets(path: pathlib.Path, count: int, duration: float) -> list[float]:
    """
    Find the moments a recording moves most.

    Decodes the whole thing small and grey, at a couple of frames a second, and
    scores each against the one before it. The offsets returned are where a
    comb, if there is one, has something to comb.

    :param path: The recording
    :param count: How many offsets to return
    :param duration: The recording's length, for turning frames into seconds
    :return: Offsets in seconds, busiest first
    """
    width, height = MOTION_SIZE
    raw = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path),
         "-vf", f"fps={MOTION_FPS},scale={width}:{height},format=gray", "-f", "rawvideo", "-"],
        capture_output=True, check=True).stdout
    frames = np.frombuffer(raw, np.uint8).astype(np.int16)
    frames = frames[:len(frames) // (width * height) * width * height].reshape(-1, height * width)
    if len(frames) < 2:
        return []
    movement = np.abs(np.diff(frames, axis=0)).mean(axis=1)
    busiest = np.argsort(movement)[::-1][:count]
    # +1 because a difference belongs to the later of the two frames it spans.
    return sorted(float((index + 1) / MOTION_FPS) for index in busiest if (index + 1) / MOTION_FPS < duration)


def sample_rows(height: int) -> int:
    """
    Choose how many rows to measure a frame over.

    :param height: The frame's own height
    :return: The largest power of two that fits, capped at :data:`COMB_ROWS`

    >>> sample_rows(720), sample_rows(480), sample_rows(240)
    (512, 256, 128)
    """
    rows = 1
    while rows * 2 <= min(height, COMB_ROWS):
        rows *= 2
    return rows


def gray_frames(path: pathlib.Path, at_seconds: list[float], height: int, width: int) -> np.ndarray:
    """
    Decode one frame at each given offset, as luma only.

    :param path: The recording
    :param at_seconds: Offsets to sample at
    :param height: Rows to crop from the middle of the frame
    :param width: Columns to keep
    :return: Frames, as (count, height, width)
    """
    frames = []
    for offset in at_seconds:
        raw = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", str(offset), "-i", str(path),
             "-frames:v", "1", "-vf", f"crop={width}:{height}:(iw-{width})/2:(ih-{height})/2,format=gray",
             "-f", "rawvideo", "-"],
            capture_output=True, check=True).stdout
        if len(raw) >= height * width:
            frames.append(np.frombuffer(raw[:height * width], np.uint8).reshape(height, width))
    return np.array(frames, dtype=np.float32)


def row_profiles(path: pathlib.Path, start: float, seconds: float) -> np.ndarray:
    """
    Reduce a stretch of the recording to one mean luma per row per frame.

    A hum bar varies only down the frame and only slowly over time, so averaging
    each row to a single value keeps all of it and discards almost everything
    else — about seven hundred bytes a frame, which is what makes measuring a
    whole sermon cheap.

    :param path: The recording
    :param start: Where to start, in seconds
    :param seconds: How much to read
    :return: Profiles, as (frames, rows)
    """
    raw = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", str(start), "-t", str(seconds),
         "-i", str(path), "-vf", f"scale=1:{PROFILE_ROWS}:flags=area,format=gray", "-f", "rawvideo", "-"],
        capture_output=True, check=True).stdout
    frames = len(raw) // PROFILE_ROWS
    return np.frombuffer(raw[:frames * PROFILE_ROWS], np.uint8).astype(np.float32).reshape(frames, PROFILE_ROWS)


def measure_comb(path: pathlib.Path, at_seconds: list[float], size: tuple[int, int]) -> tuple[float, float, float]:
    """
    Find whether a recording combs, by asking how consistently it does.

    The obvious measurement — how far the strongest peak stood above its
    neighbours — turns out to measure the wrong thing. Taken as a maximum over
    every frame and every strip, it reports the single strongest accident in the
    sample: one striped shirt, one window blind, one ruled slide. A recording
    here measured 11.4 that way, and six other frames from it measured 1.8.

    So each frame votes instead. Every frame names the period it combs at most
    strongly, and the answer is the period most of them agree on, with the
    fraction that agreed. A shirt cannot carry a vote in frames it does not
    appear in, and a comb appears wherever anything moves — which is what makes
    agreement a property of the recording where a maximum is not.

    :param path: The recording
    :param at_seconds: Offsets to sample at
    :param size: The frame's width and height
    :return: The period in rows, the fraction of frames that found it, and the
        median prominence among those that did
    """
    width, height = size
    rows = sample_rows(height)
    frames = gray_frames(path, at_seconds, rows, width)
    if not len(frames):
        return 0.0, 0.0, 0.0

    frequencies = np.fft.rfftfreq(rows)
    window = np.hanning(rows)[:, None]
    low = int(np.argmin(np.abs(frequencies - 1 / COMB_PERIODS[1])))
    high = int(np.argmin(np.abs(frequencies - 1 / COMB_PERIODS[0])))

    votes: list[tuple[int, float]] = []
    for frame in frames:
        best = (0.0, 0.0)
        for left in range(0, frame.shape[1] - STRIP_WIDTH, STRIP_WIDTH):
            strip = frame[:, left:left + STRIP_WIDTH]
            strip = strip - strip.mean(axis=0, keepdims=True)
            spectrum = np.abs(np.fft.rfft(strip * window, axis=0)).mean(axis=1)
            for index in range(low, high):
                around = np.median(spectrum[max(1, index - 16):index + 17])
                if around <= 0:
                    continue
                prominence = float(spectrum[index] / around)
                if prominence > best[1]:
                    best = (float(1 / frequencies[index]), prominence)
        period, prominence = best
        # A frame whose strongest peak is not at a whole number of rows has not
        # found a comb, whatever its strength — a comb is a row pattern, and a
        # period between rows is something in the picture.
        if prominence >= FRAME_PROMINENCE and abs(period - round(period)) <= INTEGER_TOLERANCE:
            votes.append((round(period), prominence))

    if not votes:
        return 0.0, 0.0, 0.0

    counts = collections.Counter(period for period, _ in votes)
    agreed, found = counts.most_common(1)[0]
    strengths = sorted(prominence for period, prominence in votes if period == agreed)
    return float(agreed), found / len(frames), float(statistics.median(strengths))


def measure_hum(path: pathlib.Path, start: float, seconds: float, fps: float) -> tuple[float, float, float]:
    """
    Find the drifting brightness band a recording carries, if it carries one.

    A hum bar is one pattern moving at a constant rate, so in a two-dimensional
    spectrum over rows and time it is a ridge: every spatial harmonic sits at a
    temporal frequency proportional to its own. Scanning for the drift rate whose
    ridge holds the most energy finds it under picture content that is far
    stronger but not organised that way.

    :param path: The recording
    :param start: Where to start, in seconds
    :param seconds: How much to read
    :param fps: Frame rate, for turning bins into seconds
    :return: Drift in screen heights per second, peak-to-peak luma, and prominence
    """
    profiles = row_profiles(path, start, seconds)
    if len(profiles) < 64:
        return 0.0, 0.0, 0.0

    count = len(profiles)
    residual = profiles - profiles.mean(axis=1, keepdims=True)
    residual -= residual.mean(axis=0, keepdims=True)
    windowed = residual * (np.hanning(count)[:, None] * np.hanning(PROFILE_ROWS)[None, :])
    spectrum = np.fft.fft2(windowed)
    power = np.abs(spectrum) ** 2
    temporal = np.fft.fftfreq(count, 1 / fps)

    def ridge(velocity: float, direction: int) -> float:
        """
        How much energy lies along the ridge a band drifting at this rate leaves.

        A pattern moving down the frame has its energy at *negative* temporal
        frequency for positive spatial harmonics, and the other way about when
        it moves up — so the direction has to be scanned, not assumed. Sampling
        only one sign measures the noise in the other half-plane, which is what
        this did: seven million times weaker than the band it was looking for.

        :param velocity: Drift rate in screen heights per second
        :param direction: Which way it drifts, 1 or -1
        :return: The ridge's energy against its own surroundings
        """
        above = below = 0.0
        for harmonic in range(1, 7):
            index = int(np.argmin(np.abs(temporal - direction * velocity * harmonic)))
            above += power[index, harmonic]
            below += np.median(power[max(0, index - 40):index + 41, harmonic])
        return above / below if below else 0.0

    velocities = np.arange(*HUM_VELOCITIES)
    scores = np.array([[ridge(v, direction) for v in velocities] for direction in (1, -1)])
    where = np.unravel_index(int(np.argmax(scores)), scores.shape)
    direction = 1 if where[0] == 0 else -1
    velocity = float(velocities[where[1]])

    # Both halves of each harmonic: the band itself and its conjugate, which is
    # where the other half of a real signal's energy lives.
    keep = np.zeros_like(power, dtype=bool)
    for harmonic in range(1, 9):
        for sign in (1, -1):
            index = int(np.argmin(np.abs(temporal - sign * direction * velocity * harmonic)))
            keep[index - 1:index + 2, (sign * harmonic) % PROFILE_ROWS] = True
    isolated = np.real(np.fft.ifft2(spectrum * keep))
    middle = isolated[count // 4:3 * count // 4, PROFILE_ROWS // 4:3 * PROFILE_ROWS // 4]
    return velocity, float(middle.max() - middle.min()), float(scores[where])


def probe(path: pathlib.Path) -> tuple[float, float, tuple[int, int]]:
    """
    Read a recording's duration, frame rate and frame size.

    :param path: The recording
    :return: Duration in seconds, frames per second, and (width, height)
    :raises subprocess.CalledProcessError: If the file cannot be read at all
    """
    out = subprocess.run(
        ["ffprobe", "-hide_banner", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,r_frame_rate", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, check=True, text=True).stdout.split()
    width, height, rate, duration = int(out[0]), int(out[1]), out[2], float(out[3])
    numerator, _, denominator = rate.partition("/")
    return duration, float(numerator) / float(denominator or 1), (width, height)


def main() -> int:
    """
    Measure one recording and report what, if anything, would repair it.

    :return: 0 when the recording needs no repair, 1 when it does
    """
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("video", type=pathlib.Path)
    parser.add_argument("--samples", type=int, default=6, help="Frames to sample for combing (default: 6)")
    parser.add_argument("--evenly", action="store_true",
                        help="Sample on a clock rather than where the recording moves")
    parser.add_argument("--hum-seconds", type=float, default=120.0, help="Stretch to measure hum over")
    parser.add_argument("--hum-windows", type=int, default=3,
                        help="Separate stretches to measure hum over, to see whether it is one rate (default: 3)")
    args = parser.parse_args()

    duration, fps, size = probe(args.video)
    if args.evenly:
        offsets = [duration * (index + 1) / (args.samples + 1) for index in range(args.samples)]
    else:
        offsets = motion_offsets(args.video, args.samples, duration)

    period, agreement, strength = measure_comb(args.video, offsets, size)
    combed = repair_filter(period, agreement) is not None

    windows = [duration * (index + 1) / (args.hum_windows + 1) - args.hum_seconds / 2
               for index in range(args.hum_windows)]
    measured = [measure_hum(args.video, max(0.0, start), args.hum_seconds, fps) for start in windows]
    strong = [(v, a, p) for v, a, p in measured if p >= HUM_PROMINENCE and a >= HUM_AMPLITUDE]

    print(f"{args.video.name}: {duration:.0f}s at {fps:.3f} fps, {size[0]}x{size[1]}")
    print(f"  comb : period {period:.0f} rows in {agreement:.0%} of frames, median {strength:.1f}x"
          f" -> {'REPAIR' if combed else 'none'}")
    for start, (velocity, amplitude, prominence) in zip(windows, measured, strict=True):
        print(f"  hum  : from {max(0.0, start):6.0f}s  {velocity:.4f} heights/s, "
              f"{amplitude:5.2f} luma peak-to-peak, {prominence:5.1f}x")

    if combed:
        print(f"\n  -vf {repair_filter(period, agreement)}")

    if not strong:
        print("\n  hum: nothing stands far enough above the picture to call a bar.")
        return 1 if combed else 0

    rates = [velocity for velocity, _, _ in strong]
    spread = (max(rates) - min(rates)) / max(rates)
    if len(strong) < 2 or spread > HUM_AGREEMENT:
        print(f"\n  hum: PRESENT but its drift is not one rate — {min(rates):.4f} to {max(rates):.4f}"
              f" heights/s across the stretches that detected it, a spread of {spread:.0%}."
              f"\n       A per-frame correction built on a rate that uncertain would lay down a second"
              f"\n       bar rather than remove the first. Not safe to correct on this measurement.")
    else:
        print(f"\n  hum: PRESENT and steady at {sum(rates) / len(rates):.4f} heights/s."
              f"\n       Still needs a per-frame correction, which no fixed filter expresses.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
