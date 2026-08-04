#!/usr/bin/env python3
"""
Write video metadata into media files using ffmpeg.

This is the platform-independent half of a migration: whatever the source and
destination platforms are, the downloaded file gets a consistent set of tags
before it is uploaded.
"""

import subprocess
import sys
from pathlib import Path

from ..config import DEFAULT_CREATION_TIME
from ..models import Video


def _to_iso_8601(date: str, time: str) -> str:
    """
    Combine a date and a time-of-day into an ISO 8601 timestamp.

    :param date: Date in YYYY-MM-DD format
    :param time: Time of day in HH:MM:SS format
    :return: Creation time in ISO 8601 format (YYYY-MM-DDTHH:MM:SS.000000Z)

    >>> _to_iso_8601("2025-12-13", "05:30:00")
    '2025-12-13T05:30:00.000000Z'
    """
    return f"{date}T{time}.000000Z"


def update_video_metadata(
    video_file: str | Path,
    video: Video,
    output_file: str | Path,
    creation_time: str = DEFAULT_CREATION_TIME,
) -> bool:
    """
    Update metadata of an MPEG video file using ffmpeg.

    The caller resolves ``creation_time`` — typically from the board's entry in
    the site profile — so this module stays independent of any source site.

    :param video_file: Path to the input video file
    :param video: Video object containing metadata to write
    :param output_file: Path to output file
    :param creation_time: Time of day (HH:MM:SS) to stamp alongside the publish date
    :return: True if successful, False otherwise
    :raises FileNotFoundError: If input file does not exist
    """
    video_path = Path(video_file)
    if not video_path.exists():
        raise FileNotFoundError(f"Video file not found: {video_file}")

    # Set output file path
    output_path = Path(output_file)

    # Build ffmpeg command
    cmd = ["ffmpeg", "-i", str(video_path), "-c", "copy"]

    # Add metadata options from Video object
    metadata_map = {
        "title": video.title,
        "artist": video.artist,
        "genre": video.genre,
        "date": video.publish_date,
        "year": video.year,
    }

    # Add creation_time if publish_date is available
    if video.publish_date:
        metadata_map["creation_time"] = _to_iso_8601(video.publish_date, creation_time)

    # Add bible_verse as comment only for Sermon genre
    if video.genre == "Sermon":
        metadata_map["comment"] = video.bible_verse

    for key, value in metadata_map.items():
        if value:  # Only add metadata if the field is not empty
            cmd.extend(["-metadata", f"{key}={value}"])

    # Set language for all streams
    if video.language:
        cmd.extend(["-metadata:s:", f"language={video.language}"])

    cmd.append(str(output_path))

    try:
        # Run ffmpeg command
        subprocess.run(cmd, capture_output=True, text=True, check=True)
        return True

    except subprocess.CalledProcessError as e:
        print(f"Error updating video metadata: {e.stderr}", file=sys.stderr)
        # Clean up output file if it exists
        if output_path.exists():
            output_path.unlink()
        return False
