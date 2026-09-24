#!/usr/bin/env python3
"""
Build the description an upload is published under.

The counterpart of :mod:`~video_migrator.metadata.upload_title`, over the same
:func:`~video_migrator.metadata.render.render` rule: a slot with nothing in it
takes its separator with it. The summary leads, because the title already
carries the date, the preacher and the verse, and YouTube's collapsed view shows
only the first couple of lines.

The summary is the one slot that may be absent for a reason other than missing
metadata — it is absent whenever no summary has been written yet, which is the
normal state while summarization is stubbed. The description then falls back to
exactly the metadata-only text this project published before summaries existed.
"""

from ..config import Board, Profile, load_profile
from ..models import Video
from .render import render
from .upload_title import format_date

#: The longest description YouTube accepts. A longer one is refused outright, so
#: it is caught here where the reason is visible — and where the offending piece
#: is still identifiable as the summary rather than as "the upload".
MAX_DESCRIPTION_LENGTH = 5000


def format_upload_description(video: Video, summary: str = "", profile: Profile | None = None,
                              board: Board | None = None) -> str:
    """
    Build the description a video is uploaded under.

    :param video: The video, already normalized
    :param summary: The sermon's summary, empty where none has been written.
        Comes from the summary file via
        :func:`~video_migrator.metadata.summarize.read_summary`, which answers
        empty for a file that is still a stub
    :param profile: Site profile supplying the template; defaults to the profile
        named by :data:`~video_migrator.config.DEFAULT_PROFILE`
    :param board: The board it came from, naming the service. Without one the
        service segment is left out
    :return: The formatted description
    :raises ValueError: If the result is longer than YouTube will accept

    >>> profile = load_profile()
    >>> video = Video(type="video", id="1", url="", embed_url="",
    ...               title="설교 제목", publish_date="2026-08-02",
    ...               artist="홍길동 목사", bible_verse="요한복음 21:15")
    >>> board = profile.board("sunday_sermon")
    >>> print(format_upload_description(video, "고친 설교 본문 요한복음 다섯", profile, board))
    고친 설교 본문 요한복음 다섯
    <BLANKLINE>
    본문: 요한복음 21:15
    설교: 홍길동 목사
    예배: 2026.8.2 주일예배
    예시교회

    With no summary written yet, the description is the metadata alone — and the
    blank line that would have separated them goes with the slot:

    >>> print(format_upload_description(video, "", profile, board))
    본문: 요한복음 21:15
    설교: 홍길동 목사
    예배: 2026.8.2 주일예배
    예시교회
    """
    profile = profile or load_profile()
    values = {
        "summary": summary,
        "title": video.title,
        "date": format_date(video.publish_date, profile.upload_date_format),
        "service": board.service_label(video.service_part) if board else "",
        "artist": video.artist,
        "verse": video.bible_verse,
        "church": profile.church,
    }
    description = render(profile.upload_description_template, values)
    if len(description) > MAX_DESCRIPTION_LENGTH:
        raise ValueError(
            f"description is {len(description)} characters, over YouTube's "
            f"{MAX_DESCRIPTION_LENGTH}; the summary is {len(summary)} of them"
        )
    return description
