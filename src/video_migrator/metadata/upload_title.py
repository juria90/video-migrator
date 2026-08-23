#!/usr/bin/env python3
"""
Build the title an upload is published under.

A destination platform stamps its own upload date on a video and will not accept
the date the recording actually belongs to, so on a back-catalogue migration the
title is the only place that date survives where a viewer will see it. It
therefore has to be in the title somewhere, and the rest is assembled around it
from the profile's template.
"""

import datetime
import re
from string import Formatter

from ..config import Board, Profile, load_profile
from ..corrections.verses import abbreviate
from ..models import Video


def strip_service_part(title: str, part: str) -> str:
    """
    Take the service part back out of a title that is about to name it separately.

    The part stays in the title through normalization, because that is where
    :func:`~video_migrator.metadata.normalize.service_key` looks when telling a
    day's services apart. Once it has its own segment of the upload title, a copy
    left in the title would say it twice.

    Only the forms a part is actually written in are removed — parenthesized, as
    :func:`~video_migrator.metadata.normalize.take_service_part` writes it, or
    bare with whatever separator followed it. Anything else is left alone rather
    than guessed at.

    :param title: Title, already normalized
    :param part: The marker to remove, empty to leave the title untouched
    :return: The title without the part

    >>> strip_service_part("설교 제목 (2부)", "2부")
    '설교 제목'
    >>> strip_service_part("2부 - 설교 제목", "2부")
    '설교 제목'
    >>> strip_service_part("(1부예배) 설교 제목", "1부")
    '설교 제목'
    >>> strip_service_part("설교 제목", "")
    '설교 제목'
    """
    if not part:
        return title

    marker = rf"{re.escape(part)}(?:예배)?"
    pattern = re.compile(rf"\s*(?:[(\[]\s*{marker}\s*[)\]]|{marker}\s*[-–—:]?)\s*")
    return pattern.sub(" ", title, count=1).strip()


def format_date(publish_date: str, template: str) -> str:
    """
    Write a publish date the way the profile asks for it.

    :param publish_date: Date as normalized, ``YYYY-MM-DD``
    :param template: Format over ``{year}``, ``{short_year}``, ``{month}`` and
        ``{day}``, each an integer so that ``{month:02d}`` pads and ``{month}``
        does not
    :return: The formatted date, or the input unchanged where it is not a date

    >>> format_date("2026-08-02", "{year}.{month}.{day}")
    '2026.8.2'
    >>> format_date("2026-08-02", "{short_year}.{month:02d}.{day:02d}")
    '26.08.02'
    >>> format_date("sometime in 2026", "{year}.{month}.{day}")
    'sometime in 2026'
    """
    try:
        date = datetime.date.fromisoformat(publish_date)
    except ValueError:
        return publish_date

    return template.format(year=date.year, short_year=date.year % 100, month=date.month, day=date.day)


def _render(template: str, values: dict[str, str]) -> str:
    """
    Fill a template, dropping the separator in front of every empty slot.

    A record with no preacher or no service should not publish under a title
    carrying the punctuation that would have set one off. The literal before a
    slot belongs to that slot and goes with it; the literal opening the template
    is a prefix on the whole title and only survives while its own slot does.

    :param template: Format string over the keys of ``values``
    :param values: Slot name -> its rendered value, empty where it has none
    :return: The filled template, stripped
    """
    kept: list[tuple[str, str]] = []
    trailing = ""
    for index, (literal, field, _spec, _conversion) in enumerate(Formatter().parse(template)):
        if field is None:
            trailing = literal
            continue
        value = values.get(field, "").strip()
        if value:
            # The first slot to survive opens the title, so it keeps a literal
            # only where that literal opened the template too.
            kept.append(("" if not kept and index > 0 else literal, value))

    if not kept:
        return ""
    return "".join(literal + value for literal, value in kept).strip() + trailing.rstrip()


def format_upload_title(video: Video, profile: Profile | None = None, board: Board | None = None) -> str:
    """
    Build the title a video is uploaded under.

    :param video: The video, already normalized
    :param profile: Site profile supplying the template; defaults to the profile
        named by :data:`~video_migrator.config.DEFAULT_PROFILE`
    :param board: The board it came from, naming the service. Without one the
        service segment is left out
    :return: The formatted title

    >>> profile = load_profile()
    >>> video = Video(type="video", id="1", url="", embed_url="",
    ...               title="설교 제목 (2부)", publish_date="2026-08-02",
    ...               artist="홍길동 목사", service_part="2부")
    >>> format_upload_title(video, profile, profile.board("sunday_sermon"))
    '2026.8.2 | 주일 2부 예배 | 설교 제목 | 홍길동 목사'

    A record naming no service falls back to the board's plain name:

    >>> plain = Video(type="video", id="2", url="", embed_url="",
    ...               title="설교 제목", publish_date="2026-08-02", artist="김영희 목사")
    >>> format_upload_title(plain, profile, profile.board("sunday_sermon"))
    '2026.8.2 | 주일예배 | 설교 제목 | 김영희 목사'

    An empty slot takes its separator with it:

    >>> anonymous = Video(type="video", id="3", url="", embed_url="",
    ...                   title="봉헌송 제목", publish_date="2026-08-02")
    >>> format_upload_title(anonymous, profile, profile.board("votive_song"))
    '2026.8.2 | 봉헌송 | 봉헌송 제목'
    """
    profile = profile or load_profile()
    title = strip_service_part(video.title, video.service_part)
    values = {
        "date": format_date(video.publish_date, profile.upload_date_format),
        "service": board.service_label(video.service_part) if board else "",
        "title": title,
        # Quoted here rather than in the template, because a closing quote
        # written there would belong to whatever slot came next and would go
        # missing with it — leaving a title opened and never closed.
        "quoted_title": f'"{title}"' if title else "",
        "artist": video.artist,
        "bible_verse": video.bible_verse,
        "short_verse": abbreviate(video.bible_verse),
        "church": profile.church,
    }
    return _render(profile.upload_title_template, values)
