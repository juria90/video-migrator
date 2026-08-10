#!/usr/bin/env python3
"""
Tests for the correction ledger's merge.

Every test here pins an invariant that a merge must not break, because the
ledger is the only record of work that cannot be rediscovered: an answer found
by reading a bulletin, a title settled by hand, the fact that a change was made
at all. A merge that loses one of those loses it permanently.

Two of these describe bugs that reached real data. See ``test_landing_beats_``
``withdrawal`` and ``test_a_hand_recovered_answer_survives_a_scan``.
"""

import pathlib

import pytest

from video_migrator.ledger import CONFIRMED, SCAN_SOURCE, merge, read_ledger, write_ledger

STAMP = "2024-01-01 12:00"

#: Ledger field -> the key that field has in a record, as a site would define it.
FIELD_TAG = {"title": "subject", "verse": "word", "preacher": "preacher"}


def row(num="1", field="title", old="", new="", source=SCAN_SOURCE, updated_at="", **extra):
    """
    Build one ledger row.

    :param num: The record the row is about
    :param field: The field the row is about
    :param old: The value the site held when the row was written
    :param new: The value the row wants the site to hold
    :param source: Where the new value came from
    :param updated_at: Blank while the row is outstanding, a timestamp once it has landed
    :param extra: Any further columns to set
    :return: The row
    """
    return {"num": num, "field": field, "old": old, "new": new, "reason": "",
            "source": source, "updated_at": updated_at, **extra}


def records(**by_num):
    """
    Build the records a scrape would return, one field each.

    :param by_num: Record id -> the title that record holds
    :return: Records keyed as the merge expects
    """
    return {num: {"subject": subject, "word": "", "preacher": ""} for num, subject in by_num.items()}


def finding(num="1", field="title", old="", new="", reason="a rule fired"):
    """
    Build one thing a scan reports.

    :param num: The record the finding is about
    :param field: The field the finding is about
    :param old: The value the scan saw
    :param new: The value the scan wants
    :param reason: Why the scan says so
    :return: The finding
    """
    return (num, field, old, new, reason)


def test_landing_beats_withdrawal():
    """
    A correction that lands becomes history rather than disappearing.

    The two steps race: a correction reaching the site is the same moment its
    rule stops firing, so a merge that withdrew first would delete each row at
    the instant it earned its place. This is the bug that silently erased an
    applied correction from a real ledger.
    """
    ledger = [row(num="1", old="설교 제목", new="설교 제목 (1부)")]
    merged, tally = merge(ledger, records(**{"1": "설교 제목 (1부)"}), [], STAMP, FIELD_TAG)

    assert len(merged) == 1, "the row must not be withdrawn"
    assert merged[0]["updated_at"] == STAMP
    assert merged[0]["source"].endswith(CONFIRMED)
    assert tally["landed"] == 1
    assert tally["withdrawn"] == 0


def test_a_hand_recovered_answer_survives_a_scan():
    """
    A rule may refresh what a person's row saw, never what it concluded.

    A scan can say no more than "this field is missing". Someone who read a
    bulletin knows what belongs there. When both speak about one field the
    person wins, or the merge overwrites recovered answers with placeholders —
    which is what once happened to 25 of them.
    """
    ledger = [row(num="1", field="verse", old=".", new="요한복음 3:16", source="bulletin")]
    found = [finding(num="1", field="verse", old="-", new="(look it up)")]
    merged, tally = merge(ledger, records(**{"1": ""}), found, STAMP, FIELD_TAG)

    assert merged[0]["new"] == "요한복음 3:16", "the recovered answer must stand"
    assert merged[0]["source"] == "bulletin"
    assert merged[0]["old"] == "-", "but what the site holds is refreshed"
    assert tally["old value refreshed"] == 1
    assert tally["revised"] == 0


def test_a_scan_may_revise_its_own_row():
    """A row the scan raised is the scan's to sharpen."""
    ledger = [row(num="1", new="(look it up)")]
    found = [finding(num="1", old="설교 제목", new="설교 제목 (1부)", reason="better rule")]
    merged, tally = merge(ledger, records(**{"1": "설교 제목"}), found, STAMP, FIELD_TAG)

    assert len(merged) == 1, "revised in place, not duplicated"
    assert merged[0]["new"] == "설교 제목 (1부)"
    assert merged[0]["reason"] == "better rule"
    assert tally["revised"] == 1


def test_an_applied_row_is_never_rewritten():
    """
    History is not editable, even when the field has drifted since.

    A field that changes again needs a new row saying so. Rewriting the old one
    would lose that the first change ever happened.
    """
    done = row(num="1", old="설교 제목", new="설교 제목 (1부)", updated_at=STAMP)
    found = [finding(num="1", old="설교 제목 (2부)", new="설교 제목 (3부)")]
    merged, tally = merge([done], records(**{"1": "설교 제목 (2부)"}), found, "2024-06-01 09:00", FIELD_TAG)

    assert merged[0]["new"] == "설교 제목 (1부)", "the applied row is untouched"
    assert merged[0]["updated_at"] == STAMP
    assert len(merged) == 2, "the new discrepancy gets its own row"
    assert tally["added"] == 1


def test_a_rule_that_stops_firing_withdraws_its_own_row():
    """A rule that was wrong must not leave its rows behind for good."""
    ledger = [row(num="1", new="(look it up)")]
    merged, tally = merge(ledger, records(**{"1": "설교 제목"}), [], STAMP, FIELD_TAG)

    assert merged == []
    assert tally["withdrawn"] == 1


def test_withdrawal_spares_what_the_scan_did_not_raise():
    """
    Only the scan's own outstanding rows are the scan's to retract.

    An answer from a person, a row already applied, and a record this scrape
    never saw are all outside its authority — the last because absence from a
    scrape means unexamined, not resolved.
    """
    ledger = [
        row(num="1", new="요한복음 3:16", source="bulletin"),
        row(num="2", new="설교 제목 (1부)", updated_at=STAMP),
        row(num="3", new="(look it up)"),
    ]
    merged, tally = merge(ledger, records(**{"1": "", "2": "설교 제목 (2부)"}), [], STAMP, FIELD_TAG)

    assert {r["num"] for r in merged} == {"1", "2", "3"}
    assert tally["withdrawn"] == 0


def test_a_repeat_merge_changes_nothing():
    """
    Merging twice over unchanged records is a no-op.

    Without this the file churns on every run, and a diff stops being evidence
    that something happened.
    """
    found = [finding(num="1", old="설교 제목", new="설교 제목 (1부)")]
    once, _ = merge([], records(**{"1": "설교 제목"}), found, STAMP, FIELD_TAG)
    snapshot = [dict(r) for r in once]
    twice, tally = merge(once, records(**{"1": "설교 제목"}), found, STAMP, FIELD_TAG)

    assert twice == snapshot
    assert tally["added"] == 0
    assert tally["withdrawn"] == 0
    assert tally["revised"] == 0


def test_a_field_that_drifts_back_does_not_churn():
    """
    A correction undone upstream does not re-raise a row already applied.

    Someone reverting a change on purpose would otherwise see the same row
    proposed forever.
    """
    done = row(num="1", old="설교 제목", new="설교 제목 (1부)", updated_at=STAMP)
    found = [finding(num="1", old="설교 제목", new="설교 제목 (1부)")]
    merged, tally = merge([done], records(**{"1": "설교 제목"}), found, STAMP, FIELD_TAG)

    assert len(merged) == 1
    assert tally["added"] == 0


def test_one_row_per_field_per_record():
    """Two fields of one record are two rows, and neither crowds the other out."""
    found = [
        finding(num="1", field="title", old="설교 제목", new="설교 제목 (1부)"),
        finding(num="1", field="verse", old=".", new="(look it up)"),
    ]
    merged, tally = merge([], records(**{"1": "설교 제목"}), found, STAMP, FIELD_TAG)

    assert {r["field"] for r in merged} == {"title", "verse"}
    assert tally["added"] == 2


def test_added_rows_carry_the_site_specific_columns():
    """Whatever a site needs to find the record again is set when the row is made."""
    found = [finding(num="7", old="설교 제목", new="설교 제목 (1부)")]
    merged, _ = merge([], records(**{"7": "설교 제목"}), found, STAMP, FIELD_TAG,
                      row_extras=lambda num: {"edit_url": f"https://admin.example.org/edit?num={num}"})

    assert merged[0]["edit_url"] == "https://admin.example.org/edit?num=7"
    assert merged[0]["source"] == SCAN_SOURCE


def test_a_record_missing_from_the_scrape_is_left_alone():
    """A scrape that did not reach a record says nothing about it."""
    ledger = [row(num="404", new="(look it up)")]
    merged, tally = merge(ledger, records(**{"1": "설교 제목"}), [], STAMP, FIELD_TAG)

    assert len(merged) == 1
    assert not merged[0]["updated_at"]
    assert tally["withdrawn"] == 0


@pytest.mark.parametrize("columns", [
    ["num", "field", "old", "new", "reason", "source", "updated_at"],
    ["num", "field", "old", "new", "reason", "source", "updated_at", "edit_url"],
])
def test_a_ledger_survives_the_round_trip(tmp_path, columns):
    """
    What is written is what is read back, including the columns a site adds.

    :param tmp_path: Directory to write the ledger into
    :param columns: The column set to write
    """
    path = pathlib.Path(tmp_path) / "ledger.tsv"
    original = [row(num="1", old="설교 제목", new="설교 제목 (1부)",
                    edit_url="https://admin.example.org/edit?num=1")]
    write_ledger(path, original, columns)

    assert read_ledger(path) == [{c: original[0].get(c, "") for c in columns}]


def test_reading_a_ledger_that_is_not_there_yet(tmp_path):
    """
    The first run has nothing to read and must not fail.

    :param tmp_path: A directory with no ledger in it
    """
    assert read_ledger(pathlib.Path(tmp_path) / "absent.tsv") == []
