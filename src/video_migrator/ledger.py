#!/usr/bin/env python3
"""
A merge-only ledger of field corrections made to a source site.

One row per field per record. A row says what the field held, what it should
hold, why, and where the new value came from. ``updated_at`` is blank until the
change lands and a timestamp afterwards, so one file is both the work queue and
the history of what was done.

The ledger is **merged into, never rebuilt**. A scan can rediscover that a field
is wrong, but not that someone read a bulletin to find the answer, nor why a
title was reworded. Those facts live only here, so a merge adds rows it has not
seen, stamps rows the site now satisfies, retracts rows its own rules no longer
raise, and leaves everything else alone.

Nothing here knows what a record means. The rules that judge a field, and the
columns naming where to edit it, belong to whichever site is being corrected.
"""

import collections
import csv
import pathlib
from collections.abc import Callable

#: Marks a row the scan raised itself, and may therefore revise or retract. Any
#: other source — a bulletin, a decision made by hand — outranks the rules.
SCAN_SOURCE = "scan rule"

#: Appended to a row's source when the site is found to hold its new value.
CONFIRMED = "; confirmed on the site"

#: The columns a merge maintains. A caller may carry any others alongside.
CORE_COLUMNS = ("num", "field", "old", "new", "reason", "source", "updated_at")

#: What a scan reports: record id, field name, the value found, the value wanted,
#: and why. ``new`` may be a question rather than a value, for a person to settle.
Finding = tuple[str, str, str, str, str]


def read_ledger(path: pathlib.Path) -> list[dict[str, str]]:
    """
    Read a ledger from disk.

    :param path: The TSV to read
    :return: Its rows in file order, or an empty list if there is no such file
    """
    if not path.is_file():
        return []
    with path.open(encoding="utf-8") as handle:
        return [dict(row) for row in csv.DictReader(handle, delimiter="\t")]


def write_ledger(path: pathlib.Path, rows: list[dict[str, str]], columns: list[str]) -> None:
    """
    Write a ledger to disk.

    :param path: The TSV to write
    :param rows: The rows to write, in the order they should appear
    :param columns: The columns to write, in order; missing values are left blank
    :return: None
    """
    lines = ["\t".join(columns)] + ["\t".join((row.get(c) or "") for c in columns) for row in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def is_scan_row(row: dict[str, str]) -> bool:
    """
    Did the scan raise this row itself?

    :param row: A ledger row
    :return: True when the row's source is the scan's own rules
    """
    return (row.get("source") or "").startswith(SCAN_SOURCE)


def merge(
    ledger: list[dict[str, str]],
    records: dict[str, dict[str, str]],
    found: list[Finding],
    stamp: str,
    field_tag: dict[str, str],
    row_extras: Callable[[str], dict[str, str]] | None = None,
) -> tuple[list[dict[str, str]], collections.Counter]:
    """
    Bring a ledger up to date with the site it describes, without losing anything.

    The four steps run in this order because each depends on the last:

    1. **Landing** is settled first. A correction reaching the site is the same
       moment its rule stops firing, so anything that ran before this would see
       finished work as merely absent.
    2. **Withdrawal** then retracts the scan's own outstanding rows that it can
       no longer raise, which is what stops a rule that was wrong leaving its
       rows behind for good. It never touches a stamped row, an answer that came
       from somewhere other than the rules, or a record this scrape did not see.
    3. **Revision** lets the scan sharpen a suggestion it made before. Where the
       row came from a person, only ``old`` is refreshed — a rule that can say no
       more than "missing" must not overwrite an answer someone went and found.
    4. **Addition** records discrepancies that have no row yet.

    :param ledger: The ledger as it stands; modified in place and returned
    :param records: Record id -> the fields as the site currently holds them
    :param found: What the scan makes of those records now
    :param stamp: Timestamp to write against a row found to have landed
    :param field_tag: Ledger field name -> the key it has in a record
    :param row_extras: Given a record id, extra columns to set on a row newly added for it
    :return: The merged rows and a tally of what happened to them
    """
    tally: collections.Counter = collections.Counter()

    for row in ledger:
        if row.get("updated_at"):
            continue
        record = records.get(row["num"])
        if record and record.get(field_tag[row["field"]]) == row["new"]:
            row["updated_at"] = stamp
            row["source"] = (row.get("source") or "") + CONFIRMED
            tally["landed"] += 1

    raised = {(num, name) for num, name, _, _, _ in found}
    for row in list(ledger):
        if (row.get("updated_at") or not is_scan_row(row)
                or row["num"] not in records or (row["num"], row["field"]) in raised):
            continue
        ledger.remove(row)
        tally["withdrawn"] += 1

    pending = {(r["num"], r["field"]): r for r in ledger if not r.get("updated_at")}
    tally["still pending"] = len(pending)

    for num, name, old, new, reason in found:
        key = (num, name)
        if key in pending and pending[key]["new"] == new:
            # The proposal stands, but the record may have moved under it — an
            # earlier round of the same rule, or a hand edit. Leaving ``old`` as
            # it was would make the row diverge from the site for good, since a
            # value matching neither ``old`` nor ``new`` is never written.
            if pending[key]["old"] != old:
                pending[key]["old"] = old
                tally["old value refreshed"] += 1
            continue                      # already queued, nothing else to say
        if any(r["num"] == num and r["field"] == name and r["new"] == new and r.get("updated_at")
               for r in ledger):
            continue                      # applied before and has drifted back; a fresh row would churn
        if key in pending:
            row = pending[key]
            if is_scan_row(row):
                row.update({"old": old, "new": new, "reason": reason})
                tally["revised"] += 1
            elif row["old"] != old:
                row["old"] = old
                tally["old value refreshed"] += 1
            continue
        ledger.append({
            "num": num, "field": name, "old": old, "new": new, "reason": reason,
            "source": SCAN_SOURCE, "updated_at": "", **(row_extras(num) if row_extras else {}),
        })
        tally["added"] += 1
    return ledger, tally
