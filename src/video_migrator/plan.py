#!/usr/bin/env python3
"""
The record of what a migration has done to each recording, and what is left.

A back-catalogue migration is not one long operation. It is several hundred
short ones, each of which can fail on its own, run at a different time, and be
retried without disturbing its neighbours — over weeks, on a machine that will
be rebooted, against services that rate-limit and time out.

So progress is written down rather than held in memory. Each recording has one
row, and each stage it passes through stamps its own column. A run reads the
file, finds the first recording whose row is unfinished, and continues from
whichever stage is blank. Interrupt it anywhere and the next run resumes there.

The file is merged, never rebuilt, for the same reason the correction ledger is:
it records work that cannot be redone from the site. Which YouTube video a
recording became is knowable only from here.
"""

import csv
import datetime
import pathlib

#: The columns a plan holds, in order.
#:
#: The stage columns are timestamps rather than flags. A flag says a stage
#: finished; a timestamp also says when, which is the only way to tell a
#: migration that stalled from one that is merely slow.
COLUMNS = (
    "num",            # the board's record id, the key
    "vimeo_id",       # what to fetch
    "title",          # what it will be published as, for reading the file by eye
    "published",      # the date the site gave it, which is the order the work is done in
    "fetched_at",     # stage 1: the master is on disk
    "path",           # where, while it is
    "gib",            # how large, for planning the disk
    "measured_at",    # stage 2: its artifacts are known
    "period",         # comb period in rows, blank when nothing was found
    "prominence",     # how far that stood above the picture
    "repair",         # the filter chosen, or "none"
    "repaired_at",    # stage 3: the file to upload exists (equal to fetched when repair is none)
    "youtube_id",     # stage 4: what it became
    "uploaded_at",
    "released_at",    # stage 5: the master has been deleted again
    "note",           # why a stage did not happen
)

#: The stages a recording passes through, and the column that records each one
#: as done. Order is the order they happen in.
STAGES = (
    ("fetch", "fetched_at"),
    ("measure", "measured_at"),
    ("repair", "repaired_at"),
    ("upload", "uploaded_at"),
    ("release", "released_at"),
)

#: What a row's stage is once every stage has stamped it.
DONE = "done"

#: Where a row carrying no date sorts. The site does occasionally publish a
#: recording without one, and an empty string sorts before every real date —
#: which would put the rows we know least about at the head of a migration that
#: runs for weeks.
UNDATED = "9999-99-99"


def now() -> str:
    """
    The timestamp a stage stamps itself with.

    :return: The current local time, to the minute
    """
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


def read_plan(path: pathlib.Path) -> list[dict[str, str]]:
    """
    Read a plan, or start an empty one.

    :param path: The TSV to read
    :return: Its rows, in file order
    """
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle, delimiter="\t")]


def write_plan(path: pathlib.Path, rows: list[dict[str, str]]) -> None:
    """
    Write a plan to disk.

    Lines end with ``\\n`` on every platform, for the reason
    :func:`~video_migrator.ledger.write_ledger` gives: this file is read by
    diffing it against yesterday's, and a translated line ending changes every
    row while changing no value.

    Written to a neighbouring file and moved into place, rather than over the
    top of the last one. The move is atomic, so a run killed while writing —
    which is what ^C during a batch invites — leaves the previous plan intact
    instead of half of this one. That matters more here than the cost: which
    recording became which video is knowable only from this file, and no amount
    of reading YouTube back rebuilds it.

    :param path: The TSV to write
    :param rows: The rows, in the order they should appear
    :return: None
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["\t".join(COLUMNS)]
    lines += ["\t".join((row.get(column) or "").replace("\t", " ") for column in COLUMNS) for row in rows]
    partial = path.with_name(path.name + ".writing")
    partial.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    partial.replace(path)


def stage_of(row: dict[str, str]) -> str:
    """
    Which stage a recording is waiting on.

    :param row: A plan row
    :return: The stage's name, or :data:`DONE`

    >>> stage_of({})
    'fetch'
    >>> stage_of({"fetched_at": "2026-08-22 09:00"})
    'measure'
    >>> stage_of({"fetched_at": "x", "measured_at": "x", "repaired_at": "x"})
    'upload'
    >>> stage_of(dict.fromkeys([column for _, column in STAGES], "x"))
    'done'
    """
    for stage, column in STAGES:
        if not row.get(column):
            return stage
    return DONE


def order(row: dict[str, str]) -> tuple[str, int]:
    """
    Where a row sits in the plan, and so when its turn comes.

    Oldest first. ``num`` is the board's own record id and reads like a
    chronological key, but is not one: this archive numbers its back-catalogue
    the other way about, so working through the file by num walks from 2019
    backwards to 2010 and then jumps forward again. Only the date the site
    published under orders the way a reader expects.

    :param row: A plan row
    :return: A key placing older recordings first and undated ones last

    >>> order({"published": "2011-04-03", "num": "440"})
    ('2011-04-03', 440)
    >>> order({"published": "", "num": "7"}) > order({"published": "2026-01-01", "num": "1"})
    True
    """
    return (row.get("published") or UNDATED, int(row["num"]) if row["num"].isdigit() else 0)


def merge(plan: list[dict[str, str]], wanted: list[dict[str, str]]) -> tuple[list[dict[str, str]], dict[str, int]]:
    """
    Bring a plan up to date with the recordings that should be in it.

    Adds a row for anything not yet planned and refreshes the title of anything
    already there, since a correction may have landed at the source since the
    plan was written. Nothing is ever removed: a recording dropped from the
    selection has still been uploaded, and that is the fact worth keeping.

    :param plan: The plan as it stands
    :param wanted: Rows from a scraped export, each with ``num``, ``ID``, a
        ``date`` and a title already formatted for upload
    :return: The merged plan, in :func:`order`, and a tally of what happened

    >>> plan, tally = merge([], [{"num": "12", "ID": "34", "title": "설교 제목"}])
    >>> plan[0]["num"], plan[0]["vimeo_id"], stage_of(plan[0])
    ('12', '34', 'fetch')
    >>> tally["added"]
    1
    >>> plan, tally = merge(plan, [{"num": "12", "ID": "34", "title": "고친 제목"}])
    >>> plan[0]["title"], tally["retitled"], tally["added"]
    ('고친 제목', 1, 0)

    The board's numbering is not chronological, so the plan is not in it:

    >>> plan, _ = merge([], [{"num": "9", "ID": "1", "date": "2026-01-04"},
    ...                      {"num": "3", "ID": "2", "date": "2011-06-19"}])
    >>> [row["num"] for row in plan]
    ['3', '9']
    """
    tally = {"added": 0, "retitled": 0, "unchanged": 0}
    by_num = {row["num"]: row for row in plan}

    for source in wanted:
        num = str(source["num"])
        row = by_num.get(num)
        if row is None:
            row = dict.fromkeys(COLUMNS, "")
            row.update(num=num, vimeo_id=str(source["ID"]), title=source.get("title", ""))
            plan.append(row)
            by_num[num] = row
            tally["added"] += 1
        elif source.get("title") and row.get("title") != source["title"]:
            row["title"] = source["title"]
            tally["retitled"] += 1
        else:
            tally["unchanged"] += 1
        # Refreshed like the title, and for the same reason: a date corrected at
        # the source after the plan was written should move the row, not be lost.
        row["published"] = source.get("date", "") or row.get("published", "")

    plan.sort(key=order)
    return plan, tally


#: Which columns each stage owns, and must therefore give up when it is redone.
#: A stage's own stamp is not enough: undoing an upload without forgetting the
#: video it produced would leave the plan naming something that no longer exists.
STAGE_COLUMNS = {
    "fetch": ("fetched_at", "path", "gib"),
    "measure": ("measured_at", "period", "prominence", "repair"),
    "repair": ("repaired_at",),
    "upload": ("uploaded_at", "youtube_id"),
    "release": ("released_at",),
}


def reset(row: dict[str, str], from_stage: str) -> dict[str, str]:
    """
    Send a recording back to a stage, so a run will do it again.

    Everything from that stage onward is forgotten, because the stages are a
    sequence: a recording that must be uploaded again has not been released
    either, whatever the plan says.

    :param row: The plan row to rewind
    :param from_stage: The stage to go back to
    :return: The same row, rewound
    :raises KeyError: If there is no such stage

    >>> row = {column: "x" for _, column in STAGES}
    >>> row["youtube_id"] = "aBcDeFgHiJk"
    >>> stage_of(reset(row, "upload"))
    'upload'
    >>> row["youtube_id"]
    ''
    >>> stage_of(reset(dict.fromkeys([c for _, c in STAGES], "x"), "fetch"))
    'fetch'
    """
    names = [stage for stage, _ in STAGES]
    for stage in names[names.index(from_stage):]:
        for column in STAGE_COLUMNS[stage]:
            row[column] = ""
    row["note"] = ""
    return row


def outstanding(plan: list[dict[str, str]]) -> list[dict[str, str]]:
    """
    The rows a run still has work to do on.

    A row that failed carries a note, and is still outstanding: the failure may
    have been a timeout, and the next run should try it again. What stops a row
    being retried forever is a person reading the note, not the file itself.

    :param plan: The plan
    :return: The unfinished rows, in plan order
    """
    return [row for row in plan if stage_of(row) != DONE]


def tally_stages(plan: list[dict[str, str]]) -> dict[str, int]:
    """
    How many recordings are waiting on each stage.

    :param plan: The plan
    :return: Stage name -> count, including :data:`DONE`

    >>> tally_stages([{}, {"fetched_at": "x"}])
    {'fetch': 1, 'measure': 1}
    """
    counts: dict[str, int] = {}
    for row in plan:
        counts[stage_of(row)] = counts.get(stage_of(row), 0) + 1
    return counts
