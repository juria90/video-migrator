#!/usr/bin/env python3
"""
Tests for the description an upload is published under.

The case that matters most is the one where there is no summary: while
summarization is stubbed that is every recording, so the description a new
upload carries has to come out exactly as it did before this stage existed. A
regression there would be applied to several hundred videos before anybody read
one.
"""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from video_migrator.config import load_profile  # noqa: E402
from video_migrator.metadata.render import render  # noqa: E402
from video_migrator.metadata.upload_description import (  # noqa: E402
    MAX_DESCRIPTION_LENGTH,
    format_upload_description,
)
from video_migrator.models import Video  # noqa: E402

#: A placeholder summary, in the allowlist's vocabulary per CLAUDE.md.
SUMMARY = "고친 설교 본문 요한복음 다섯"


def sermon(**overrides) -> Video:
    """
    A normalized record standing in for one scraped recording.

    :param overrides: Fields to set
    :return: The record
    """
    fields = {"type": "vimeo", "id": "999888777666", "url": "", "embed_url": "",
              "title": "설교 제목", "publish_date": "2026-08-02", "artist": "홍길동 목사",
              "bible_verse": "요한복음 21:15"}
    fields.update(overrides)
    return Video(**fields)


@pytest.fixture
def profile():
    """
    The published example profile.

    :return: The profile, and its Sunday board
    """
    loaded = load_profile("example")
    return loaded, loaded.board("sunday_sermon")


def test_the_summary_leads_because_that_is_what_a_viewer_sees(profile) -> None:
    """
    YouTube shows the first couple of lines and hides the rest behind "more".

    :param profile: Fixture supplying the profile and its board
    """
    loaded, board = profile
    description = format_upload_description(sermon(), SUMMARY, loaded, board)

    assert description.startswith(SUMMARY)
    assert "본문: 요한복음 21:15" in description
    assert "설교: 홍길동 목사" in description


def test_without_a_summary_the_description_is_the_metadata_alone(profile) -> None:
    """
    Which is what this project published before summaries existed, unchanged.

    The blank line that would have separated the summary from the metadata goes
    with the slot; the label in front of the verse does not.

    :param profile: Fixture supplying the profile and its board
    """
    loaded, board = profile
    description = format_upload_description(sermon(), "", loaded, board)

    assert description.startswith("본문: 요한복음 21:15"), "the label is content, not separator"
    assert not description.startswith("\n")
    assert SUMMARY not in description


def test_a_recording_missing_its_metadata_carries_no_empty_labels(profile) -> None:
    """
    A record with no verse and no preacher should not publish bare labels.

    :param profile: Fixture supplying the profile and its board
    """
    loaded, board = profile
    description = format_upload_description(sermon(bible_verse="", artist=""), SUMMARY, loaded, board)

    assert "본문:" not in description
    assert "설교:" not in description
    assert description.startswith(SUMMARY)


def test_a_description_too_long_for_youtube_is_refused_here(profile) -> None:
    """
    YouTube refuses the upload outright, which says nothing about which field.

    This is the guard behind the stub marker: a transcript reaching the summary
    slot is tens of thousands of characters, and the error has to name it.

    :param profile: Fixture supplying the profile and its board
    """
    loaded, board = profile
    with pytest.raises(ValueError, match="over YouTube's"):
        format_upload_description(sermon(), "가" * (MAX_DESCRIPTION_LENGTH + 1), loaded, board)


def test_a_description_at_the_limit_is_still_sent(profile) -> None:
    """
    The check is a limit, not a margin.

    :param profile: Fixture supplying the profile and its board
    """
    loaded, board = profile
    template = format_upload_description(sermon(), "", loaded, board)
    room = MAX_DESCRIPTION_LENGTH - len(template) - len("\n\n")
    description = format_upload_description(sermon(), "가" * room, loaded, board)

    assert len(description) == MAX_DESCRIPTION_LENGTH


def test_an_empty_slot_takes_only_its_separator_not_its_label() -> None:
    """
    The rule the title and the description share, stated on its own.

    The title's separators are punctuation and go entirely; the description
    labels its slots, and a label is content that has to survive the separator
    in front of it.
    """
    assert render("{a} | {b}", {"a": "", "b": "둘"}) == "둘"
    assert render("{a}\n\n본문: {b}", {"a": "", "b": "요 21:15"}) == "본문: 요 21:15"
    assert render("{a}\n\n본문: {b}", {"a": "하나", "b": "요 21:15"}) == "하나\n\n본문: 요 21:15"
