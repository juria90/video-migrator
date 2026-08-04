#!/usr/bin/env python3
"""Test script for creation_time based on board type."""

import json
import subprocess
from pathlib import Path

import pytest

from video_migrator import Video, update_video_metadata
from video_migrator.config import load_profile


def create_test_video(output_path: Path) -> None:
    """
    Create a minimal test video file with 1 video and 1 audio stream (0 length).
    Adds default metadata to ensure the test properly overwrites it.

    :param output_path: Path where the test video should be created
    """
    cmd = [
        "ffmpeg",
        "-f", "lavfi",
        "-i", "color=c=black:s=320x240:d=0.1",  # Black video, 0.1 second
        "-f", "lavfi",
        "-i", "anullsrc=r=44100:cl=stereo:d=0.1",  # Silent audio, 0.1 second
        "-c:v", "libx264",
        "-c:a", "aac",
        "-t", "0.1",
        # Add default metadata that should be overwritten
        "-metadata", "title=Original Title",
        "-metadata", "artist=Original Artist",
        "-metadata", "genre=Original Genre",
        "-metadata", "date=1999-01-01",
        "-metadata", "year=1999",
        "-metadata", "creation_time=1999-01-01T00:00:00.000000Z",
        "-metadata", "comment=Original Comment",
        "-metadata:s:", "language=eng",
        "-y",  # Overwrite output file
        str(output_path),
    ]

    subprocess.run(cmd, capture_output=True, check=True)


@pytest.fixture
def test_video_file(tmp_path):
    """Create a temporary test video file."""
    video_path = tmp_path / "test_input.mp4"
    create_test_video(video_path)
    yield video_path
    # Cleanup happens automatically with tmp_path


@pytest.mark.parametrize(
    "board,expected_time,publish_date",
    [
        ("early_morning_prayer", "05:30:00", "2012-06-25"),
        ("sunday_sermon", "10:30:00", "2023-03-15"),
        ("friday_prayer", "19:30:00", "2024-11-08"),
        ("wednesday_prayer", "00:00:00", "2022-01-20"),
        ("charisma_praise", "00:00:00", "2021-12-25"),
    ],
)
def test_creation_time_for_board(test_video_file, tmp_path, board, expected_time, publish_date):
    """
    Test that creation_time is set correctly based on board type.

    :param test_video_file: Fixture providing test video file
    :param tmp_path: Pytest fixture for temporary directory
    :param board: Board name
    :param expected_time: Expected time portion (HH:MM:SS)
    :param publish_date: Publish date (YYYY-MM-DD)
    """
    # Get original metadata
    original_result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            str(test_video_file),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    original_metadata = json.loads(original_result.stdout)
    original_tags = original_metadata.get("format", {}).get("tags", {})

    print(f"\n--- Board: {board} ---")
    print("Original metadata:")
    for key in ["title", "artist", "genre", "date", "year", "creation_time", "comment"]:
        print(f"  {key}: {original_tags.get(key, 'N/A')}")

    output_file = tmp_path / f"test_output_{board}.mp4"

    video = Video(
        type="vimeo",
        id="123456789",
        url="https://vimeo.com/123456789",
        embed_url="https://player.vimeo.com/video/123456789",
        title=f"Test video for {board}",
        bible_verse="Test verse" if "prayer" in board or "sermon" in board else "",
        publish_date=publish_date,
        artist="Test Artist",
        genre="Sermon" if "prayer" in board or "sermon" in board else "Praise",
        language="kor",
    )

    # The board's time-of-day now comes from the site profile; check the profile
    # still declares what this test expects before checking what ffmpeg wrote.
    profile_time = load_profile().board(board).creation_time
    assert profile_time == expected_time, f"Profile declares {profile_time} for board {board}, expected {expected_time}"

    # Update metadata
    success = update_video_metadata(test_video_file, video, output_file, creation_time=profile_time)
    assert success, f"Failed to update metadata for board {board}"

    # Verify the creation_time in the output file
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            str(output_file),
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    metadata = json.loads(result.stdout)
    new_tags = metadata.get("format", {}).get("tags", {})
    creation_time = new_tags.get("creation_time", "")

    print("New metadata:")
    for key in ["title", "artist", "genre", "date", "year", "creation_time", "comment"]:
        print(f"  {key}: {new_tags.get(key, 'N/A')}")

    expected_creation_time = f"{publish_date}T{expected_time}.000000Z"

    assert creation_time == expected_creation_time, (
        f"Board '{board}': Expected creation_time '{expected_creation_time}', "
        f"but got '{creation_time}'"
    )


if __name__ == "__main__":
    # Run tests when executed directly
    pytest.main([__file__, "-v"])
