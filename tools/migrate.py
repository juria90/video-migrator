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
import csv
import logging
import pathlib
import subprocess
import sys
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
#: Measured on this archive's own material, encoding a 720p sermon: ``medium``
#: runs at 1.08x realtime, ``fast`` at 1.30x, ``veryfast`` at 1.93x. The
#: temptation is ``veryfast``, which is quickest and produces the *smallest*
#: file — but that is not compression, it is loss. At a fixed CRF a weaker
#: preset finds fewer efficient ways to carry detail and so discards more of it.
#:
#: These are archival copies, and the notch has already given up some vertical
#: detail; stacking a weaker encoder on top compounds that. ``fast`` is 20%
#: quicker than ``medium`` for a difference not worth measuring, which is the
#: trade worth taking. Override with --preset where throughput matters more.
DEFAULT_PRESET = "fast"


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

    ticker = Ticker()
    for line in process.stdout:
        key, _, value = line.strip().partition("=")
        if key in ("out_time_us", "out_time_ms") and value.isdigit() and ticker.due():
            # out_time_ms is misnamed and holds microseconds, as out_time_us does.
            seconds = int(value) / 1_000_000
            share = f" ({seconds / duration:.0%})" if duration else ""
            logger.info("  encoded %d:%02d of %d:%02d%s",
                        seconds // 60, seconds % 60, duration // 60, duration % 60, share)
    if process.wait() != 0:
        partial.unlink(missing_ok=True)
        raise subprocess.CalledProcessError(process.returncode, "ffmpeg", stderr=process.stderr.read())

    partial.replace(repaired)
    row.update(path=str(repaired), repaired_at=now())


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
    argv = upload_arguments(
        file=row["path"], title=row["title"], description=description,
        privacy=options.privacy, category=options.category,
        recording_date=source.get("date", ""), channel=options.channel,
        session_file=str(pathlib.Path(row["path"]).with_suffix(".upload-session")),
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
        upload_status, reason = confirm_upload(get_authenticated_service(options_parsed), video_id)
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
    failures = 0

    queue = outstanding(plan)
    if args.cached_first:
        # Stable, so recordings already on disk come first and the rest keep
        # their order behind them.
        queue.sort(key=lambda row: not already_fetched(row, args.work_dir))
        ready = sum(already_fetched(row, args.work_dir) for row in queue)
        logger.info("%d of %d outstanding recordings are already fetched; those go first", ready, len(queue))

    for row in queue[:args.limit]:
        source = by_num.get(row["num"], {})
        logger.info(f"num={row['num']} {row['title'][:70]}")
        try:
            while (stage := stage_of(row)) != DONE and stage != args.stop_before:
                logger.info(f"  {stage}...")
                if stage == "fetch":
                    do_fetch(api, row, args.work_dir)
                    logger.info(f"    {row['gib']} GiB")
                elif stage == "measure":
                    do_measure(row)
                    logger.info(f"    period {row['period']} rows, {row['prominence']} -> {'repair' if row['repair'] != 'none' else 'nothing to do'}")
                elif stage == "repair":
                    (do_repair(row, args.crf, args.preset) if not args.no_repair
                     else row.update(repaired_at=now()))
                elif stage == "upload":
                    do_upload(row, source, args)
                    logger.info(f"    https://youtu.be/{row['youtube_id']}")
                elif stage == "release":
                    do_release(row, args.keep)
                row["note"] = ""
                write_plan(plan_path, plan)
        except Exception as exc:
            row["note"] = f"{stage}: {type(exc).__name__}: {str(exc)[:120]}"
            write_plan(plan_path, plan)
            failures += 1
            logger.info(f"  FAILED at {stage}: {type(exc).__name__}: {exc}")
            traceback.print_exc(limit=2)

    logger.info(f"{plan_path}: {tally_stages(plan)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
