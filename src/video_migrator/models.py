#!/usr/bin/env python3
"""
Shared data model for videos moving between platforms.

The :class:`Video` record is the common currency of the pipeline: scrapers
produce it, sources download against it, the metadata layer writes it into the
media file, and sinks upload it.
"""

import re
from dataclasses import dataclass


@dataclass
class Video:
    """Video metadata container."""

    type: str
    id: str
    url: str
    embed_url: str
    title: str
    bible_verse: str = ""
    publish_date: str = ""
    artist: str = ""
    year: str = ""
    genre: str = ""
    language: str = ""
    #: Which of a day's services this recording is, as the marker the site uses
    #: ("2부"). Empty on a board that holds one service a day, and on a row that
    #: names none. It stays in the title as well, since that is where
    #: :func:`~video_migrator.metadata.normalize.service_key` looks.
    service_part: str = ""
    #: The source CMS's own id for the record this came from, and the only key
    #: that identifies every row: two records can share a video, a record can
    #: carry no video at all, and a date holds as many rows as there were
    #: services that day. Correction ledgers key on it. Empty for a scraper
    #: whose CMS exposes no such id.
    num: str = ""

    def __post_init__(self):
        """Extract year from publish_date and detect language if not already set."""
        if not self.year and self.publish_date:
            # Extract year from date format YYYY-MM-DD
            year_match = re.match(r"^(\d{4})", self.publish_date)
            if year_match:
                self.year = year_match.group(1)

        if not self.language and self.title:
            # Detect language based on title content
            self.language = self._detect_language(self.title + self.bible_verse)

    def _detect_language(self, text: str) -> str:
        """
        Detect language of text based on character ranges.

        :param text: Text to analyze
        :return: ISO 639-2/B language code ('kor' for Korean, 'eng' for English, etc.)
        """
        # Count different character types
        korean_count = sum(1 for char in text if "가" <= char <= "힣")  # Hangul syllables

        # If significant Korean characters, return Korean
        if korean_count > 0:
            return "kor"

        # Default to English for Latin script or unknown
        return "eng"
