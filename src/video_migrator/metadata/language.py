#!/usr/bin/env python3
"""
The language a recording is in, as the scrape records it and as tools want it.

A scrape records ISO 639-2/B — ``kor``, ``eng`` — and nothing downstream wants
that form. YouTube wants BCP-47, which for these is the bare ISO 639-1 subtag,
and so does Whisper. One map serves both.
"""

#: ISO 639-2/B, as a scrape records it, to the ISO 639-1 subtag everything else
#: asks for. Which language a video declares decides which audiences YouTube
#: offers it to, and which language Whisper transcribes it as; an unstated one
#: is guessed at in both places.
LANGUAGE_TAGS = {"kor": "ko", "eng": "en", "spa": "es", "chi": "zh", "jpn": "ja"}
