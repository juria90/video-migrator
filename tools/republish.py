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
import logging
import pathlib
import re
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from video_migrator.config import load_profile  # noqa: E402
from video_migrator.logs import configure  # noqa: E402
from video_migrator.metadata.upload_title import format_date  # noqa: E402
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

    :param row: The plan row
    :param live: The title the video carries now
    :param wanted: The title it should carry
    :param note: Why it is being left alone, empty when it is not
    :return: None
    """
    if note:
        logger.info(f"num={row['num']} {row['youtube_id']} skipped: {note}")
        return
    logger.info(f"num={row['num']} {row['youtube_id']}")
    logger.info(f"    was {live}")
    logger.info(f"    now {wanted}")


def main() -> int:
    """
    Edit the published videos the plan knows about.

    :return: 0 when every video considered was edited or deliberately skipped,
        1 when any failed
    """
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--plan", type=pathlib.Path, required=True, help="The migration plan to work from")
    parser.add_argument("--profile", default="example", help="Profile supplying the date format now in use")
    parser.add_argument("--title", action="store_true", help="Bring the title's date into the current format")
    parser.add_argument("--previous-date-format", default=PREVIOUS_DATE_FORMAT,
                        help=f"The format the back catalogue was published under (default: {PREVIOUS_DATE_FORMAT})")
    parser.add_argument("--limit", type=int, help="Edit at most this many videos")
    parser.add_argument("--channel", default="", help="Channel the videos must belong to")
    parser.add_argument("--pause", type=float, default=PAUSE_SECONDS,
                        help=f"Seconds between edits (default: {PAUSE_SECONDS})")
    parser.add_argument("--apply", action="store_true", help="Send the edits; without it nothing is changed")
    args = parser.parse_args()
    configure()

    if not args.title:
        parser.error("nothing to do: pass --title")

    profile = load_profile(args.profile)
    plan = read_plan(args.plan)
    published = published_videos(plan)
    if not published:
        logger.info(f"{args.plan} holds no video to edit")
        return 0

    youtube_options = create_argument_parser().parse_args(["--channel", args.channel] if args.channel else [])
    youtube = get_authenticated_service(youtube_options)
    verify_channel(youtube, expected_channel(youtube_options))

    logger.info(f"{len(published)} published recording(s); reading back what they say")
    snippets = fetch_snippets(youtube, [row["youtube_id"] for row in published])
    decided = retitle(plan, snippets, profile.upload_date_format, args.previous_date_format)

    changing = to_change(decided, args.limit)
    sending = {row["youtube_id"] for row, _snippet, _wanted, _note in changing}

    for row, snippet, wanted, note in decided:
        if note or row["youtube_id"] in sending:
            report(row, snippet.get("title", ""), wanted, note)

    skipped = sum(1 for entry in decided if entry[3])
    logger.info(f"{len(changing)} to edit, {skipped} left alone")
    if not args.apply:
        logger.info("nothing was sent — pass --apply to send it")
        return 0

    failures = 0
    for index, (row, snippet, wanted, _note) in enumerate(changing):
        try:
            update_snippet(youtube, row["youtube_id"], writable_snippet(snippet) | {"title": wanted})
        except Exception as exc:
            failures += 1
            logger.info(f"num={row['num']} FAILED: {type(exc).__name__}: {exc}")
            continue
        # Written after each edit rather than at the end: the plan is the only
        # record of which recording became which video, and a run stopped part
        # way through several hundred must not lose the ones it did.
        row["title"] = wanted
        write_plan(args.plan, plan)
        logger.info(f"num={row['num']} {row['youtube_id']} retitled")
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
