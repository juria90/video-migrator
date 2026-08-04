#!/usr/bin/env python3
"""
Command-line interface for listing videos found on a source website.

Scrapes a board, normalizes the metadata, and writes the result out as text,
JSON or CSV - the input list for the download/upload stages of a migration.
"""

import argparse
import csv
import io
import json
import sys
from dataclasses import asdict

from .config import DEFAULT_PROFILE, load_profile
from .metadata.normalize import fix_video_metadata, validate_videos
from .models import Video
from .scrapers.gnuboard import GnuBoardScraper, board_url


def format_output(videos: list[Video], output_format: str, verbose: bool = False) -> str:
    """
    Format video data according to specified output format.

    :param videos: List of Video objects
    :param output_format: Output format (json, csv, or text)
    :param verbose: Whether to include verbose information (for text format)
    :return: Formatted output string
    """
    if output_format == "json":
        return json.dumps([asdict(v) for v in videos], indent=2, ensure_ascii=False)
    elif output_format == "csv":
        csv_buffer = io.StringIO()
        csv_writer = csv.writer(csv_buffer, quoting=csv.QUOTE_MINIMAL)
        csv_writer.writerow(
            [
                "Type",
                "ID",
                "URL",
                "Embed URL",
                "Title",
                "Bible Verse",
                "Publish Date",
                "Year",
                "Preacher",
                "Genre",
                "Language",
            ]
        )
        for video in videos:
            csv_writer.writerow(
                [
                    video.type,
                    video.id,
                    video.url,
                    video.embed_url,
                    video.title,
                    video.bible_verse,
                    video.publish_date,
                    video.year,
                    video.artist,
                    video.genre,
                    video.language,
                ]
            )
        return csv_buffer.getvalue()
    else:  # text
        output = ""
        for i, video in enumerate(videos, 1):
            output += f"\n{i}. [{video.type.upper()}] {video.title}\n"
            if video.bible_verse:
                output += f"   Bible Verse: {video.bible_verse}\n"
            if video.publish_date:
                output += f"   Date: {video.publish_date}\n"
            if video.year:
                output += f"   Year: {video.year}\n"
            if video.artist:
                output += f"   Preacher: {video.artist}\n"
            if video.genre:
                output += f"   Genre: {video.genre}\n"
            if video.language:
                output += f"   Language: {video.language}\n"
            output += f"   URL: {video.url}\n"
            if verbose:
                output += f"   ID: {video.id}\n"
                output += f"   Embed: {video.embed_url}\n"
        return output


def _validate_language(value: str) -> str:
    """
    Validate that language is either empty or a 3-letter ISO 639-2/B code.

    :param value: Language code to validate
    :return: The validated language code
    :raises argparse.ArgumentTypeError: If the language code is invalid
    """
    if value == "":
        return value
    if len(value) == 3 and value.isalpha():
        return value.lower()
    raise argparse.ArgumentTypeError(f"Language must be empty or a 3-letter ISO 639-2/B code, got: {value}")


def create_argument_parser() -> argparse.ArgumentParser:
    """
    Create and configure the argument parser for the CLI.

    :return: Configured argument parser
    """
    board_names = load_profile(DEFAULT_PROFILE).board_names

    parser = argparse.ArgumentParser(description="Scrape video links and metadata from a webpage")
    parser.add_argument(
        "-b",
        "--board",
        "--bo-table",
        choices=board_names,
        default=board_names[0],
        help=f"Board table name on the profile's site (default: {board_names[0]})",
    )
    parser.add_argument(
        "-l",
        "--language",
        type=_validate_language,
        default="",
        help="Language of the videos as 3-letter ISO 639-2/B code (e.g., 'kor' for Korean, 'eng' for English)",
    )
    parser.add_argument(
        "-p",
        "--pages",
        type=int,
        default=None,
        help="Number of pages to scrape (default: auto-detect from pagination)",
    )
    parser.add_argument("-o", "--output", help="Output JSON file path")
    parser.add_argument(
        "-f",
        "--format",
        choices=["json", "text", "csv"],
        default="text",
        help="Output format (default: text)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Show detailed information")
    parser.add_argument(
        "--cache-dir",
        default=".cache",
        help="Directory for caching HTML pages (default: .cache)",
    )
    parser.add_argument(
        "--cache-duration",
        type=int,
        default=3600,
        help="Cache duration in seconds (default: 3600)",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Disable caching and always fetch fresh content",
    )

    return parser


def main() -> None:
    """
    Main function to handle command-line arguments and initiate scraping.

    :raises SystemExit: Exits with code 1 if scraping fails
    """
    parser = create_argument_parser()
    args = parser.parse_args()

    try:
        # Use board from arguments
        board = args.board
        url = board_url(board)
        genre = load_profile(DEFAULT_PROFILE).board(board).genre

        # Set cache duration to 0 if caching is disabled
        cache_duration = 0 if args.no_cache else args.cache_duration

        scraper = GnuBoardScraper(
            url,
            genre=genre,
            language=args.language,
            cache_dir=args.cache_dir,
            cache_duration=cache_duration,
        )
        videos = scraper.get_all_videos(max_pages=args.pages)

        if not videos:
            print("No videos found on the page.", file=sys.stderr)
            sys.exit(0)

        # Normalize video metadata
        fix_video_metadata(videos)

        # Format output
        output = format_output(videos, args.format, args.verbose)

        # Output to file or stdout
        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(output)
            print(f"\nResults saved to: {args.output}")

            # Validate video data
            validate_videos(videos)
        else:
            print(output)

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
