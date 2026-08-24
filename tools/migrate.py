#!/usr/bin/env python3
"""
Carry recordings from the source archive to YouTube, one stage at a time.

Each recording passes through five stages — fetch, measure, repair, upload,
release — and each stamps the plan as it finishes. A run does as many as it is
told to and stops; the next run reads the plan and continues from wherever the
last one stopped, including part way through a single recording.

That matters more than it sounds. This is hundreds of gigabytes over weeks,
against two services that rate-limit, on a machine that will be rebooted. The
question is never whether a run will be interrupted, only what it costs when it
is — and the answer here is one stage of one recording.

Repair is decided per recording from what is measured, not from the year: this
archive's formats change mid-year and its faults are not the same fault. A
recording measuring nothing is passed through untouched, which is not merely
cheap but correct — filtering a clean recording costs real detail.

Needs an FFT to measure, so it is run with numpy supplied for the command:

    uv run --with numpy python tools/migrate.py --board <export>.tsv --limit 1
"""

import argparse
import concurrent.futures
import contextlib
import csv
import logging
import os
import pathlib
import signal
import subprocess
import sys
import threading
import traceback

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from measure_artifacts import measure_comb, motion_offsets, probe  # noqa: E402

from video_migrator.cli import selects  # noqa: E402
from video_migrator.config import load_profile  # noqa: E402
from video_migrator.logs import Ticker, configure  # noqa: E402
from video_migrator.media.artifacts import repair_filter  # noqa: E402
from video_migrator.metadata.upload_title import format_upload_title  # noqa: E402
from video_migrator.models import Video  # noqa: E402
from video_migrator.plan import (  # noqa: E402
    DONE,
    merge,
    now,
    outstanding,
    read_plan,
    reset,
    stage_of,
    tally_stages,
    write_plan,
)
from video_migrator.sinks.youtube import (  # noqa: E402
    REFUSED,
    confirm_upload,
    create_argument_parser,
    get_authenticated_service,
    upload,
    upload_arguments,
)
from video_migrator.sources.vimeo_api import VimeoAPI, load_token  # noqa: E402

#: Frames measured per recording when deciding whether to repair it.
MEASURE_SAMPLES = 16

#: What a repaired file is called, beside the master it came from.
REPAIRED_SUFFIX = ".repaired.mp4"

#: How hard x264 works on a repaired recording.
#:
#: Measured on this archive's own material — a 720p sermon carrying the notch,
#: on an idle sixteen-core machine. SSIM is against the filter's own output,
#: before any lossy encoder has seen it:
#:
#: ============  ==========  ========  =======
#: preset        throughput  size      SSIM
#: ============  ==========  ========  =======
#: ``medium``    2.77x       12.5 MiB  0.99349
#: ``fast``      2.87x       13.0 MiB  0.99347
#: ``veryfast``  3.54x       10.7 MiB  0.99260
#: ============  ==========  ========  =======
#:
#: The temptation is ``veryfast``, which is quickest and produces the *smallest*
#: file — but that is not compression, it is loss, and it is the only one whose
#: SSIM moves. At a fixed CRF a weaker preset finds fewer efficient ways to
#: carry detail and so discards more of it. These are archival copies, and the
#: notch has already given up some vertical detail; stacking a weaker encoder on
#: top compounds that.
#:
#: ``fast`` and ``medium`` are indistinguishable in quality and nearly so in
#: speed, because the encoder is not what the time goes on. Decoding the same
#: clip runs at 40x and the notch at 4.6x, which puts x264 itself at roughly
#: 7.7x: the filter costs about twice what the encoder does, so a weaker preset
#: cannot buy much however weak it gets. ``fast`` for the margin it does give.
#: Override with --preset where throughput matters more.
DEFAULT_PRESET = "fast"

#: Which stages may not run beside a copy of themselves.
#:
#: The stages do not want the same thing. Fetch and upload are network and spend
#: their time waiting; measure and repair are ffmpeg and take every core they can
#: get. Run one recording at a time and the machine alternates between saturating
#: the link and saturating the processor, each idle while the other works.
#:
#: What must not overlap is two transfers, which would only halve each other's
#: bandwidth, or two encodes, which would halve each other's throughput. So the
#: stages are gated by the resource they contend for rather than by recording,
#: and the overlap that is left — an encode running while the next recording
#: downloads and the last one uploads — is free.
#:
#: Release touches neither: it deletes a file.
CONTENDS_FOR = {"fetch": "network", "upload": "network", "measure": "cpu", "repair": "cpu"}

#: What YouTube says when the account has published as many videos today as it
#: is allowed to. This is a cap on videos per day and is not the API quota; it
#: does not ease off after a few minutes, and nothing further will upload until
#: it resets.
#:
#: So it ends the run rather than failing one recording. Left to carry on, every
#: remaining recording is fetched, measured and encoded in turn — twenty minutes
#: each — only to arrive at the same refusal, and a night's run produces nothing
#: but a queue of repaired files and six hundred identical tracebacks.
DAILY_LIMIT = "uploadLimitExceeded"

#: The encodes running right now, so that a run being stopped can stop them too.
#:
#: ^C at a terminal reaches the whole foreground process group, so ffmpeg
#: usually dies alongside the run without anything here doing it. Usually is not
#: good enough: under ``nohup``, in a detached ``tmux``, or when the signal is
#: sent to this process alone, it does not — and the run then waits out an
#: encode it has already decided to abandon, which on this material is twenty
#: minutes of appearing to ignore the interrupt. Ending them here makes stopping
#: mean the same thing wherever the run was started from.
ENCODING: set[subprocess.Popen] = set()
ENCODING_LOCK = threading.Lock()


logger = logging.getLogger(__name__)


def wanted_from(board: pathlib.Path, preacher: list[str] | None,
                profile_name: str, board_name: str) -> list[dict]:
    """
    Read the export and work out what each recording should be published as.

    :param board: A scraped export
    :param preacher: Preacher patterns, or None for every preacher; see
        :func:`~video_migrator.cli.selects`
    :param profile_name: Profile supplying the upload title template
    :param board_name: Which board's service naming to use
    :return: One entry per recording, carrying ``num``, ``ID`` and ``title``
    """
    profile = load_profile(profile_name)
    board_config = profile.board(board_name)
    wanted = []
    with board.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle, delimiter="\t"):
            if row["Type"] != "vimeo":
                continue
            if not selects(row["Preacher"], preacher):
                continue
            video = Video(type=row["Type"], id=row["ID"], url=row["URL"], embed_url=row["Embed URL"],
                          title=row["Title"], bible_verse=row["Bible Verse"],
                          publish_date=row["Publish Date"], artist=row["Preacher"],
                          genre=row["Genre"], language=row["Language"])
            wanted.append({"num": row["num"], "ID": row["ID"],
                           "title": format_upload_title(video, profile, board_config),
                           "date": row["Publish Date"], "verse": row["Bible Verse"],
                           "artist": row["Preacher"], "language": row["Language"]})
    return wanted


def already_fetched(row: dict[str, str], work_dir: pathlib.Path) -> bool:
    """
    Is this recording's master already on disk, whatever the plan says?

    A survey fetches recordings to measure them and a plan knows nothing about
    it, so the two disagree: gigabytes sit in the working directory against rows
    that still read as unfetched. Asking the directory rather than the plan is
    what lets those be finished rather than fetched again.

    :param row: A plan row
    :param work_dir: Where masters are kept
    :return: Whether the file is there
    """
    return (work_dir / f"{row['num']}-{row['vimeo_id']}.mp4").exists()


def do_fetch(api: VimeoAPI, row: dict[str, str], work_dir: pathlib.Path) -> None:
    """
    Bring one recording's master to disk.

    :param api: An authenticated Vimeo session
    :param row: The plan row to advance
    :param work_dir: Where masters are kept
    :return: None
    """
    path = work_dir / f"{row['num']}-{row['vimeo_id']}.mp4"
    if not path.exists():
        api.download(row["vimeo_id"], path)
    row.update(path=str(path), gib=f"{path.stat().st_size / (1 << 30):.2f}", fetched_at=now())


def do_measure(row: dict[str, str]) -> None:
    """
    Decide what, if anything, this recording needs done to it.

    :param row: The plan row to advance
    :return: None
    """
    path = pathlib.Path(row["path"])
    duration, _fps, size = probe(path)
    period, agreement, strength = measure_comb(path, motion_offsets(path, MEASURE_SAMPLES, duration), size)
    chosen = repair_filter(period, agreement)
    row.update(period=f"{period:.0f}", prominence=f"{agreement:.0%} at {strength:.1f}x",
               repair=chosen or "none", measured_at=now())


def repaired_paths(master: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    """
    Where a repaired recording is written, and where it is written to first.

    :param master: The recording being repaired
    :return: The finished path and the part-file it is built in

    >>> finished, partial = repaired_paths(pathlib.Path("/w/2-384381619.mp4"))
    >>> finished.name, partial.name
    ('2-384381619.repaired.mp4', '2-384381619.repaired.mp4.part')
    """
    finished = master.with_suffix(REPAIRED_SUFFIX)
    # Appended rather than substituted: ``with_suffix`` replaces the extension,
    # which would leave a file called ".part" and nothing saying what is in it.
    return finished, finished.with_suffix(finished.suffix + ".part")


def is_daily_limit(exc: BaseException) -> bool:
    """
    Is this YouTube's daily cap, rather than something wrong with one recording?

    :param exc: Whatever the upload raised
    :return: Whether nothing more will upload today

    >>> is_daily_limit(ValueError("... reason: uploadLimitExceeded ..."))
    True
    >>> is_daily_limit(OSError("no route to host"))
    False
    """
    return DAILY_LIMIT in str(exc)


@contextlib.contextmanager
def held(gate: threading.Semaphore | None, num: str, stage: str):
    """
    Hold the gate a stage needs, saying so when it has to wait for it.

    A run with several recordings in flight announces all of them as they start,
    and nothing in the log then distinguishes the one that is working from the
    two queued behind the encoder. That reads as three downloads at once. Saying
    which are waiting, and for what, is the difference.

    :param gate: The semaphore for whatever this stage contends for, or None for
        a stage that contends for nothing
    :param num: The recording, so the message says whose stage is waiting
    :param stage: The stage about to run
    :return: A context holding the gate for as long as the stage runs
    """
    if gate is None:
        yield
        return
    if not gate.acquire(blocking=False):
        logger.info(f"  num={num} waiting to {stage}")
        gate.acquire()
    try:
        yield
    finally:
        gate.release()


def stop_encoding() -> int:
    """
    End every encode now running.

    Sent a terminate rather than a kill, so ffmpeg closes the file it is writing
    before it goes. The part-file is discarded either way — an encode cannot be
    resumed — but a half-flushed one is worth avoiding on principle.

    :return: How many encodes were stopped
    """
    with ENCODING_LOCK:
        running = list(ENCODING)
    for process in running:
        process.terminate()
    return len(running)


def do_repair(row: dict[str, str], crf: int, preset: str = DEFAULT_PRESET) -> None:
    """
    Produce the file that will actually be uploaded.

    Where nothing is to be done the master *is* that file, and no encode
    happens — which is most of the point of measuring first. Where something is,
    it is written beside the master and moved into place only once ffmpeg has
    finished, so an interrupted encode leaves nothing that looks complete.

    :param row: The plan row to advance
    :param crf: Quality for the re-encode, lower being better
    :param preset: How hard x264 works; see :data:`DEFAULT_PRESET`
    :return: None
    :raises subprocess.CalledProcessError: If ffmpeg fails
    """
    if row["repair"] in ("", "none"):
        row["repaired_at"] = now()
        return

    master = pathlib.Path(row["path"])
    repaired, partial = repaired_paths(master)
    duration, _fps, _size = probe(master)

    # ``-progress pipe:1`` reports position on stdout in a stable key=value form,
    # which is the only thing ffmpeg emits that is meant to be parsed. Without
    # it an encode of an hour-long sermon says nothing at all for twenty
    # minutes, and a stalled one looks exactly like a slow one.
    process = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1",
         "-y", "-i", str(master),
         "-vf", row["repair"], "-c:v", "libx264", "-crf", str(crf), "-preset", preset,
         # Named rather than inferred: ffmpeg cannot guess a container from a
         # name ending in ".part", and the part-file is what stops an
         # interrupted encode leaving something that looks finished.
         "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart",
         "-f", "mp4", str(partial)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    with ENCODING_LOCK:
        ENCODING.add(process)

    ticker = Ticker()
    for line in process.stdout:
        key, _, value = line.strip().partition("=")
        if key in ("out_time_us", "out_time_ms") and value.isdigit() and ticker.due():
            # out_time_ms is misnamed and holds microseconds, as out_time_us does.
            seconds = int(value) / 1_000_000
            share = f" ({seconds / duration:.0%})" if duration else ""
            logger.info("  encoded %d:%02d of %d:%02d%s",
                        seconds // 60, seconds % 60, duration // 60, duration % 60, share)
    try:
        finished = process.wait()
    finally:
        with ENCODING_LOCK:
            ENCODING.discard(process)
    if finished != 0:
        partial.unlink(missing_ok=True)
        raise subprocess.CalledProcessError(process.returncode, "ffmpeg", stderr=process.stderr.read())

    partial.replace(repaired)
    # ``path`` keeps naming the master. Pointing it at the repaired file instead
    # meant that sending a recording back to this stage left it addressing
    # output that the stage had not produced yet — and, once that output had
    # been deleted, addressing nothing at all.
    row["repaired_at"] = now()


def do_upload(row: dict[str, str], source: dict, options: argparse.Namespace) -> None:
    """
    Publish the recording, and remember what it became.

    :param row: The plan row to advance
    :param source: The export entry it came from, for the description
    :param options: Options carrying channel, privacy and category
    :return: None
    :raises ValueError: If the upload is refused
    """
    description = "\n".join(part for part in (
        source.get("verse", ""), source.get("artist", ""), source.get("date", "")) if part)
    # The repaired file where the recording needed repairing, the master where it
    # did not — decided by what is on disk rather than by what the plan says, so
    # that a repair rerun after the plan was written is still picked up.
    master = pathlib.Path(row["path"])
    repaired, _partial = repaired_paths(master)
    sending = repaired if repaired.exists() else master

    argv = upload_arguments(
        file=str(sending), title=row["title"], description=description,
        privacy=options.privacy, category=options.category,
        recording_date=source.get("date", ""), channel=options.channel,
        session_file=str(sending.with_suffix(sending.suffix + ".upload-session")),
        made_for_kids=options.made_for_kids, embeddable=options.embeddable,
        language=options.language or source.get("language", ""))
    options_parsed = create_argument_parser().parse_args(argv)
    video_id = upload(options_parsed)
    row["youtube_id"] = video_id or ""

    # The insert answered as soon as the bytes landed, which is before YouTube
    # looked at them. Stamping the stage now would record a success for a video
    # that may be refused a minute later, and nothing would ever look at it
    # again. So the id is kept either way and the stage is stamped only once
    # YouTube has had its chance to object.
    if video_id and not options.no_confirm:
        try:
            upload_status, reason = confirm_upload(get_authenticated_service(options_parsed), video_id)
        except Exception as exc:
            # Failing to *ask* is not the recording failing. The video is up and
            # its id is written down; treating this as an upload failure would
            # leave the row outstanding and the next run would upload a second
            # copy of a recording that is already published.
            row["note"] = f"uploaded as {video_id}, but confirming it failed: {type(exc).__name__}: {exc}"
            logger.warning("could not confirm %s: %s", video_id, exc)
        else:
            if upload_status in REFUSED:
                raise ValueError(f"YouTube refused {video_id}: {upload_status} {reason}".strip())
    row["uploaded_at"] = now()


def do_release(row: dict[str, str], keep: bool) -> None:
    """
    Give the disk back, now that the recording is published.

    Deliberately the last stage and its own stage: deleting gigabytes is only
    safe once ``youtube_id`` says where the recording went, and a run that stops
    before this one merely leaves a file behind.

    :param row: The plan row to advance
    :param keep: Leave the files where they are
    :return: None
    """
    if not keep:
        for path in pathlib.Path(row["path"]).parent.glob(f"{row['num']}-{row['vimeo_id']}*"):
            path.unlink(missing_ok=True)
    row["released_at"] = now()


def advance(row: dict[str, str], source: dict, args: argparse.Namespace, api: VimeoAPI,
            plan: list[dict[str, str]], plan_path: pathlib.Path,
            gates: dict[str, threading.Semaphore], writing: threading.Lock,
            stopping: threading.Event) -> bool:
    """
    Carry one recording through every stage it has left.

    Each stage waits for the gate belonging to whatever it contends for, and
    holds it only while it runs — so a recording queues for the encoder rather
    than for the recording in front of it, and the stages that want nothing in
    common proceed together. See :data:`CONTENDS_FOR`.

    A failure is reported and swallowed: one recording that cannot be fetched
    should not stop the several hundred behind it, and the note it leaves is
    what a person reads later to decide whether it is worth another attempt.

    :param row: The plan row to advance, stamped in place as each stage finishes
    :param source: The board entry behind it, supplying the upload's metadata
    :param args: The parsed command line
    :param api: An authenticated Vimeo session
    :param plan: Every row, since the file is rewritten whole after each stage
    :param plan_path: Where that file lives
    :param gates: One semaphore per resource named in :data:`CONTENDS_FOR`
    :param writing: Held while the plan is written; two threads rewriting one
        file at once would interleave their lines
    :param stopping: Set when the run has been interrupted. Checked between
        stages rather than during one, so that a stage either finishes and
        stamps the plan or is not begun
    :return: Whether the recording advanced as far as it was asked to
    """
    if stopping.is_set():
        # Never begun, and there may be six hundred more behind it. Saying so
        # for each would bury the reason the run stopped under its own report.
        return True
    logger.info(f"num={row['num']} {row['title'][:70]}")
    # Cleared once per attempt rather than after each stage: a note a stage
    # leaves behind describes this attempt and must outlive the stage that
    # wrote it — an upload that succeeded but could not be confirmed says so
    # here, and nowhere else.
    row["note"] = ""
    stage = stage_of(row)
    try:
        while (stage := stage_of(row)) != DONE and stage != args.stop_before:
            if stopping.is_set():
                logger.info(f"  num={row['num']} stopping, {stage} not begun")
                return True
            with held(gates.get(CONTENDS_FOR.get(stage, "")), row["num"], stage):
                # Asked again, on the other side of the gate. Waiting for the
                # encoder takes as long as an encode, so the interruption
                # usually arrives *during* the wait — and a recording that
                # checked before queueing has already passed the question by
                # then. Without asking twice, ^C starts the very encode it was
                # meant to prevent, moments after saying it had stopped.
                if stopping.is_set():
                    logger.info(f"  num={row['num']} stopping, {stage} not begun")
                    return True
                logger.info(f"  num={row['num']} {stage}...")
                if stage == "fetch":
                    do_fetch(api, row, args.work_dir)
                    logger.info(f"    num={row['num']} {row['gib']} GiB")
                elif stage == "measure":
                    do_measure(row)
                    logger.info(f"    num={row['num']} period {row['period']} rows, {row['prominence']} -> "
                                f"{'repair' if row['repair'] != 'none' else 'nothing to do'}")
                elif stage == "repair":
                    (do_repair(row, args.crf, args.preset) if not args.no_repair
                     else row.update(repaired_at=now()))
                elif stage == "upload":
                    do_upload(row, source, args)
                    logger.info(f"    num={row['num']} https://youtu.be/{row['youtube_id']}")
                elif stage == "release":
                    do_release(row, args.keep)
            with writing:
                write_plan(plan_path, plan)
    except Exception as exc:
        if stopping.is_set():
            # Whatever this stage was doing, we ended it: ffmpeg was terminated,
            # or a request was cut off with the process. Recording a note would
            # describe a fault in the recording and send somebody looking for
            # one that is not there. The plan already says the stage is unfinished.
            logger.info(f"  num={row['num']} {stage} stopped part way")
            with writing:
                write_plan(plan_path, plan)
            return True
        if is_daily_limit(exc):
            # Not this recording's fault, and not worth a traceback: every
            # recording left would reach the same refusal. Said before the plan
            # is written rather than after — setting ``stopping`` wakes every
            # thread queued at a gate, and each announces itself, so a write of
            # several hundred rows in between puts the consequences on screen
            # ahead of their cause.
            logger.info(f"  num={row['num']} YouTube's daily upload limit is reached — stopping the run")
            stopping.set()
            row["note"] = f"{stage}: YouTube accepted no more videos today"
            with writing:
                write_plan(plan_path, plan)
            return False
        row["note"] = f"{stage}: {type(exc).__name__}: {str(exc)[:120]}"
        with writing:
            write_plan(plan_path, plan)
        logger.info(f"  num={row['num']} FAILED at {stage}: {type(exc).__name__}: {exc}")
        traceback.print_exc(limit=2)
        return False
    return True


def main() -> int:
    """
    Advance the plan by up to ``--limit`` recordings.

    :return: 0 when every recording attempted advanced, 1 when any failed
    """
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--board", type=pathlib.Path, required=True, help="A scraped export to migrate")
    parser.add_argument("--plan", type=pathlib.Path, help="The migration plan (default: beside --board)")
    parser.add_argument("--preacher", action="append", metavar="GLOB",
                        help="Which preachers to migrate, as a shell glob: '홍길동*' covers one "
                             "credited under more than one title, and '!김영희 목사' means every "
                             "preacher but that one. Repeatable; omit for every preacher")
    parser.add_argument("--profile", default="example", help="Profile supplying the upload title template")
    parser.add_argument("--board-name", default="sunday_sermon")
    parser.add_argument("--work-dir", type=pathlib.Path, default=pathlib.Path.home() / "vimeo-work")
    parser.add_argument("--limit", type=int, default=1, help="How many recordings to finish (default: 1)")
    parser.add_argument("--channel", default="", help="Channel the uploads must land on")
    # Unlisted rather than public: the recordings embed on the church's own pages
    # and are linkable from them, without several hundred sermons arriving in
    # subscribers' feeds at once. Public is a later decision, and a cheap one —
    # videos.update costs 50 quota units against a daily 10,000.
    parser.add_argument("--privacy", default="unlisted", choices=("private", "unlisted", "public"),
                        help="How each recording is published (default: unlisted)")
    parser.add_argument("--category", type=int, default=29)
    parser.add_argument("--made-for-kids", choices=("yes", "no"), default="no",
                        help="Whether these are children's content, which YouTube requires every "
                             "upload to declare (default: no)")
    parser.add_argument("--embeddable", choices=("yes", "no"), default="yes",
                        help="Whether other sites may embed the videos (default: yes)")
    parser.add_argument("--language", default="",
                        help="Override the language each recording is published as; "
                             "by default the one the scrape recorded")
    parser.add_argument("--crf", type=int, default=18, help="Quality of a repaired re-encode")
    parser.add_argument("--preset", default=DEFAULT_PRESET,
                        choices=("ultrafast", "superfast", "veryfast", "faster", "fast",
                                 "medium", "slow", "slower", "veryslow"),
                        help=f"How hard x264 works on a repaired recording (default: {DEFAULT_PRESET})")
    parser.add_argument("--jobs", type=int, default=1, metavar="N",
                        help="How many recordings to have in flight at once. Their stages are gated "
                             "by what each contends for, so one recording encodes while the next "
                             "downloads and the last uploads (default: 1)")
    parser.add_argument("--repair-jobs", type=int, default=1, metavar="N",
                        help="How many recordings may be encoding at once (default: 1). x264 already "
                             "takes most of the machine, so raise this only alongside an encoder "
                             "that does not")
    parser.add_argument("--no-repair", action="store_true", help="Measure, but upload the master either way")
    parser.add_argument("--keep", action="store_true", help="Do not delete masters after uploading")
    parser.add_argument("--no-confirm", action="store_true",
                        help="Do not wait to see whether YouTube refuses each upload. Quicker, and "
                             "records a success for anything it later rejects")
    parser.add_argument("--cached-first", action="store_true",
                        help="Take recordings whose master is already in --work-dir before the rest. "
                             "A survey leaves gigabytes there that the plan does not know about")
    parser.add_argument("--stop-before", choices=("measure", "repair", "upload", "release"),
                        help="Advance each recording only as far as this stage")
    parser.add_argument("--redo", action="append", metavar="NUM", default=[],
                        help="Send this recording back to --redo-from and do it again. Repeatable")
    parser.add_argument("--redo-from", default="upload",
                        choices=("fetch", "measure", "repair", "upload", "release"),
                        help="Which stage --redo goes back to (default: upload)")
    args = parser.parse_args()
    configure()

    plan_path = args.plan or args.board.parent / "migration-plan.tsv"
    wanted = wanted_from(args.board, args.preacher, args.profile, args.board_name)
    by_num = {entry["num"]: entry for entry in wanted}

    plan, tally = merge(read_plan(plan_path), wanted)

    if args.redo:
        by_number = {row["num"]: row for row in plan}
        for num in args.redo:
            if num not in by_number:
                logger.info(f"  no recording numbered {num} is planned")
                continue
            reset(by_number[num], args.redo_from)
            logger.info(f"  num={num} sent back to {args.redo_from}")

    write_plan(plan_path, plan)
    logger.info(f"{len(plan)} recordings planned ({tally['added']} new, {tally['retitled']} retitled)")
    logger.info(f"  waiting: {tally_stages(plan)}")

    api = VimeoAPI(load_token())
    args.work_dir.mkdir(parents=True, exist_ok=True)

    queue = outstanding(plan)
    if args.cached_first:
        # Stable, so recordings already on disk come first and the rest keep
        # their order behind them.
        queue.sort(key=lambda row: not already_fetched(row, args.work_dir))
        ready = sum(already_fetched(row, args.work_dir) for row in queue)
        logger.info("%d of %d outstanding recordings are already fetched; those go first", ready, len(queue))

    gates = {resource: threading.Semaphore(1) for resource in set(CONTENDS_FOR.values())}
    gates["cpu"] = threading.Semaphore(args.repair_jobs)
    writing = threading.Lock()
    stopping = threading.Event()

    # Two ways to stop, because they cost differently. ^C reaches the whole
    # process group, so ffmpeg dies with the run and an encode in progress is
    # lost — up to twenty minutes of it, since an encode cannot be resumed.
    # SIGTERM reaches only this process: the stages already running finish and
    # stamp the plan, and nothing further is begun. Hence `kill <pid>` for a
    # tidy stop and ^C for an urgent one.
    signal.signal(signal.SIGTERM, lambda *_signal: stopping.set())
    if args.jobs > 1:
        logger.info("%d recordings in flight, %d encoding at once", args.jobs, args.repair_jobs)
    logger.info("pid %d — `kill %d` stops after the stages now running; ^C stops at once",
                os.getpid(), os.getpid())

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs)
    started = [pool.submit(advance, row, by_num.get(row["num"], {}), args, api,
                           plan, plan_path, gates, writing, stopping)
               for row in queue[:args.limit]]
    try:
        failures = sum(not finished.result() for finished in started)
    except KeyboardInterrupt:
        # Two things stop, and differently. Recordings not yet begun are
        # cancelled outright: every one is submitted to the pool up front and
        # shutting a pool down waits for its queue, so without cancelling, a run
        # of thirty answers ^C by continuing for most of a day. Recordings in
        # flight are told to stop between stages instead, since a stage that
        # finishes stamps the plan where one abandoned half way must be redone.
        stopping.set()
        encoding = stop_encoding()
        logger.info("interrupted — %sthe recordings in flight stop after the stage they are in",
                    f"ending {encoding} encode(s) in progress; " if encoding else "")
        pool.shutdown(wait=True, cancel_futures=True)
        logger.info(f"{plan_path}: {tally_stages(plan)}")
        return 1
    finally:
        pool.shutdown(wait=True)

    if any(DAILY_LIMIT in (row["note"] or "") or "no more videos today" in (row["note"] or "")
           for row in plan):
        ready = sum(1 for row in plan if stage_of(row) == "upload")
        logger.info("YouTube's daily upload limit was reached. %d recording(s) are fetched, measured "
                    "and repaired already; running again once it resets uploads them without "
                    "redoing any of that.", ready)
    logger.info(f"{plan_path}: {tally_stages(plan)}")
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        # ^C anywhere outside the part of the run that handles it — while the
        # plan is being read, or on a second press during the shutdown the first
        # one started. A traceback here says nothing except where the process
        # happened to be, and buries whatever the run had already reported.
        logger.info("stopped. Every stage that finished is in the plan; run again to continue")
        sys.exit(130)
