#!/usr/bin/env python3
"""Tests for the record of what a migration has done and what is left."""

import threading

import pytest

from video_migrator.logs import Ticker
from video_migrator.plan import (
    COLUMNS,
    DONE,
    STAGES,
    UNDATED,
    merge,
    order,
    outstanding,
    read_plan,
    reset,
    stage_of,
    tally_stages,
    write_plan,
)


def planned(num: str, **stamps) -> dict[str, str]:
    """
    Build a plan row stamped up to some stage.

    :param num: The record id
    :param stamps: Stage columns to fill
    :return: The row
    """
    row = dict.fromkeys(COLUMNS, "")
    row["num"] = num
    row.update(stamps)
    return row


def test_a_new_recording_starts_at_the_first_stage() -> None:
    """Nothing has happened to it yet, so everything is still to do."""
    plan, tally = merge([], [{"num": "12", "ID": "34", "title": "설교 제목"}])
    assert tally == {"added": 1, "retitled": 0, "unchanged": 0}
    assert stage_of(plan[0]) == "fetch"
    assert set(plan[0]) == set(COLUMNS)


def test_stages_are_answered_in_order() -> None:
    """Each stamp advances the row to the next thing it is waiting on."""
    row = planned("1")
    for stage, column in STAGES:
        assert stage_of(row) == stage
        row[column] = "2026-08-22 09:00"
    assert stage_of(row) == DONE


def test_a_second_pass_adds_only_what_is_new() -> None:
    """
    Re-planning is how a migration picks up recordings scraped since.

    :return: None
    """
    plan, _ = merge([], [{"num": "1", "ID": "10", "title": "설교 제목 하나"}])
    plan[0]["fetched_at"] = "2026-08-22 09:00"
    plan, tally = merge(plan, [{"num": "1", "ID": "10", "title": "설교 제목 하나"},
                               {"num": "2", "ID": "20", "title": "설교 제목 둘"}])
    assert tally["added"] == 1 and tally["unchanged"] == 1
    assert [row["num"] for row in plan] == ["1", "2"]
    # The work already done on the first is untouched.
    assert plan[0]["fetched_at"] == "2026-08-22 09:00"


def test_a_corrected_title_reaches_a_recording_not_yet_uploaded() -> None:
    """
    Titles keep changing at the source while a migration runs for weeks.

    Refreshing one costs nothing before the upload and is the difference between
    publishing the settled title and the one that happened to be current when
    the plan was first written.
    """
    plan, _ = merge([], [{"num": "1", "ID": "10", "title": "설교 제목"}])
    plan, tally = merge(plan, [{"num": "1", "ID": "10", "title": "고친 설교 제목"}])
    assert plan[0]["title"] == "고친 설교 제목"
    assert tally["retitled"] == 1


def test_a_recording_no_longer_selected_is_kept() -> None:
    """
    Narrowing the selection must not erase what was already published.

    Which YouTube video a recording became is knowable from nowhere else, so a
    row is never dropped however the selection changes.
    """
    plan, _ = merge([], [{"num": "1", "ID": "10", "title": "설교 제목 하나"},
                         {"num": "2", "ID": "20", "title": "설교 제목 둘"}])
    plan[1]["youtube_id"] = "abc123"
    plan, _ = merge(plan, [{"num": "1", "ID": "10", "title": "설교 제목 하나"}])
    assert [row["num"] for row in plan] == ["1", "2"]
    assert plan[1]["youtube_id"] == "abc123"


def test_a_failed_row_is_still_outstanding() -> None:
    """
    A note records why a stage did not happen; it does not close the row.

    Most failures are a timeout or a rate limit, and the next run should simply
    try again. What ends a retry loop is a person reading the note.
    """
    row = planned("1", note="download timed out")
    assert outstanding([row]) == [row]
    assert stage_of(row) == "fetch"


def test_a_finished_recording_drops_out_of_the_work() -> None:
    """Every stage stamped means there is nothing left to do to it."""
    finished = planned("1", **{column: "x" for _, column in STAGES})
    assert outstanding([finished]) == []
    assert tally_stages([finished]) == {DONE: 1}


def test_a_plan_survives_a_round_trip(tmp_path) -> None:
    """
    The file is the only memory a migration has between runs.

    :param tmp_path: Fixture supplying a directory to write into
    """
    plan, _ = merge([], [{"num": "9", "ID": "90", "title": "설교 제목"}])
    plan[0]["note"] = "held\tby a tab"
    path = tmp_path / "plan.tsv"
    write_plan(path, plan)
    assert b"\r\n" not in path.read_bytes()
    read = read_plan(path)
    assert read[0]["num"] == "9" and read[0]["title"] == "설교 제목"
    # A tab in a value would otherwise become a column boundary on the way back.
    assert read[0]["note"] == "held by a tab"


def test_reading_a_plan_that_does_not_exist_yet_is_not_an_error(tmp_path) -> None:
    """
    The first run has no plan to read.

    :param tmp_path: Fixture supplying a directory with no plan in it
    """
    assert read_plan(tmp_path / "absent.tsv") == []


@pytest.mark.parametrize("stage,column", list(STAGES))
def test_every_stage_has_a_column_and_the_column_is_kept(stage, column) -> None:
    """
    A stage that stamped a column the file does not hold would be forgotten.

    :param stage: The stage's name
    :param column: The column it stamps
    """
    assert column in COLUMNS


def test_redoing_a_stage_forgets_what_that_stage_produced() -> None:
    """
    Undoing an upload must forget the video, not just the timestamp.

    A plan naming a YouTube video that has been deleted is worse than one naming
    none: it reads as finished work, and nothing will look at it again.
    """
    row = planned("1", **{column: "x" for _, column in STAGES})
    row["youtube_id"] = "aBcDeFgHiJk"
    reset(row, "upload")
    assert stage_of(row) == "upload"
    assert row["youtube_id"] == ""
    # Earlier stages are untouched, so the master is not fetched again.
    assert row["fetched_at"] == "x" and row["measured_at"] == "x"


def test_redoing_an_early_stage_undoes_the_later_ones_too() -> None:
    """
    The stages are a sequence, so going back to one goes back past all of them.

    A recording being fetched again has not been uploaded, whatever the plan
    says — the file it was uploaded from is about to be replaced.
    """
    row = planned("1", **{column: "x" for _, column in STAGES})
    row.update(youtube_id="aBcDeFgHiJk", path="/somewhere/1.mp4", repair="none")
    reset(row, "fetch")
    assert stage_of(row) == "fetch"
    assert row["youtube_id"] == "" and row["path"] == "" and row["repair"] == ""


def test_redoing_clears_the_note_that_explained_the_failure() -> None:
    """A stale reason is misleading once the work is queued to happen again."""
    row = planned("1", fetched_at="x", note="upload: HttpError: video too long")
    reset(row, "upload")
    assert row["note"] == ""


def test_a_ticker_reports_at_once_then_holds_off() -> None:
    """
    A stage should say it has started, then go quiet until it has something new.

    Waiting for the first interval would leave the longest silence exactly where
    it is least welcome — at the beginning, before anything has been reported at
    all.
    """
    clock = [1000.0]
    ticker = Ticker(30.0, lambda: clock[0])
    assert ticker.due() is True
    assert ticker.due() is False

    clock[0] += 29.9
    assert ticker.due() is False

    clock[0] += 0.2
    assert ticker.due() is True
    assert ticker.due() is False


def test_reports_stretch_out_as_a_stage_runs_long() -> None:
    """
    A minute-and-a-half fetch and a twenty-minute encode want different rhythms.

    Half a minute suits the first and would fill a screen for the second, so
    the interval grows with elapsed time rather than being predicted from a
    total nobody knows in advance.
    """
    clock = [0.0]
    ticker = Ticker(clock=lambda: clock[0])
    assert ticker.interval() == 30.0

    clock[0] = 600
    assert ticker.interval() == 60.0

    clock[0] = 36000
    assert ticker.interval() == 180.0


def test_a_long_stage_reports_a_bounded_number_of_times() -> None:
    """
    However long a stage runs, it says so about ten times rather than hundreds.

    :return: None
    """
    clock = [0.0]
    ticker = Ticker(clock=lambda: clock[0])
    reports = 0
    for second in range(0, 3600, 5):
        clock[0] = second
        reports += ticker.due()
    assert 20 <= reports <= 45


def test_the_plan_runs_oldest_first_rather_than_by_record_id() -> None:
    """
    The board's numbering runs backwards through the back-catalogue.

    Sorting by ``num`` would work through 2019 towards 2010 and then jump
    forward to 2021, which is the order this shape of board produces and not an
    order anybody wants a migration done in.
    """
    plan, _ = merge([], [
        {"num": "32", "ID": "1", "date": "2019-12-29"},
        {"num": "489", "ID": "2", "date": "2010-11-07"},
        {"num": "560", "ID": "3", "date": "2021-01-03"},
    ])
    assert [row["num"] for row in plan] == ["489", "32", "560"]


def test_a_recording_the_site_never_dated_waits_until_last() -> None:
    """
    An undated row sorts on :data:`UNDATED`, not on the empty string.

    Sorting blank first would open a migration with the recordings least is
    known about, where sorting it last leaves them for someone to look at.
    """
    plan, _ = merge([], [{"num": "1", "ID": "1", "date": ""},
                         {"num": "2", "ID": "2", "date": "2026-08-09"}])
    assert [row["num"] for row in plan] == ["2", "1"]
    assert order(plan[1])[0] == UNDATED


def test_two_recordings_published_the_same_day_keep_their_record_order() -> None:
    """A morning and an evening service share a date, and num separates them."""
    plan, _ = merge([], [{"num": "8", "ID": "1", "date": "2026-08-09"},
                         {"num": "7", "ID": "2", "date": "2026-08-09"}])
    assert [row["num"] for row in plan] == ["7", "8"]


def test_a_date_corrected_at_the_source_moves_the_recording() -> None:
    """
    Dates are corrected on the board like titles are, and the plan follows.

    A recording filed under the wrong year would otherwise keep the position
    the wrong year gave it for the rest of the migration.
    """
    plan, _ = merge([], [{"num": "1", "ID": "1", "date": "2026-01-04"},
                         {"num": "2", "ID": "2", "date": "2011-06-19"}])
    assert [row["num"] for row in plan] == ["2", "1"]
    plan, _ = merge(plan, [{"num": "1", "ID": "1", "date": "2010-01-04"},
                           {"num": "2", "ID": "2", "date": "2011-06-19"}])
    assert [row["num"] for row in plan] == ["1", "2"]


def test_a_plan_written_before_dates_were_kept_gains_them(tmp_path) -> None:
    """
    The column is added to a file that predates it without losing a stage.

    :param tmp_path: Where to write the plan
    """
    path = tmp_path / "plan.tsv"
    older = [column for column in COLUMNS if column != "published"]
    row = dict.fromkeys(older, "")
    row.update(num="489", vimeo_id="1", fetched_at="2026-08-22 09:00", youtube_id="aBcDeFgHiJk")
    path.write_text("\t".join(older) + "\n" + "\t".join(row[column] for column in older) + "\n",
                    encoding="utf-8", newline="\n")

    plan, _ = merge(read_plan(path), [{"num": "489", "ID": "1", "date": "2010-11-07"}])
    write_plan(path, plan)

    reread = read_plan(path)
    assert reread[0]["published"] == "2010-11-07"
    assert reread[0]["fetched_at"] == "2026-08-22 09:00"
    assert reread[0]["youtube_id"] == "aBcDeFgHiJk"


def test_writing_a_plan_leaves_nothing_beside_it(tmp_path) -> None:
    """
    The file it is staged through is moved, not copied and left.

    :param tmp_path: Where to write the plan
    """
    path = tmp_path / "plan.tsv"
    write_plan(path, [planned("1")])
    assert [child.name for child in tmp_path.iterdir()] == ["plan.tsv"]


def test_a_plan_being_rewritten_is_never_seen_half_written(tmp_path) -> None:
    """
    A reader either sees the previous plan or the new one, never a truncation.

    Writing over the top of the file would leave a window in which it holds
    fewer rows than either version, and a run killed inside that window would
    lose the record of what it had already published.

    :param tmp_path: Where to write the plan
    """
    path = tmp_path / "plan.tsv"
    write_plan(path, [planned(str(n)) for n in range(50)])

    seen = []
    stop = threading.Event()

    def keep_reading() -> None:
        while not stop.is_set():
            seen.append(len(read_plan(path)))

    reader = threading.Thread(target=keep_reading)
    reader.start()
    try:
        for size in (200, 50) * 20:
            write_plan(path, [planned(str(n)) for n in range(size)])
    finally:
        stop.set()
        reader.join()

    assert seen, "the reader never got a look in"
    assert set(seen) <= {50, 200}, f"saw a partly written plan: {sorted(set(seen))}"


def test_several_threads_stamping_at_once_keep_every_row(tmp_path) -> None:
    """
    The driver writes the whole file after each stage, from whichever thread.

    :param tmp_path: Where to write the plan
    """
    path = tmp_path / "plan.tsv"
    rows = [planned(str(n)) for n in range(20)]
    writing = threading.Lock()

    def stamp(row: dict[str, str]) -> None:
        for column in ("fetched_at", "measured_at", "repaired_at"):
            row[column] = "2026-08-23 15:00"
            with writing:
                write_plan(path, rows)

    threads = [threading.Thread(target=stamp, args=(row,)) for row in rows]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    written = read_plan(path)
    assert len(written) == 20
    assert all(row["repaired_at"] == "2026-08-23 15:00" for row in written)
