#!/usr/bin/env python3
"""
Tests for editing videos that are already published.

The date swap is the whole of this tool's judgement, and it runs against
recordings nobody can re-upload: a wrong edit is applied to a live video and the
title it replaced is gone. So the cases that must *not* change anything are
tested at least as hard as the one that must.
"""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "tools"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

import republish  # noqa: E402

from video_migrator.config import load_profile  # noqa: E402
from video_migrator.metadata.summarize import summarize, summary_path, write_text  # noqa: E402
from video_migrator.metadata.upload_description import format_upload_description  # noqa: E402
from video_migrator.models import Video  # noqa: E402

OLD = "{month:02d}{day:02d}{short_year:02d}"
NEW = "{short_year:02d}{month:02d}{day:02d}"


def swap(title: str, published: str = "2026-08-02") -> tuple[str, str]:
    """
    Rewrite a title's date between the two formats this migration uses.

    :param title: The title as published
    :param published: The date the site gave the recording
    :return: The rewritten title and the reason it was left alone
    """
    return republish.rewrite_date(title, published, OLD, NEW)


def test_the_date_token_is_rewritten_in_place() -> None:
    """Only the six digits change; every other character survives untouched."""
    assert swap('[예시교회 - 홍길동 목사] 080226 주일예배 | "설교 제목" 요 21:15') == (
        '[예시교회 - 홍길동 목사] 260802 주일예배 | "설교 제목" 요 21:15', "")


def test_a_title_already_rewritten_is_recognized_rather_than_refused() -> None:
    """
    This is what makes the pass resumable.

    A run interrupted half way leaves videos on both sides of the edit, and the
    next run reads them all back. One already carrying the new date holds no
    match for the old one — which must read as done, not as a title whose date
    could not be found.
    """
    assert swap("[예시교회] 260802 주일예배") == ("[예시교회] 260802 주일예배", "already 260802")


def test_a_date_both_formats_write_alike_is_left_alone() -> None:
    """MMDDYY and YYMMDD agree whenever month, day and year are the same number."""
    title = "[예시교회] 111111 주일예배"
    assert swap(title, published="2011-11-11") == (title, "both formats write 111111")


def test_the_token_appearing_twice_refuses_to_guess() -> None:
    """
    A sermon title holding the same six digits gives no way to tell which is
    the date, so the row is reported and nothing is sent.
    """
    title = '[예시교회] 080226 주일예배 | "080226 특별집회"'
    assert swap(title) == (title, "080226 appears 2 times")


def test_digits_around_the_token_do_not_count_as_the_date() -> None:
    """
    The match is bounded, so a longer run of digits is not a date hiding inside
    a larger number — which would otherwise be edited into nonsense.
    """
    title = "[예시교회] 1080226 주일예배"
    assert swap(title) == (title, "080226 appears 0 times")


def test_a_recording_the_site_gave_no_date_is_left_alone() -> None:
    """
    ``format_date`` hands back what it cannot parse, so both formats produce the
    same non-date and the row is skipped rather than having that string hunted
    for in the title.
    """
    title = "[예시교회] 주일예배"
    _rewritten, note = swap(title, published="")
    assert note == "both formats write "


def test_only_rows_that_became_a_video_are_considered() -> None:
    """A plan is mostly recordings that have not been uploaded yet."""
    plan = [
        {"num": "1", "youtube_id": "abc"},
        {"num": "2", "youtube_id": ""},
        {"num": "3"},
        {"num": "4", "youtube_id": "  "},
    ]
    assert [row["num"] for row in republish.published_videos(plan)] == ["1"]


@pytest.fixture
def plan() -> list[dict[str, str]]:
    """
    Two published recordings and one that has not been uploaded.

    :return: The plan rows
    """
    return [
        {"num": "1", "youtube_id": "aaa", "published": "2026-08-02", "title": "[예시교회] 080226 주일예배"},
        {"num": "2", "youtube_id": "bbb", "published": "2026-08-09", "title": "[예시교회] 080926 주일예배"},
        {"num": "3", "youtube_id": "", "published": "2026-08-16", "title": "[예시교회] 081626 주일예배"},
    ]


def test_each_published_row_is_decided_from_what_youtube_says(plan) -> None:
    """
    The live title is what gets edited, not the plan's copy of it.

    A correction applied at the source after the upload leaves the two
    disagreeing, and the published text is the one a viewer sees.
    """
    snippets = {
        "aaa": {"title": "[예시교회] 080226 주일예배 (고친 제목)"},
        "bbb": {"title": "[예시교회] 080926 주일예배"},
    }
    decided = republish.retitle(plan, snippets, NEW, OLD)

    assert [row["num"] for row, _snippet, _wanted, _note in decided] == ["1", "2"]
    assert decided[0][2] == "[예시교회] 260802 주일예배 (고친 제목)"
    assert decided[1][2] == "[예시교회] 260809 주일예배"
    assert not any(note for _row, _snippet, _wanted, note in decided)


def test_a_video_youtube_does_not_know_is_reported_not_edited(plan) -> None:
    """
    A deleted video, or an id written down wrong, comes back from the read as
    nothing at all. That must not be mistaken for a title needing no change.
    """
    decided = republish.retitle(plan, {"aaa": {"title": "[예시교회] 080226 주일예배"}}, NEW, OLD)

    missing = next(entry for entry in decided if entry[0]["num"] == "2")
    assert missing[1] == {}
    assert missing[3] == "YouTube does not know this video"


def test_the_limit_counts_edits_rather_than_rows_looked_at() -> None:
    """
    ``--limit 1`` exists to put one video in front of a person before the rest
    follow. Spending it on a row that was never going to change would send
    nothing and prove nothing.
    """
    decided = [
        ({"num": "1"}, {}, "", "already 260802"),
        ({"num": "2"}, {}, "[예시교회] 260809 주일예배", ""),
        ({"num": "3"}, {}, "[예시교회] 260816 주일예배", ""),
    ]
    assert [row["num"] for row, _s, _w, _n in republish.to_change(decided, 1)] == ["2"]
    assert [row["num"] for row, _s, _w, _n in republish.to_change(decided, None)] == ["2", "3"]


def test_released_rows_are_marked_rather_than_sent_back_to_fetch(tmp_path) -> None:
    """
    Adding a stage column sends every existing row back to it.

    A released recording reads as summarize-outstanding, and a run would hand it
    to ``advance``, which would try to re-fetch a master that has been deleted —
    a gigabyte from Vimeo for each of a hundred and thirty-five videos that are
    already published. The marking pass is what stands between the new column
    and that, so it has to run before the stage does.

    :param tmp_path: Fixture supplying a directory
    """
    plan = [
        {"num": "1", "released_at": "2026-08-01 10:00", "summarized_at": ""},   # finished
        {"num": "2", "released_at": "", "summarized_at": ""},                   # still in flight
        {"num": "3", "released_at": "2026-08-02 10:00", "summarized_at": "x"},  # already marked
    ]
    assert republish.mark_summarized(plan) == 1

    assert plan[0]["summarized_at"] == republish.NOT_SUMMARIZED
    assert plan[1]["summarized_at"] == "", "a recording whose media is still on disk gets summarized"
    assert plan[2]["summarized_at"] == "x", "an existing stamp is not overwritten"


def test_marking_says_the_stage_did_not_run_rather_than_that_it_did() -> None:
    """
    A timestamp would claim a transcript exists. None does, and none will.
    """
    plan = [{"num": "1", "released_at": "2026-08-01 10:00", "summarized_at": ""}]
    republish.mark_summarized(plan)

    assert plan[0]["summarized_at"] == "n/a"
    assert ":" not in plan[0]["summarized_at"], "it must not read like a time"


def described(site_dir: pathlib.Path, summary: str, live_description: str = None) -> list:
    """
    Decide what one published video's description should become.

    :param site_dir: Where the summary file goes
    :param summary: What the recording's summary file holds, empty for no file
    :param live_description: What the video's description says now; by default
        the metadata-only one this migration wrote at upload
    :return: The decision, as :func:`republish.redescribe` returns it
    """
    profile = load_profile("example")
    board = profile.board("sunday_sermon")
    if summary:
        write_text(summary_path(site_dir, "12"), summary)

    video = Video(type="vimeo", id="999888777666", url="", embed_url="", title="설교 제목",
                  bible_verse="요한복음 21:15", publish_date="2026-08-02", artist="홍길동 목사",
                  genre="Sermon", language="kor")
    if live_description is None:
        live_description = format_upload_description(video, "", profile, board)

    plan = [{"num": "12", "vimeo_id": "999888777666", "youtube_id": "aBcDeFgHiJk",
             "title": "설교 제목", "published": "2026-08-02"}]
    snippets = {"aBcDeFgHiJk": {"title": "설교 제목", "description": live_description}}
    return republish.redescribe(plan, snippets, {"12": video}, site_dir, profile, board)


def test_a_video_whose_summary_is_not_written_yet_is_skipped(tmp_path) -> None:
    """
    Today that is all hundred and thirty-five of them.

    Pushing anyway would spend 50 quota units to write back the metadata-only
    description the video already carries, on a pass that is quota-bound.

    :param tmp_path: Fixture supplying a directory
    """
    (_row, _snippet, wanted, note) = described(tmp_path, "")[0]

    assert note == "no summary written yet"
    assert wanted == "", "nothing is built for a video that will not be sent"


def test_a_summary_still_carrying_its_stub_marker_is_skipped_too(tmp_path) -> None:
    """
    The marker is the whole publishing rule, and both writers have to test it.

    A stub holds the transcript. Sent, it would put tens of thousands of
    characters into the description of a live sermon.

    :param tmp_path: Fixture supplying a directory
    """
    stub = summarize("설교 본문 요한복음 하나 둘", "stub")
    (_row, _snippet, wanted, note) = described(tmp_path, stub)[0]

    assert note == "no summary written yet"
    assert wanted == ""


def test_a_written_summary_is_built_into_the_description_and_sent(tmp_path) -> None:
    """
    A summary somebody finished by hand is what this pass exists to publish.

    :param tmp_path: Fixture supplying a directory
    """
    summary = "고친 설교 본문 요한복음 다섯"
    (_row, _snippet, wanted, note) = described(tmp_path, summary + "\n")[0]

    assert note == ""
    assert wanted.startswith(summary)
    # The verse and the preacher survive. Rebuilding a description without them
    # would delete both from a video that is carrying them.
    assert "본문: 요한복음 21:15" in wanted
    assert "설교: 홍길동 목사" in wanted


def test_a_video_that_already_says_it_is_not_sent_again(tmp_path) -> None:
    """
    Re-running the pass must be free. Each edit costs 50 of a daily 10,000.

    :param tmp_path: Fixture supplying a directory
    """
    summary = "고친 설교 본문 요한복음 다섯"
    built = described(tmp_path, summary + "\n")[0][2]
    (_row, _snippet, _wanted, note) = described(tmp_path, summary + "\n", live_description=built)[0]

    assert note == "already says this"


def test_a_description_somebody_edited_by_hand_is_never_overwritten(tmp_path) -> None:
    """
    ``videos.update`` replaces the field whole, so pushing over an edit destroys it.

    The check is exact — the live text must be what this profile renders for
    this recording with no summary — which is also what stops a second pass
    prepending a changed summary on top of the one already published.

    :param tmp_path: Fixture supplying a directory
    """
    summary = "고친 설교 본문 요한복음 다섯"
    (_row, _snippet, _wanted, note) = described(
        tmp_path, summary + "\n", live_description="설교 제목\n\n본문: 요한복음 21:15")[0]

    assert note == "the description is not the one this migration wrote"


def test_a_recording_the_export_does_not_cover_is_left_alone(tmp_path) -> None:
    """
    Without the export there is no verse and no preacher, and a description
    built without them would delete both.

    :param tmp_path: Fixture supplying a directory
    """
    profile = load_profile("example")
    write_text(summary_path(tmp_path, "12"), "고친 설교 본문 요한복음 다섯\n")
    plan = [{"num": "12", "vimeo_id": "999888777666", "youtube_id": "aBcDeFgHiJk",
             "title": "설교 제목", "published": "2026-08-02"}]
    snippets = {"aBcDeFgHiJk": {"title": "설교 제목", "description": "본문: 요한복음 21:15"}}

    decided = republish.redescribe(plan, snippets, {}, tmp_path, profile,
                                   profile.board("sunday_sermon"))
    assert decided[0][3] == "the export does not cover this recording"
    assert decided[0][2] == ""
