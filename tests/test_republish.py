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

import republish  # noqa: E402

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
