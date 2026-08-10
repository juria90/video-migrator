#!/usr/bin/env python3
"""
Decide what may be written back to a source site, and what may not.

Applying a correction is separated from deciding to apply it, because the two
fail differently. A decision is wrong quietly — the wrong value, or a value
overwriting someone's work — and only a reader would notice. Everything in this
module is pure, so those decisions can be tested without a site to write to.

The governing case is that a ledger is a snapshot. Between the scan that wrote a
row and the run that applies it, a record may have been corrected already, or
edited by hand to something else entirely. Only a record still holding what the
scan saw is safe to write.
"""

import pathlib

from ..ledger import read_ledger, write_ledger

#: A ``new`` value in parentheses is a question for a person rather than a value
#: to write — "(look it up in the bulletin)" — so it is never applied.
ASK_MARKER = "("


def decide(on_site: str, recorded: str, suggested: str) -> str:
    """
    Decide what to do with one field, given what the record holds right now.

    :param on_site: The value the record holds now
    :param recorded: The value the ledger recorded as current when it was scanned
    :param suggested: The value the ledger proposes
    :return: ``"skip"``, ``"write"`` or ``"diverged"``

    >>> decide("old", "old", "new")        # untouched since the scan
    'write'
    >>> decide("new", "old", "new")        # already applied, however it got there
    'skip'
    >>> decide("mine", "old", "new")       # someone edited it in between
    'diverged'
    """
    if on_site == suggested:
        return "skip"
    if on_site == recorded:
        return "write"
    return "diverged"


def applicable(row: dict[str, str], field_input: dict[str, str], kinds: set[str] | None = None) -> str | None:
    """
    Decide whether a ledger row is one that can be applied automatically.

    A row carrying ``updated_at`` is history rather than work. A suggestion in
    parentheses is a question for a person. Anything else is applicable as long
    as there is a form field to write it to — so a value that was missing and has
    since been recovered by hand applies like any other.

    :param row: A row of the ledger
    :param field_input: Ledger field name -> the form input holding it
    :param kinds: Fields to keep, or None for all
    :return: The form input to change, or None when the row is not applicable

    >>> inputs = {"title": "subject"}
    >>> applicable({"updated_at": "", "field": "title", "new": "설교 제목"}, inputs)
    'subject'
    >>> applicable({"updated_at": "", "field": "title", "new": "(look it up)"}, inputs)
    >>> applicable({"updated_at": "2024-01-01", "field": "title", "new": "설교 제목"}, inputs)
    """
    if row.get("updated_at"):
        return None
    if kinds and row["field"] not in kinds:
        return None
    if not row["new"] or row["new"].startswith(ASK_MARKER):
        return None
    return field_input.get(row["field"])


def stamp_landed(path: pathlib.Path, landed: set[tuple[str, str]], when: str) -> int:
    """
    Record in a ledger that these changes reached the site.

    Meant to be called only once a new value has been read back from somewhere
    independent of the tool that wrote it, so that a stamp means the change was
    observed rather than merely submitted.

    :param path: The ledger file
    :param landed: The (record id, field) pairs that were verified
    :param when: Timestamp to write
    :return: How many rows were stamped
    """
    rows = read_ledger(path)
    if not rows:
        return 0
    columns = list(rows[0])
    stamped = 0
    for row in rows:
        if (row["num"], row["field"]) in landed and not row.get("updated_at"):
            row["updated_at"] = when
            stamped += 1
    write_ledger(path, rows, columns)
    return stamped
