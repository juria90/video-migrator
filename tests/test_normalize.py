#!/usr/bin/env python3
"""Tests for metadata normalization and validation."""

import dataclasses

import pytest

from video_migrator.config import load_profile
from video_migrator.metadata.normalize import (
    fix_video_metadata,
    has_preacher_title,
    service_key,
    snap_to_weekday,
    validate_videos,
)
from video_migrator.models import Video


@pytest.fixture
def profile():
    """The shipped church-love profile, which declares both title lists."""
    return load_profile("churchlove")


@pytest.fixture
def strict_profile(profile):
    """
    The same profile with the weekday treated as an invariant.

    :param profile: The shipped profile to derive from
    """
    return dataclasses.replace(profile, max_publish_date_drift=6)


@pytest.mark.parametrize(
    "preacher",
    [
        "홍길동 목사",
        # An associate pastor's title is one word, so ' 목사' alone misses it.
        "김영희 협동목사",
        "홍길동 선교사",
        # A home church may follow the title, with or without a space.
        "홍길동 목사 (예시교회 담임)",
        "김영희 목사(예시교회 담임)",
        # English titles precede the name instead of following it.
        "Rev. John Doe",
        "Pastor John Doe",
        "Dr. Jane Roe",
    ],
)
def test_has_preacher_title_accepts_a_titled_name(profile, preacher) -> None:
    """
    A name carrying a title in either position is not worth warning about.

    :param profile: Site profile supplying the recognized titles
    :param preacher: Preacher name as the site publishes it
    """
    assert has_preacher_title(preacher, profile)


@pytest.mark.parametrize(
    "preacher",
    [
        "Jane Roe",
        "John Doe",
        "예시교회",
        # A parenthetical qualifies a title; it cannot stand in for one.
        "홍길동 (예시교회 담임)",
    ],
)
def test_has_preacher_title_rejects_an_untitled_name(profile, preacher) -> None:
    """
    A name with no title still warns — that is the check's whole point.

    :param profile: Site profile supplying the recognized titles
    :param preacher: Preacher name as the site publishes it
    """
    assert not has_preacher_title(preacher, profile)


def make_video(title: str) -> Video:
    """
    Build a Video carrying just the title under test.

    :param title: Title as scraped
    :return: A Video ready to normalize
    """
    return Video(type="youtube", id="x", url="u", embed_url="e", title=title)


@pytest.mark.parametrize(
    "scraped,expected",
    [
        ("1부예배) 설교 제목 여섯", "(1부예배) 설교 제목 여섯"),
        ("2부예배) An Example Story", "(2부예배) An Example Story"),
        # A title whose parentheses already match is left alone.
        ("설교 제목 둘 (Subtitle)", "설교 제목 둘 (Subtitle)"),
        ("제목 (Title, With Comma)", "제목 (Title, With Comma)"),
        ("설교 제목, 부제", "설교 제목, 부제"),
    ],
)
def test_fix_video_metadata_closes_an_orphaned_paren(profile, scraped, expected) -> None:
    """
    A title opening with a bare ')' gets its missing '(' back.

    :param profile: Site profile supplying the rewrite rules
    :param scraped: Title as scraped
    :param expected: Title after normalization
    """
    videos = [make_video(scraped)]
    fix_video_metadata(videos, profile)
    assert videos[0].title == expected


@pytest.mark.parametrize(
    "scraped_title,scraped_preacher,title,preacher",
    [
        ("설교 제목 하나", "홍길동 선교사_2부설교", "설교 제목 하나 (2부)", "홍길동 선교사"),
        ("설교 제목 둘 (Subtitle)", "홍길동 선교사_1부설교", "설교 제목 둘 (Subtitle) (1부)", "홍길동 선교사"),
        # The separator may be a space, and the part may stand on its own.
        ("설교 제목 셋", "홍길동 목사 1부", "설교 제목 셋 (1부)", "홍길동 목사"),
        # A title already naming the part keeps the one it has.
        ("2부 설교 제목 넷", "김영희 목사 2부", "2부 설교 제목 넷", "김영희 목사"),
        # A name carrying no service part is left completely alone.
        ("설교 제목", "홍길동 목사", "설교 제목", "홍길동 목사"),
    ],
)
def test_fix_video_metadata_moves_a_service_part_off_the_preacher(
    profile, scraped_title, scraped_preacher, title, preacher
) -> None:
    """
    A service part recorded against the preacher belongs in the title.

    :param profile: Site profile supplying the extraction pattern
    :param scraped_title: Title as scraped
    :param scraped_preacher: Preacher as scraped
    :param title: Title once the part has moved across
    :param preacher: Preacher with the part taken off
    """
    video = Video(type="vimeo", id="x", url="u", embed_url="e", title=scraped_title, artist=scraped_preacher)
    fix_video_metadata([video], profile)
    assert (video.title, video.artist) == (title, preacher)


def test_fix_video_metadata_reports_every_field_it_rewrote(profile) -> None:
    """
    A rewrite the caller cannot see is a rewrite nobody can audit.

    :param profile: Site profile supplying the rewrite rules
    """
    video = Video(
        type="vimeo",
        id="x",
        url="u",
        embed_url="e",
        title="예배) 설교 제목",
        artist="홍길동 선교사_2부설교",
        publish_date="2016-10-17",
    )
    changes = fix_video_metadata([video], profile, profile.board("sunday_sermon"))

    # The title is reported once, as its net change: the bracket rule and the
    # service part both rewrote it.
    assert {(c.field, c.before, c.after) for c in changes} == {
        ("title", "예배) 설교 제목", "(예배) 설교 제목 (2부)"),
        ("preacher", "홍길동 선교사_2부설교", "홍길동 선교사"),
        ("date", "2016-10-17", "2016-10-16"),
    }
    assert all(c.index == 1 for c in changes), "the index must match validate_videos' numbering"


def test_fix_video_metadata_reports_nothing_when_it_changes_nothing(profile) -> None:
    """
    Clean input produces an empty report rather than a noisy one.

    :param profile: Site profile supplying the rewrite rules
    """
    video = Video(
        type="vimeo",
        id="x",
        url="u",
        embed_url="e",
        title="설교 제목",
        artist="홍길동 목사",
        publish_date="2016-10-16",
    )
    assert fix_video_metadata([video], profile, profile.board("sunday_sermon")) == []


SUNDAY = 6
WEDNESDAY = 2


@pytest.mark.parametrize(
    "scraped,weekday,expected",
    [
        # The site stamped these the day after the service for years.
        ("2016-10-17", SUNDAY, "2016-10-16"),
        ("2015-07-27", SUNDAY, "2015-07-26"),
        # Friday services drift onto Saturday the same way.
        ("2016-10-16", SUNDAY, "2016-10-16"),
        ("2019-09-11", WEDNESDAY, "2019-09-11"),
        ("2019-09-12", WEDNESDAY, "2019-09-11"),
        # Two or more days off is not a late upload; leave it for a human.
        ("2016-11-24", SUNDAY, "2016-11-24"),
        ("2019-07-16", SUNDAY, "2019-07-16"),
        # A date *before* the service day is never pulled back a whole week.
        ("2019-09-10", WEDNESDAY, "2019-09-10"),
        # Anything unparseable passes through untouched.
        ("", SUNDAY, ""),
        ("2016-13-45", SUNDAY, "2016-13-45"),
    ],
)
def test_snap_to_weekday(scraped, weekday, expected) -> None:
    """
    Only a date one day past the service is pulled back onto it.

    :param scraped: Publish date as the site records it
    :param weekday: The board's meeting day, as a weekday ordinal
    :param expected: The date after correction
    """
    assert snap_to_weekday(scraped, weekday) == expected


def test_fix_video_metadata_moves_the_year_with_the_date(profile) -> None:
    """A Monday 1 January belongs to the year before, and `year` is written to the file."""
    board = profile.board("sunday_sermon")
    video = Video(type="vimeo", id="x", url="u", embed_url="e", title="t", publish_date="2018-01-01")
    assert video.year == "2018"

    fix_video_metadata([video], profile, board)

    assert video.publish_date == "2017-12-31"
    assert video.year == "2017"


def dated(title: str, publish_date: str) -> Video:
    """
    Build a Video carrying just a title and a publish date.

    :param title: Title, used to identify the video in assertions
    :param publish_date: Date as scraped
    :return: A Video ready to normalize
    """
    return Video(type="vimeo", id=title, url="u", embed_url="e", title=title, publish_date=publish_date)


def test_correct_publish_dates_moves_a_two_service_sunday_as_a_pair(profile) -> None:
    """
    Both halves of a two-service Sunday drifted together, so both are corrected.

    :param profile: Site profile supplying the correction policy
    """
    videos = [dated("1부", "2016-10-17"), dated("2부", "2016-10-17")]
    fix_video_metadata(videos, profile, profile.board("sunday_sermon"))
    assert [v.publish_date for v in videos] == ["2016-10-16", "2016-10-16"]


def test_correct_publish_dates_refuses_a_guess_onto_an_occupied_sunday(strict_profile) -> None:
    """
    A big correction contradicted by the row already holding that date is not made.

    :param strict_profile: Profile treating the board's weekday as an invariant
    """
    held = dated("already there", "2019-07-14")
    guess = dated("off by two days", "2019-07-16")
    fix_video_metadata([held, guess], strict_profile, strict_profile.board("sunday_sermon"))

    assert held.publish_date == "2019-07-14"
    assert guess.publish_date == "2019-07-16", "a guess that collides must be left for a human"


def test_correct_publish_dates_allows_a_second_service_beside_its_first(strict_profile) -> None:
    """
    A day holds several services, so a second one does not conflict with the first.

    :param strict_profile: Profile treating the board's weekday as an invariant
    """
    first = dated("감사 1부예배", "2016-11-20")
    second = dated("감사 2부예배", "2016-11-24")
    fix_video_metadata([first, second], strict_profile, strict_profile.board("sunday_sermon"))

    assert first.publish_date == "2016-11-20"
    assert second.publish_date == "2016-11-20", "a 2부 belongs beside its 1부, not left behind"


def test_correct_publish_dates_refuses_two_guesses_competing_for_one_sunday(strict_profile) -> None:
    """
    Two rows cannot both be the same service, so neither guess wins by position.

    :param strict_profile: Profile treating the board's weekday as an invariant
    """
    videos = [dated("first", "2014-10-23"), dated("second", "2014-10-23")]
    fix_video_metadata(videos, strict_profile, strict_profile.board("sunday_sermon"))
    assert [v.publish_date for v in videos] == ["2014-10-23", "2014-10-23"]


def test_correct_publish_dates_applies_a_guess_onto_a_free_sunday(strict_profile) -> None:
    """
    Nothing contradicts a big correction landing on an unused date, so it is made.

    :param strict_profile: Profile treating the board's weekday as an invariant
    """
    video = dated("four days late", "2011-08-18")
    fix_video_metadata([video], strict_profile, strict_profile.board("sunday_sermon"))
    assert video.publish_date == "2011-08-14"


@pytest.mark.parametrize(
    "title,expected",
    [
        # The site writes the service part in whatever style the typist chose,
        # so the marker is matched by containment rather than by position.
        ("1부 설교 제목", {"1부"}),
        ("설교 제목 (1부)", {"1부"}),
        ("설교 제목 1부", {"1부"}),
        ("(1부) 설교 제목", {"1부"}),
        ("(1부예배) 설교 제목", {"1부"}),
        ("설교 제목 (2부)", {"2부"}),
        # Run into the following word, and still the second service.
        ("설교 제목 (2부설교)", {"2부"}),
        # One record covering both services is neither one alone.
        ("1부 설교 제목 하나 / 2부 설교 제목 둘", {"1부", "2부"}),
        ("설교 제목 1부 영상", {"1부", "영상"}),
        # A title naming no service is itself a distinguishing answer.
        ("설교 제목", set()),
    ],
)
def test_service_key_reads_every_style_the_site_uses(profile, title, expected) -> None:
    """
    A service marker counts wherever it sits in the title.

    :param profile: Site profile supplying the markers
    :param title: Title as the site writes it
    :param expected: The markers it should be understood to carry
    """
    assert service_key(title, profile.service_parts) == expected


def test_validate_videos_flags_one_video_filed_twice(profile, capsys) -> None:
    """
    The same recording under two records is a duplicate the site has to fix.

    :param profile: Site profile supplying the validation vocabulary
    :param capsys: Pytest fixture capturing the printed warnings
    """

    def video(title: str, video_id: str) -> Video:
        return Video(
            type="vimeo",
            id=video_id,
            url=f"https://vimeo.com/{video_id}",
            embed_url="e",
            title=title,
            publish_date="2018-03-25",
            artist="홍길동 목사",
        )

    videos = [
        video("(1부예배) 설교 제목", "900000000001"),
        video("(1부예배) 설교 제목", "900000000001"),
        # Same title and date but a different recording: a two-service Sunday
        # preaches one sermon twice, and neither record is a duplicate.
        video("설교 제목 둘", "900000000002"),
        video("설교 제목 둘", "900000000003"),
    ]
    validate_videos(videos, profile, profile.board("sunday_sermon"))

    warning = capsys.readouterr().out
    assert "filed under more than one record" in warning
    assert "https://vimeo.com/900000000001" in warning
    assert "https://vimeo.com/900000000002" not in warning, "two recordings of one sermon are not duplicates"


def test_validate_videos_is_quiet_on_clean_input(profile, capsys) -> None:
    """
    A board with nothing wrong produces no warnings at all.

    :param profile: Site profile supplying the validation vocabulary
    :param capsys: Pytest fixture capturing the printed warnings
    """
    videos = [
        Video(
            type="vimeo",
            id="900000000001",
            url="https://vimeo.com/900000000001",
            embed_url="e",
            title="설교 제목",
            publish_date="2016-10-16",
            artist="홍길동 목사",
        )
    ]
    validate_videos(videos, profile, profile.board("sunday_sermon"))
    assert capsys.readouterr().out == ""


def test_fix_video_metadata_leaves_a_dateless_board_alone(profile) -> None:
    """A board that meets every day declares no weekday, so its dates are never moved."""
    board = profile.board("early_morning_prayer")
    assert board.weekday_index is None

    video = Video(type="vimeo", id="x", url="u", embed_url="e", title="t", publish_date="2016-10-17")
    fix_video_metadata([video], profile, board)

    assert video.publish_date == "2016-10-17"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
