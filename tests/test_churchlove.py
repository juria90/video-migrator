#!/usr/bin/env python3
"""Tests for the church-love.net VOD scraper and the platform recognizer."""

import pytest

from video_migrator.config import load_profile
from video_migrator.scrapers import scraper_for
from video_migrator.scrapers.churchlove import (
    ChurchLoveScraper,
    Entry,
    _parse_vod_info,
    board_url,
    extract_entries,
)
from video_migrator.scrapers.platforms import identify

# Two tiles as the CMS renders them, trimmed to the parts the scraper reads:
# the onclick URLs carrying num/vodType, and the title in <strong>.
LISTING_HTML = """
<div class="jmBroad_list">
  <div class="jmBroad_box">
    <ul><div>
      <div style="background-image: url(/user/saveDir/vod/1234_thum.jpg);">
        <div class="jmBroad_boxCover"><p>
          <a href="#" onclick="document.location.href='/main/sub.html?num=1234&pageCode=9&category=&srcYear=&keyfield=&key=&Mode=view&vodType=1&page=1';" title="영상보기"><img src="x.png" /></a>
        </p></div>
      </div>
      <p>
        <strong onclick="javascript:document.location.href='/main/sub.html?num=1234&pageCode=9&Mode=view&vodType=1&page=1';">설교 제목, 부제</strong>
        본문 : 요한복음 21:15-23<br>설교자 : 홍길동 목사<br>날짜 : 2026-08-02
      </p>
    </div></ul>
  </div>
  <div class="jmBroad_box">
    <ul><div><p>
      <strong onclick="javascript:document.location.href='/main/sub.html?num=5678&pageCode=9&Mode=view&vodType=1&page=1';">설교 제목 다섯</strong>
    </p></div></ul>
  </div>
</div>
"""

VOD_INFO_XML = """<?xml version="1.0" encoding="utf-8" ?>
<result>
    <data>
        <code>vodInfo</code>
        <vodInfo>
            <num>1234</num>
            <vodFile><![CDATA[https://www.youtube.com/embed/aBcDeFgHiJk]]></vodFile>
            <playerType><![CDATA[iframe]]></playerType>
            <subject><![CDATA[설교 제목, 부제]]></subject>
            <word><![CDATA[요한복음 21:15-23]]></word>
            <preacher><![CDATA[홍길동 목사]]></preacher>
            <date><![CDATA[2026-08-02]]></date>
        </vodInfo>
    </data>
</result>
"""


def test_extract_entries_reads_every_tile() -> None:
    """Each tile yields its recording id, its board's media type, and its title."""
    assert extract_entries(LISTING_HTML) == [
        Entry(num="1234", vod_type="1", label="설교 제목, 부제"),
        Entry(num="5678", vod_type="1", label="설교 제목 다섯"),
    ]


def test_extract_entries_on_a_page_past_the_end() -> None:
    """A page beyond the last one has no tiles, which is how the walk terminates."""
    assert extract_entries("<div class='jmBroad_list'></div>") == []


def test_parse_vod_info_unwraps_cdata() -> None:
    """The endpoint wraps every field in CDATA; the parser hands back plain text."""
    info = _parse_vod_info(VOD_INFO_XML)
    assert info["vodFile"] == "https://www.youtube.com/embed/aBcDeFgHiJk"
    assert info["preacher"] == "홍길동 목사"
    assert info["date"] == "2026-08-02"


def test_parse_vod_info_on_a_deleted_recording() -> None:
    """A deleted recording answers with a JavaScript alert rather than XML."""
    assert _parse_vod_info("<script> window.alert('\\n삭제된 데이터입니다.'); </script>") is None


def test_resolve_builds_a_video_from_the_vod_record(monkeypatch) -> None:
    """
    Metadata comes from the VOD endpoint, not from the tile.

    :param monkeypatch: Pytest fixture used to stub out the network call
    """
    scraper = ChurchLoveScraper("https://example.org/main/sub.html?pageCode=9", genre="Sermon", language="kor")
    monkeypatch.setattr(ChurchLoveScraper, "fetch_vod_info", lambda self, entry: _parse_vod_info(VOD_INFO_XML))

    video = scraper.resolve(Entry(num="1234", vod_type="1", label="설교 제목, 부제"))

    assert video.type == "youtube"
    assert video.id == "aBcDeFgHiJk"
    assert video.url == "https://www.youtube.com/watch?v=aBcDeFgHiJk"
    assert video.embed_url == "https://www.youtube.com/embed/aBcDeFgHiJk"
    assert video.title == "설교 제목, 부제"
    assert video.bible_verse == "요한복음 21:15-23"
    assert video.artist == "홍길동 목사"
    assert video.publish_date == "2026-08-02"
    assert video.year == "2026"
    assert video.genre == "Sermon"


@pytest.mark.parametrize(
    "vod_file",
    [
        # The site stores some records with the id missing from the player URL.
        "https://player.vimeo.com/video/",
        "",
        "https://vod46.example.co.kr/sermon.mp4",
    ],
)
def test_resolve_skips_a_recording_it_cannot_place(monkeypatch, vod_file) -> None:
    """
    A record whose player URL names no video this package can fetch is dropped.

    :param monkeypatch: Pytest fixture used to stub out the network call
    :param vod_file: Player URL as the endpoint reports it
    """
    scraper = ChurchLoveScraper("https://example.org/main/sub.html?pageCode=9", genre="Sermon")
    monkeypatch.setattr(ChurchLoveScraper, "fetch_vod_info", lambda self, entry: {"vodFile": vod_file})

    assert scraper.resolve(Entry(num="1", vod_type="1", label="x")) is None


def test_board_url_uses_the_boards_page_code() -> None:
    """This CMS addresses a board by page code, which the profile supplies."""
    assert board_url("choir_praise", "churchlove") == "https://example.org/main/sub.html?pageCode=17"


def test_board_url_without_a_page_code_says_so() -> None:
    """An unknown board has no page code, and a bare URL would silently scrape the wrong thing."""
    with pytest.raises(ValueError, match="page_code"):
        board_url("no_such_board", "churchlove")


def test_scraper_for_resolves_the_profiles_declared_software() -> None:
    """A profile naming its software in `site.scraper` resolves without being registered."""
    assert scraper_for(load_profile("churchlove")) is ChurchLoveScraper


@pytest.mark.parametrize(
    "url,expected",
    [
        (
            "https://www.youtube.com/embed/aBcDeFgHiJk",
            ("youtube", "aBcDeFgHiJk", "https://www.youtube.com/watch?v=aBcDeFgHiJk"),
        ),
        (
            "https://youtu.be/aBcDeFgHiJk",
            ("youtube", "aBcDeFgHiJk", "https://www.youtube.com/watch?v=aBcDeFgHiJk"),
        ),
        (
            "https://player.vimeo.com/video/999999999999",
            ("vimeo", "999999999999", "https://vimeo.com/999999999999"),
        ),
        (
            "https://vimeo.com/999999999999",
            ("vimeo", "999999999999", "https://vimeo.com/999999999999"),
        ),
        # The site stores some player URLs mangled; the id is still recoverable.
        (
            "https://vod46.example.co.kr//ttps://player.vimeo.com/video/900000000002",
            ("vimeo", "900000000002", "https://vimeo.com/900000000002"),
        ),
        (
            "https://soundcloud.com/example/sermon-01",
            ("soundcloud", "example_sermon-01", "https://soundcloud.com/example/sermon-01"),
        ),
    ],
)
def test_identify_recognizes_the_hosting_platform(url, expected) -> None:
    """
    Embed and watch URLs alike resolve to platform, id and canonical page.

    :param url: URL as a scraper found it
    :param expected: The (platform, id, canonical_url) triple it stands for
    """
    assert identify(url) == expected


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
