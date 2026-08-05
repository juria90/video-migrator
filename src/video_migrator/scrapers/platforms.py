#!/usr/bin/env python3
"""
The video hosting platforms a scraped link can point at, and how to read one.

Every scraper in this package ends up doing the same thing with a URL it found:
work out which platform hosts it, pull the id out, and rebuild the canonical
page URL for it. That mapping depends on the platform, not on the CMS the link
was scraped from, so it lives here and not in the individual scraper modules.
"""

import hashlib
import re

from ..models import Video

#: Display names for the platforms a scraped video can live on. Unlike the board
#: list this is not configuration: each label corresponds to a platform this
#: package can actually resolve, so adding one here would produce a label, not a
#: capability.
PLATFORM_LABELS = {
    "vimeo": "Vimeo",
    "youtube": "YouTube",
    "soundcloud": "SoundCloud",
}

#: Matches a YouTube id in the embed, short and watch forms.
_YOUTUBE_ID = re.compile(r"(?:youtube\.com/(?:embed/|watch\?(?:[^\s]*&)?v=)|youtu\.be/)([a-zA-Z0-9_-]+)")

#: Matches a Vimeo id in both ``vimeo.com/<id>`` and ``player.vimeo.com/video/<id>``.
_VIMEO_ID = re.compile(r"vimeo\.com/(?:video/)?(\d+)")

#: Matches the ``<user>/<track>`` slug of a SoundCloud track URL.
_SOUNDCLOUD_SLUG = re.compile(r"soundcloud\.com/([^/]+/[^/?#]+)")


def identify(url: str) -> tuple[str, str, str] | None:
    """
    Recognize the hosting platform behind a video URL.

    Embed URLs and watch URLs are both accepted, since a scraper may find either
    depending on how the CMS renders its player.

    :param url: Any URL pointing at a hosted video
    :return: ``(platform, id, canonical_url)``, or None if the platform is not
        one this package can resolve

    >>> identify("https://www.youtube.com/embed/aBcDeFgHiJk")
    ('youtube', 'aBcDeFgHiJk', 'https://www.youtube.com/watch?v=aBcDeFgHiJk')
    >>> identify("https://player.vimeo.com/video/999999999999")
    ('vimeo', '999999999999', 'https://vimeo.com/999999999999')
    >>> identify("https://vod46.example.co.kr/sermon.mp4") is None
    True
    """
    if match := _YOUTUBE_ID.search(url):
        video_id = match.group(1)
        return "youtube", video_id, f"https://www.youtube.com/watch?v={video_id}"

    if match := _VIMEO_ID.search(url):
        video_id = match.group(1)
        return "vimeo", video_id, f"https://vimeo.com/{video_id}"

    if "soundcloud.com" in url:
        # A track slug makes a stable id; anything else only has the URL to hash.
        if match := _SOUNDCLOUD_SLUG.search(url):
            slug = match.group(1)
            return "soundcloud", slug.replace("/", "_"), f"https://soundcloud.com/{slug}"
        return "soundcloud", hashlib.md5(url.encode()).hexdigest()[:12], url

    return None


def summarize_types(videos: list[Video]) -> str:
    """
    Summarize how many videos of each platform type were found.

    :param videos: Videos found on a single page
    :return: Comma-separated summary such as "12 Vimeo, 3 YouTube"

    >>> summarize_types([])
    ''
    """
    counts = []
    for video_type, label in PLATFORM_LABELS.items():
        count = sum(1 for v in videos if v.type == video_type)
        if count > 0:
            counts.append(f"{count} {label}")
    return ", ".join(counts)
