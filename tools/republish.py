#!/usr/bin/env python3
"""
Bring videos that are already on YouTube back into line with the plan.

The migration publishes a recording once and moves on, so a decision taken
afterwards — a title format settled on after a hundred videos are already up —
reaches the back catalogue only by editing what is published. That is what this
does, and the reason it is a separate tool rather than a stage: it changes
nothing on disk, needs no media, and is run against recordings the plan has
already finished with.

It reads what a video currently says before touching it, and edits *that* text
rather than rebuilding a title from the export. A rebuild would silently undo
every correction applied at the source since the upload, which is most of the
work this project exists to do.

Prints what it would send and sends nothing, until told ``--apply``:

    uv run python tools/republish.py --plan <plan>.tsv --profile <site> --title
"""

import argparse
import csv
import logging
import pathlib
import re
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from video_migrator.config import load_profile  # noqa: E402
from video_migrator.logs import configure  # noqa: E402
from video_migrator.metadata.summarize import read_summary, summary_path  # noqa: E402
from video_migrator.metadata.upload_description import format_upload_description  # noqa: E402
from video_migrator.metadata.upload_title import format_date  # noqa: E402
from video_migrator.models import Video  # noqa: E402
from video_migrator.plan import read_plan, write_plan  # noqa: E402
from video_migrator.sinks.youtube import (  # noqa: E402
    create_argument_parser,
    expected_channel,
    fetch_snippets,
    get_authenticated_service,
    update_snippet,
    verify_channel,
    writable_snippet,
)

logger = logging.getLogger(__name__)

#: The date format the back catalogue was published under, before the profile
#: was changed to the sortable one.
#:
#: Kept here rather than in the profile, which now holds only the format in use.
#: A one-off pass needs to know both, and the old one is of no interest to
#: anything that publishes.
PREVIOUS_DATE_FORMAT = "{month:02d}{day:02d}{short_year:02d}"

#: How long to wait between edits. Several hundred writes arriving as fast as
#: the API answers is a shape worth not presenting to a service that may decide
#: an account is misbehaving; the whole pass is quota-bound anyway.
PAUSE_SECONDS = 1.0

#: Stamped into ``summarized_at`` for a recording that finished before the stage
#: existed. A time would say the stage ran; this says it did not, and will not.
#:
#: Kept in step with ``tools/migrate.py``, which stamps the same value when it
#: meets such a row itself. Two spellings of "not summarized" would mean a row
#: marked by one tool read as outstanding by the other.
NOT_SUMMARIZED = "n/a"


def rewrite_date(title: str, published: str, previous: str, current: str) -> tuple[str, str]:
    """
    Rewrite the date a published title carries, without rebuilding the title.

    The date is found by computing what the old format *would* have produced for
    this recording and looking for exactly that, bounded so it cannot match
    inside a longer run of digits. Anything other than one match is refused
    rather than guessed at: a title holding the token twice gives no way to tell
    the date from a number in the sermon's own name.

    :param title: The title as the video currently carries it
    :param published: The date the site gave the recording, ``YYYY-MM-DD``
    :param previous: The date format the title was published under
    :param current: The date format it should carry
    :return: The rewritten title and an empty note, or the title unchanged and
        the reason it was left alone

    >>> old, new = "{month:02d}{day:02d}{short_year:02d}", "{short_year:02d}{month:02d}{day:02d}"
    >>> rewrite_date("[예시교회] 080226 주일예배", "2026-08-02", old, new)
    ('[예시교회] 260802 주일예배', '')

    A pass that has already run leaves nothing to do, and says so rather than
    reporting a title it cannot find a date in:

    >>> rewrite_date("[예시교회] 260802 주일예배", "2026-08-02", old, new)
    ('[예시교회] 260802 주일예배', 'already 260802')

    A date the two formats write identically needs no edit:

    >>> rewrite_date("[예시교회] 111111 주일예배", "2011-11-11", old, new)
    ('[예시교회] 111111 주일예배', 'both formats write 111111')

    Neither does a title the date cannot be located in:

    >>> rewrite_date("[예시교회] 주일예배", "2026-08-02", old, new)
    ('[예시교회] 주일예배', '080226 appears 0 times')
    """
    was = format_date(published, previous)
    becomes = format_date(published, current)
    if was == becomes:
        return title, f"both formats write {was}"

    found = list(re.finditer(rf"(?<!\d){re.escape(was)}(?!\d)", title))
    if len(found) == 1:
        at = found[0]
        return title[:at.start()] + becomes + title[at.end():], ""
    if re.search(rf"(?<!\d){re.escape(becomes)}(?!\d)", title):
        return title, f"already {becomes}"
    return title, f"{was} appears {len(found)} times"


def published_videos(plan: list[dict[str, str]]) -> list[dict[str, str]]:
    """
    The rows this tool can act on: the ones that became a video.

    :param plan: Every row of the plan
    :return: Those carrying a ``youtube_id``, in plan order
    """
    return [row for row in plan if (row.get("youtube_id") or "").strip()]


def mark_summarized(plan: list[dict[str, str]]) -> int:
    """
    Stamp the rows that finished before the ``summarize`` stage existed.

    Adding a stage column sends every existing row back to it: a released
    recording reads as ``summarize``-outstanding, and a run would hand it to
    ``advance``, which would try to re-fetch a master that has been deleted. The
    rows are correct that they have no summary — they are picked up by the
    backfill instead — so they are stamped ``n/a`` rather than with a time,
    which says the stage did not run rather than that it did.

    Only released rows are marked. One still mid-flight has its media on disk
    and should genuinely be summarized.

    A note the summarize stage left is cleared with the stamp. A run that met
    these rows before they were marked will have failed on each of them —
    there is no master to transcribe — and a note describing that failure,
    left on a row that is now deliberately never going to be summarized, sends
    somebody looking for a fault that has been dealt with. Notes from any other
    stage are kept: an upload that could not be confirmed is real history, and
    this is not the pass that decides it has been read.

    :param plan: Every row of the plan, stamped in place
    :return: How many rows were marked

    >>> plan = [{"released_at": "2026-08-01 10:00", "summarized_at": ""},
    ...         {"released_at": "", "summarized_at": ""}]
    >>> mark_summarized(plan)
    1
    >>> [row["summarized_at"] for row in plan]
    ['n/a', '']

    Running it twice marks nothing the second time:

    >>> mark_summarized(plan)
    0

    The failure such a row will have left behind goes with it, and a note from
    any other stage stays:

    >>> plan = [{"released_at": "x", "summarized_at": "", "note": "summarize: FileNotFoundError"},
    ...         {"released_at": "y", "summarized_at": "", "note": "upload: could not confirm"}]
    >>> mark_summarized(plan)
    2
    >>> [row["note"] for row in plan]
    ['', 'upload: could not confirm']
    """
    marked = 0
    for row in plan:
        if (row.get("released_at") or "").strip() and not (row.get("summarized_at") or "").strip():
            row["summarized_at"] = NOT_SUMMARIZED
            if (row.get("note") or "").startswith("summarize:"):
                row["note"] = ""
            marked += 1
    return marked


def videos_from(board: pathlib.Path) -> dict[str, Video]:
    """
    Read the export the migration ran from, keyed by record id.

    A description carries the verse and the preacher, and the plan holds
    neither — it records what a recording *became*, not what it said. So the
    export has to be read, exactly as ``tools/migrate.py`` reads it when
    building the description for a fresh upload.

    :param board: A scraped export
    :return: Record id -> the normalized record, Vimeo rows only
    """
    videos = {}
    with board.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle, delimiter="\t"):
            if row["Type"] != "vimeo":
                continue
            videos[row["num"]] = Video(
                type=row["Type"], id=row["ID"], url=row["URL"], embed_url=row["Embed URL"],
                title=row["Title"], bible_verse=row["Bible Verse"], publish_date=row["Publish Date"],
                artist=row["Preacher"], genre=row["Genre"], language=row["Language"])
    return videos


def redescribe(plan: list[dict[str, str]], snippets: dict[str, dict], videos: dict[str, Video],
               site_dir: pathlib.Path, profile, board) -> list[tuple[dict[str, str], dict, str, str]]:
    """
    Work out what each published video's description should become.

    Three things are refused rather than guessed at, on the same principle the
    retitle pass runs on — these are live videos nobody can re-upload, and a
    wrong edit replaces text that is then gone:

    - **No summary, or one still carrying the stub marker.** The description
      would come out as the metadata alone, which is what the video already
      says, and each pointless edit costs 50 of a daily 10,000 quota units.
    - **A description this migration did not write.** ``videos.update``
      replaces the field whole, so pushing over a description somebody has
      edited by hand destroys their edit. The check is exact: the live text
      must equal what this profile renders for this recording with no summary.
      That also makes the pass idempotent, and it is what stops a *changed*
      summary being prepended on top of the one already published.
    - **A recording the export does not cover.** Its verse and preacher are
      unknown, and a description built without them would delete both from a
      video that is carrying them.

    :param plan: Every row of the plan
    :param snippets: Video id -> the snippet read back from YouTube
    :param videos: Record id -> the normalized record, from :func:`videos_from`
    :param site_dir: Where the summaries are kept, ``sites/<site>``
    :param profile: Profile supplying the description template
    :param board: The board naming the service
    :return: One entry per published row — the row, its snippet, the description
        it should carry, and the reason it is being left alone where there is one
    """
    decided = []
    for row in published_videos(plan):
        snippet = snippets.get(row["youtube_id"])
        if snippet is None:
            decided.append((row, {}, "", "YouTube does not know this video"))
            continue
        video = videos.get(row["num"])
        if video is None:
            decided.append((row, snippet, "", "the export does not cover this recording"))
            continue
        summary = read_summary(summary_path(site_dir, row["num"]))
        if not summary:
            decided.append((row, snippet, "", "no summary written yet"))
            continue

        live = snippet.get("description", "")
        wanted = format_upload_description(video, summary, profile, board)
        if live == wanted:
            decided.append((row, snippet, wanted, "already says this"))
            continue
        if live != format_upload_description(video, "", profile, board):
            decided.append((row, snippet, wanted,
                            "the description is not the one this migration wrote"))
            continue
        decided.append((row, snippet, wanted, ""))
    return decided


def retitle(plan: list[dict[str, str]], snippets: dict[str, dict], current_format: str,
            previous_format: str) -> list[tuple[dict[str, str], dict, str, str]]:
    """
    Work out what each published video's title should become.

    :param plan: Every row of the plan
    :param snippets: Video id -> the snippet read back from YouTube
    :param current_format: The date format the profile now publishes under
    :param previous_format: The one the back catalogue was published under
    :return: One entry per published row — the row, its snippet, the title it
        should carry, and the reason it is being left alone where there is one.
        A row whose video YouTube does not know is reported and carries no
        snippet
    """
    decided = []
    for row in published_videos(plan):
        snippet = snippets.get(row["youtube_id"])
        if snippet is None:
            decided.append((row, {}, "", "YouTube does not know this video"))
            continue
        live = snippet.get("title", "")
        wanted, note = rewrite_date(live, row["published"], previous_format, current_format)
        decided.append((row, snippet, wanted, note))
    return decided


def to_change(decided: list[tuple[dict[str, str], dict, str, str]],
              limit: int | None) -> list[tuple[dict[str, str], dict, str, str]]:
    """
    Narrow a decision to the videos that will actually be edited.

    The limit is applied after the skips are dropped, so it counts edits rather
    than rows looked at. Counting rows would make ``--limit 1`` — which exists
    to put a single video in front of a person before the rest follow — spend
    itself on a row that was never going to change, and send nothing.

    :param decided: Every published row, as :func:`retitle` decided it
    :param limit: How many videos may be edited, None for all of them
    :return: The entries to send, in plan order

    >>> rows = [({"num": "1"}, {}, "a", ""), ({"num": "2"}, {}, "", "skipped"), ({"num": "3"}, {}, "c", "")]
    >>> [row["num"] for row, _s, _w, _n in to_change(rows, None)]
    ['1', '3']
    >>> [row["num"] for row, _s, _w, _n in to_change(rows, 1)]
    ['1']
    """
    changing = [entry for entry in decided if not entry[3]]
    return changing if limit is None else changing[:limit]


def report(row: dict[str, str], live: str, wanted: str, note: str) -> None:
    """
    Say what is about to happen to one video, or why nothing is.

    A description runs to several lines, so both sides are indented under their
    own heading rather than put on one line each: the point of a dry run is that
    somebody reads what is about to be sent.

    :param row: The plan row
    :param live: The text the video carries now
    :param wanted: The text it should carry
    :param note: Why it is being left alone, empty when it is not
    :return: None
    """
    if note:
        logger.info(f"num={row['num']} {row['youtube_id']} skipped: {note}")
        return
    logger.info(f"num={row['num']} {row['youtube_id']}")
    for heading, text in (("was", live), ("now", wanted)):
        lines = text.splitlines() or [""]
        logger.info(f"    {heading} {lines[0]}")
        for line in lines[1:]:
            logger.info(f"        {line}")


def main() -> int:
    """
    Edit the published videos the plan knows about.

    :return: 0 when every video considered was edited or deliberately skipped,
        1 when any failed
    """
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--plan", type=pathlib.Path, required=True, help="The migration plan to work from")
    parser.add_argument("--profile", default="example", help="Profile supplying the date format now in use")
    parser.add_argument("--board", type=pathlib.Path,
                        help="The scraped export, required by --description: the verse and the "
                             "preacher a description carries are there and not in the plan")
    parser.add_argument("--board-name", default="sunday_sermon",
                        help="Which board's service naming a rebuilt description uses")
    parser.add_argument("--title", action="store_true", help="Bring the title's date into the current format")
    parser.add_argument("--description", action="store_true",
                        help="Publish the sermon's summary into the video's description. A recording "
                             "whose summary is missing, or is still a stub, is skipped")
    parser.add_argument("--mark-summarized", action="store_true",
                        help=f"Stamp every released row's summarized_at as {NOT_SUMMARIZED!r} and stop. "
                             "Run this once before the summarize stage goes live, or rows that are "
                             "already finished are sent back to fetch")
    parser.add_argument("--site-dir", type=pathlib.Path,
                        help="Where the summaries are kept (default: the directory above --plan)")
    parser.add_argument("--previous-date-format", default=PREVIOUS_DATE_FORMAT,
                        help=f"The format the back catalogue was published under (default: {PREVIOUS_DATE_FORMAT})")
    parser.add_argument("--limit", type=int, help="Edit at most this many videos")
    parser.add_argument("--channel", default="", help="Channel the videos must belong to")
    parser.add_argument("--pause", type=float, default=PAUSE_SECONDS,
                        help=f"Seconds between edits (default: {PAUSE_SECONDS})")
    parser.add_argument("--apply", action="store_true", help="Send the edits; without it nothing is changed")
    args = parser.parse_args()
    configure()

    if args.title and args.description:
        # Each rebuilds one field and sends the whole writable snippet back. Run
        # together they would be one edit carrying two changes, which is exactly
        # the shape that makes a bad pass impossible to unpick afterwards.
        parser.error("pass --title or --description, not both")
    if not (args.title or args.description or args.mark_summarized):
        parser.error("nothing to do: pass --title, --description or --mark-summarized")
    if args.description and not args.board:
        parser.error("--description needs --board: the verse and preacher are in the export, not the plan")

    plan = read_plan(args.plan)

    if args.mark_summarized:
        # Touches no video and needs no credentials, so it answers before any
        # of the YouTube setup below.
        marked = mark_summarized(plan)
        logger.info(f"{marked} released recording(s) marked {NOT_SUMMARIZED!r}")
        if not args.apply:
            logger.info("nothing was written — pass --apply to write it")
            return 0
        write_plan(args.plan, plan)
        logger.info(f"{args.plan} written")
        return 0

    profile = load_profile(args.profile)
    published = published_videos(plan)
    if not published:
        logger.info(f"{args.plan} holds no video to edit")
        return 0

    youtube_options = create_argument_parser().parse_args(["--channel", args.channel] if args.channel else [])
    youtube = get_authenticated_service(youtube_options)
    verify_channel(youtube, expected_channel(youtube_options))

    logger.info(f"{len(published)} published recording(s); reading back what they say")
    snippets = fetch_snippets(youtube, [row["youtube_id"] for row in published])

    field = "title" if args.title else "description"
    if args.title:
        decided = retitle(plan, snippets, profile.upload_date_format, args.previous_date_format)
    else:
        site_dir = args.site_dir or args.plan.parent.parent
        decided = redescribe(plan, snippets, videos_from(args.board), site_dir,
                             profile, profile.board(args.board_name))

    changing = to_change(decided, args.limit)
    sending = {row["youtube_id"] for row, _snippet, _wanted, _note in changing}

    for row, snippet, wanted, note in decided:
        if note or row["youtube_id"] in sending:
            report(row, snippet.get(field, ""), wanted, note)

    skipped = sum(1 for entry in decided if entry[3])
    logger.info(f"{len(changing)} to edit, {skipped} left alone")
    if not args.apply:
        logger.info("nothing was sent — pass --apply to send it")
        return 0

    failures = 0
    for index, (row, snippet, wanted, _note) in enumerate(changing):
        try:
            update_snippet(youtube, row["youtube_id"], writable_snippet(snippet) | {field: wanted})
        except Exception as exc:
            failures += 1
            logger.info(f"num={row['num']} FAILED: {type(exc).__name__}: {exc}")
            continue
        if args.title:
            # Written after each edit rather than at the end: the plan is the only
            # record of which recording became which video, and a run stopped part
            # way through several hundred must not lose the ones it did. Only the
            # title is written back, because it is the only edited field the plan
            # holds a column for — a description lives in its summary file.
            row["title"] = wanted
            write_plan(args.plan, plan)
        logger.info(f"num={row['num']} {row['youtube_id']} {field} updated")
        if index < len(changing) - 1:
            time.sleep(args.pause)

    logger.info(f"{len(changing) - failures} edited, {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        logger.info("stopped. Every edit that finished is in the plan; run again to continue")
        sys.exit(130)
