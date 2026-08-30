#!/usr/bin/env python3
"""Tests for the title an upload is published under."""

import dataclasses

import pytest

from video_migrator.cli import keep_preachers
from video_migrator.config import load_profile
from video_migrator.metadata.normalize import fix_video_metadata, has_preacher_title
from video_migrator.metadata.upload_title import format_date, format_upload_title, strip_service_part
from video_migrator.models import Video


@pytest.fixture
def profile():
    """The shipped church-love profile, which declares an upload template."""
    return load_profile("churchlove")


def make_video(title: str, **overrides) -> Video:
    """
    Build a video carrying only the fields a title is assembled from.

    :param title: Title, as it stands after normalization
    :param overrides: Any other :class:`~video_migrator.models.Video` field
    :return: The video
    """
    fields = {
        "type": "video",
        "id": "1",
        "url": "",
        "embed_url": "",
        "title": title,
        "publish_date": "2026-08-02",
        "artist": "홍길동 목사",
    }
    return Video(**{**fields, **overrides})


@pytest.mark.parametrize(
    ("date_format", "expected"),
    [
        # Korean writes a date year-first, and unpadded is its ordinary form.
        ("{year}.{month}.{day}", "2026.8.2"),
        ("{year}.{month:02d}.{day:02d}", "2026.08.02"),
        ("{short_year}.{month:02d}.{day:02d}", "26.08.02"),
        ("{year}-{month:02d}-{day:02d}", "2026-08-02"),
    ],
)
def test_format_date_writes_the_form_the_profile_asks_for(date_format, expected) -> None:
    """
    The date slot is a str.format template, so padding is the profile's choice.

    :param date_format: Template the profile declares
    :param expected: How the date should come out
    """
    assert format_date("2026-08-02", date_format) == expected


def test_format_date_leaves_a_date_it_cannot_read_alone() -> None:
    """A date the site never wrote properly is passed through, not dropped."""
    assert format_date("sometime in 2026", "{year}.{month}.{day}") == "sometime in 2026"


@pytest.mark.parametrize(
    ("title", "part", "expected"),
    [
        # The form take_service_part writes.
        ("설교 제목 (2부)", "2부", "설교 제목"),
        # The forms the site itself uses.
        ("2부 - 설교 제목", "2부", "설교 제목"),
        ("(1부예배) 설교 제목", "1부", "설교 제목"),
        ("1부 설교 제목", "1부", "설교 제목"),
        ("설교 제목 (2부 연합)", "2부 연합", "설교 제목"),
        # A record naming no service keeps every word it has.
        ("설교 제목", "", "설교 제목"),
    ],
)
def test_strip_service_part_removes_only_the_part(title, part, expected) -> None:
    """
    The part is taken out of the title exactly once it has its own segment.

    :param title: Title as normalized, still carrying the part
    :param part: The marker about to be named separately
    :param expected: The title without it
    """
    assert strip_service_part(title, part) == expected


def test_strip_service_part_leaves_a_title_that_never_named_it() -> None:
    """
    A part recorded somewhere other than the title takes nothing out of it.

    Saying the service twice would be a cosmetic fault; cutting a word out of a
    title that never carried the part would not be, so nothing is removed on
    spec.
    """
    assert strip_service_part("설교 제목 하나", "3부") == "설교 제목 하나"


def test_title_names_the_service_the_record_belongs_to(profile) -> None:
    """
    A record naming which service it is gets the board's qualified name.

    :param profile: Site profile supplying the template and the board
    """
    video = make_video("설교 제목 (2부)", service_part="2부")
    title = format_upload_title(video, profile, profile.board("sunday_sermon"))
    assert title == "2026.8.2 | 주일 2부 예배 | 설교 제목 | 홍길동 목사"


def test_title_falls_back_to_the_plain_service_name(profile) -> None:
    """
    A record naming no service still says which board it came from.

    :param profile: Site profile supplying the template and the board
    """
    video = make_video("설교 제목")
    title = format_upload_title(video, profile, profile.board("sunday_sermon"))
    assert title == "2026.8.2 | 주일예배 | 설교 제목 | 홍길동 목사"


def test_a_board_holding_one_service_ignores_a_part(profile) -> None:
    """
    Without a service_template there is nothing for a part to qualify.

    :param profile: Site profile supplying the template and the board
    """
    video = make_video("설교 제목 (2부)", service_part="2부")
    title = format_upload_title(video, profile, profile.board("wednesday_prayer"))
    assert title == "2026.8.2 | 수요기도회 | 설교 제목 | 홍길동 목사"


def test_an_empty_slot_takes_its_separator_with_it(profile) -> None:
    """
    A praise board credits no preacher, and must not publish a dangling ' | '.

    :param profile: Site profile supplying the template and the board
    """
    video = make_video("찬양 제목", artist="")
    title = format_upload_title(video, profile, profile.board("votive_song"))
    assert title == "2026.8.2 | 봉헌송 | 찬양 제목"


def test_a_title_without_a_board_leaves_the_service_out(profile) -> None:
    """
    Nothing names the service when the board is unknown, so nothing is invented.

    :param profile: Site profile supplying the template
    """
    video = make_video("설교 제목")
    assert format_upload_title(video, profile) == "2026.8.2 | 설교 제목 | 홍길동 목사"


def test_the_date_leads_even_when_it_is_the_only_field(profile) -> None:
    """
    A record with nothing but a date still produces a usable title.

    :param profile: Site profile supplying the template
    """
    video = make_video("", artist="")
    assert format_upload_title(video, profile) == "2026.8.2"


def test_a_custom_template_reorders_the_segments(profile) -> None:
    """
    Which fields a title carries, and in what order, is a profile edit.

    :param profile: Site profile to derive a variant from
    """
    reordered = dataclasses.replace(
        profile, upload_title_template="{date} | {title} | {service} | {artist} | {bible_verse}"
    )
    video = make_video("설교 제목", bible_verse="요한복음 21:15-23")
    title = format_upload_title(video, reordered, reordered.board("sunday_sermon"))
    assert title == "2026.8.2 | 설교 제목 | 주일예배 | 홍길동 목사 | 요한복음 21:15-23"


def test_normalization_records_the_part_the_title_names(profile) -> None:
    """
    The part reaches the formatter without the caller having to find it.

    :param profile: Site profile supplying the normalization vocabulary
    """
    videos = [
        make_video("설교 제목 하나", artist="홍길동 목사_2부설교"),
        make_video("설교 제목 둘 (1부)"),
        make_video("설교 제목 셋 영상"),
        make_video("설교 제목 넷"),
    ]
    fix_video_metadata(videos, profile)
    assert [video.service_part for video in videos] == ["2부", "1부", "", ""]


def test_a_praise_board_accepts_a_performer_rather_than_a_preacher(profile) -> None:
    """
    The three praise boards credit whoever performed, who holds no title.

    :param profile: Site profile supplying the recognized titles
    """
    choir = profile.board("choir_praise")
    assert has_preacher_title("성가대", profile, choir)
    # The same name on a sermon board is still worth a warning.
    assert not has_preacher_title("성가대", profile, profile.board("sunday_sermon"))


def test_a_board_declaring_no_override_inherits_the_profile(profile) -> None:
    """
    Only a board that says otherwise deviates from the profile's vocabulary.

    :param profile: Site profile supplying the recognized titles
    """
    sermon = profile.board("sunday_sermon")
    assert profile.preacher_titles(sermon) == profile.valid_preacher_titles
    assert profile.preacher_prefixes(sermon) == profile.valid_preacher_prefixes


@pytest.fixture
def bracketed(profile):
    """
    A profile titling uploads the way a real site's archive does.

    Bracketed church and preacher, a YYMMDD date, the title in quotes and the
    verse abbreviated — the shipped profile stays generic, so the shape a real
    site publishes under is declared here instead.

    :param profile: The shipped church-love profile to vary
    :return: The profile with that template
    """
    return dataclasses.replace(
        profile,
        church="예시교회",
        upload_title_template="[{church} - {artist}] {date} {service} | {quoted_title} {short_verse}",
        upload_date_format="{short_year:02d}{month:02d}{day:02d}",
    )


def test_the_bracketed_template_assembles_every_slot(bracketed) -> None:
    """
    Church, preacher, date, service, quoted title and short verse, in order.

    :param bracketed: Site profile supplying the template
    """
    video = make_video("설교 제목 (2부)", bible_verse="요한복음 21:15-23", service_part="2부")
    assert (format_upload_title(video, bracketed, bracketed.board("sunday_sermon"))
            == '[예시교회 - 홍길동 목사] 260802 주일 2부 예배 | "설교 제목" 요 21:15-23')


def test_a_record_naming_no_service_takes_the_plain_board_name(bracketed) -> None:
    """
    Without a part there is no ``N부`` to announce, so the board's name stands.

    :param bracketed: Site profile supplying the template
    """
    video = make_video("설교 제목", bible_verse="요한복음 21:15-23")
    assert (format_upload_title(video, bracketed, bracketed.board("sunday_sermon"))
            == '[예시교회 - 홍길동 목사] 260802 주일예배 | "설교 제목" 요 21:15-23')


def test_a_title_stays_closed_when_the_verse_is_missing(bracketed) -> None:
    """
    The quotes belong to the title, not to the slot that happens to follow it.

    Written as literals in the template, the closing quote would belong to
    ``{short_verse}`` and go missing with it, publishing a title that opens a
    quote and never closes it.

    :param bracketed: Site profile supplying the template
    """
    video = make_video("설교 제목")
    assert (format_upload_title(video, bracketed, bracketed.board("sunday_sermon"))
            == '[예시교회 - 홍길동 목사] 260802 주일예배 | "설교 제목"')


def test_a_bracket_closes_even_without_a_preacher(bracketed) -> None:
    """
    A missing preacher takes its separator, leaving the brackets balanced.

    :param bracketed: Site profile supplying the template
    """
    video = make_video("설교 제목", artist="", bible_verse="창세기 1:1")
    assert (format_upload_title(video, bracketed, bracketed.board("sunday_sermon"))
            == '[예시교회] 260802 주일예배 | "설교 제목" 창 1:1')


def test_a_verse_the_canon_does_not_spell_is_published_as_it_stands(bracketed) -> None:
    """
    An English book name is left alone rather than guessed at.

    :param bracketed: Site profile supplying the template
    """
    video = make_video("설교 제목", bible_verse="John 12:12-16")
    assert format_upload_title(video, bracketed, bracketed.board("sunday_sermon")).endswith(
        '| "설교 제목" John 12:12-16')


def test_no_preacher_given_keeps_the_whole_board() -> None:
    """Without ``--preacher`` a scrape carries everyone it found."""
    videos = [make_video("설교 제목 하나"), make_video("설교 제목 둘", artist="김영희 목사")]
    assert keep_preachers(videos, None) == videos
    assert keep_preachers(videos, []) == videos


def test_a_named_preacher_narrows_the_scrape() -> None:
    """A channel belonging to one person carries only that person's recordings."""
    videos = [make_video("설교 제목 하나"), make_video("설교 제목 둘", artist="김영희 목사")]
    assert [video.artist for video in keep_preachers(videos, ["홍길동 목사"])] == ["홍길동 목사"]


def test_a_preacher_credited_two_ways_is_named_twice() -> None:
    """
    One person spelled differently on differently-languaged services is one person.

    The filter matches exactly rather than guessing that two spellings are the
    same name, so both have to be given — and giving both keeps both services.
    """
    videos = [make_video("설교 제목 하나"),
              make_video("설교 제목 둘", artist="Rev. John Doe"),
              make_video("설교 제목 셋", artist="김영희 목사")]
    kept = keep_preachers(videos, ["홍길동 목사", "Rev. John Doe"])
    assert [video.artist for video in kept] == ["홍길동 목사", "Rev. John Doe"]


def test_a_preacher_nobody_is_credited_to_warns_rather_than_passing_silently(capsys) -> None:
    """
    An empty result is more often a typo in the name than a board without them.

    :param capsys: Fixture capturing what the run reported
    """
    assert keep_preachers([make_video("설교 제목")], ["김영희 목사"]) == []
    assert "nothing on this board is credited to 김영희 목사" in capsys.readouterr().err
