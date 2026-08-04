#!/usr/bin/env python3
"""
Normalize and validate scraped video metadata.

Scraped titles, dates and artist names are inconsistent; these helpers clean
them up before they are written into media files or uploaded.
"""

import re

from ..config import Profile, load_profile
from ..models import Video
from ..utils.multi_regex_replace import multi_replace


def fix_video_metadata(videos: list[Video], profile: Profile | None = None) -> None:
    """
    Normalize video metadata including preacher names and titles.

    The title rewrite rules, preacher aliases and title-spacing words all come
    from the site profile, so tuning them is a YAML edit rather than a code
    change.

    :param videos: List of Video objects to normalize
    :param profile: Site profile supplying the vocabulary; defaults to the
        profile named by :data:`~video_migrator.config.DEFAULT_PROFILE`
    """
    profile = profile or load_profile()
    # First pass comes straight from the profile, applied in declaration order
    title_replacements_first = profile.title_replacements

    # Build title replacements dictionary - second pass
    title_replacements_second = {}
    # Add space before specific words if not at start and no space before them
    for word in profile.title_spacing_words:
        title_replacements_second[rf"(\B){re.escape(word)}"] = rf" {word}"
    # Collapse consecutive spaces (must be last)
    title_replacements_second[r"\s{2,}"] = " "

    # Build bible_verse replacements dictionary
    bible_verse_replacements = {
        r"^[([](.+)[)\]]$": r"\1",  # Remove parentheses/brackets only when wrapping entire string
        r"\s{2,}": " ",  # Collapse consecutive spaces
    }

    for video in videos:
        # Normalize title - apply in two passes
        if video.title:
            video.title = multi_replace(video.title.strip(), title_replacements_first)
            video.title = multi_replace(video.title, title_replacements_second)

        # Normalize bible_verse
        if video.bible_verse:
            video.bible_verse = multi_replace(video.bible_verse, bible_verse_replacements).strip()

        # Normalize preacher names
        if video.artist in profile.preacher_names:
            video.artist = profile.preacher_names[video.artist]


def validate_videos(videos: list[Video], profile: Profile | None = None) -> None:
    """
    Validate video data and print warnings for any issues found.

    :param videos: List of Video objects to validate
    :param profile: Site profile supplying the valid preacher titles; defaults
        to the profile named by :data:`~video_migrator.config.DEFAULT_PROFILE`
    """
    profile = profile or load_profile()
    valid_titles = profile.valid_preacher_titles
    invalid_dates = []
    invalid_preachers = []
    invalid_brackets = []
    date_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}$")

    for i, video in enumerate(videos, 1):
        title = video.title
        if title and not _has_matching_brackets(title):
            invalid_brackets.append((i, title))

        publish_date = video.publish_date
        if publish_date and not date_pattern.match(publish_date):
            invalid_dates.append((i, video.title, publish_date))

        preacher = video.artist
        if preacher and not any(preacher.endswith(title) for title in valid_titles):
            invalid_preachers.append((i, video.title, preacher))

    if invalid_brackets:
        print("\nWarning: Found videos with mismatched brackets in title:")
        for idx, title in invalid_brackets:
            print(f"  {idx}. {title}")

    if invalid_dates:
        print("\nWarning: Found videos with invalid publish_date format (expected YYYY-mm-dd):")
        for idx, title, date in invalid_dates:
            print(f"  {idx}. {title}: '{date}'")

    if invalid_preachers:
        print(f"\nWarning: Found videos with preacher not ending with {', '.join(repr(t) for t in valid_titles)}:")
        for idx, title, preacher in invalid_preachers:
            print(f"  {idx}. {title}: '{preacher}'")


def _has_matching_brackets(text: str) -> bool:
    """
    Check if a string has matching brackets for () and [].

    :param text: String to check
    :return: True if all brackets are matched, False otherwise
    """
    stack = []
    pairs = {"(": ")", "[": "]"}

    for char in text:
        if char in pairs:
            stack.append(char)
        elif char in pairs.values() and (not stack or pairs[stack.pop()] != char):
            return False

    return len(stack) == 0
