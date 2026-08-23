#!/usr/bin/env python3
"""
Command-line interface for listing videos found on a source website.

Scrapes a board, normalizes the metadata, and writes the result out as text,
JSON or CSV - the input list for the download/upload stages of a migration.
"""

import argparse
import csv
import fnmatch
import io
import json
import sys
from dataclasses import asdict

from .config import DEFAULT_PROFILE, load_profile
from .metadata.normalize import fix_video_metadata, report_changes, validate_videos
from .models import Video
from .scrapers import scraper_for


def format_output(videos: list[Video], output_format: str, verbose: bool = False) -> str:
    """
    Format video data according to specified output format.

    :param videos: List of Video objects
    :param output_format: Output format (json, csv, tsv, or text)
    :param verbose: Whether to include verbose information (for text format)
    :return: Formatted output string
    """
    if output_format == "json":
        return json.dumps([asdict(v) for v in videos], indent=2, ensure_ascii=False)
    elif output_format in ("csv", "tsv"):
        # A tab needs no quoting in any field this pipeline carries, so a TSV
        # export stays greppable and column-addressable by cut and awk; a title
        # holding a comma does not survive that in the CSV.
        # Lines end with ``\n`` on every platform, for the reason
        # :func:`~video_migrator.ledger.write_ledger` gives: one dated export is
        # read by diffing it against the last, and a translated line ending
        # changes every row while changing no value.
        csv_buffer = io.StringIO()
        csv_writer = csv.writer(
            csv_buffer,
            delimiter="\t" if output_format == "tsv" else ",",
            quoting=csv.QUOTE_MINIMAL,
            lineterminator="\n",
        )
        csv_writer.writerow(
            [
                "num",
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
                    video.num,
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


#: Prefixed to a preacher pattern to mean "every preacher but this one".
EXCLUDE = "!"


def selects(name: str, patterns: list[str] | None) -> bool:
    """
    Does a set of preacher patterns select this name?

    One list expresses both halves of a choice. A pattern is a shell glob, so
    ``홍길동*`` covers a preacher credited 목사 on one service and 협동목사 on
    another; prefixed with ``!`` it excludes instead, so ``!김영희 목사`` is
    every other preacher on the board. Patterns without a wildcard match
    exactly, which is what most of them are.

    Exclusions are applied after inclusions and win over them, so a list may
    name a group and then carve a name out of it. A list of exclusions alone
    means everyone else.

    :param name: The preacher a recording is credited to
    :param patterns: Patterns to test, or None to select everyone
    :return: Whether the recording is selected

    >>> selects("김영희 목사", None)
    True
    >>> selects("홍길동 목사", ["홍길동 목사"]), selects("김영희 목사", ["홍길동 목사"])
    (True, False)
    >>> selects("홍길동 목사", ["!홍길동 목사"]), selects("김영희 목사", ["!홍길동 목사"])
    (False, True)
    >>> selects("홍길동 협동목사", ["홍길동*"])
    True
    >>> selects("Dr. Doe", ["Dr. *", "!Dr. Doe"])
    False
    """
    if not patterns:
        return True
    keep = [pattern for pattern in patterns if not pattern.startswith(EXCLUDE)]
    drop = [pattern[len(EXCLUDE):] for pattern in patterns if pattern.startswith(EXCLUDE)]
    if any(fnmatch.fnmatchcase(name, pattern) for pattern in drop):
        return False
    return not keep or any(fnmatch.fnmatchcase(name, pattern) for pattern in keep)


def keep_preachers(videos: list[Video], patterns: list[str] | None) -> list[Video]:
    """
    Narrow a scrape to the recordings a given preacher is, or is not, credited on.

    A destination channel usually belongs to one person, and a board holds
    everyone who has ever stood in — guests, visiting speakers, a whole
    conference. Publishing those is a decision about someone else's recording,
    so the migration is told whose to carry rather than working it out.

    Both halves of that come from one list: naming a preacher selects the
    channel that is theirs, and ``!`` before the same name selects everything
    left over for wherever that goes instead. See :func:`selects`.

    :param videos: The scraped videos, already normalized
    :param patterns: Preacher patterns, or None to keep everyone
    :return: The videos to carry forward

    >>> videos = [Video(type="v", id="1", url="", embed_url="", title="설교 제목", artist="홍길동 목사"),
    ...           Video(type="v", id="2", url="", embed_url="", title="설교 제목", artist="김영희 목사")]
    >>> [v.id for v in keep_preachers(videos, None)]
    ['1', '2']
    >>> [v.id for v in keep_preachers(videos, ["홍길동 목사"])]
    <BLANKLINE>
    Keeping 1 recording(s) matching 홍길동 목사; 1 left out.
    ['1']
    >>> [v.id for v in keep_preachers(videos, ["!홍길동 목사"])]
    <BLANKLINE>
    Keeping 1 recording(s) matching !홍길동 목사; 1 left out.
    ['2']
    """
    if not patterns:
        return videos

    kept = [video for video in videos if selects(video.artist, patterns)]
    dropped = len(videos) - len(kept)
    if dropped:
        print(f"\nKeeping {len(kept)} recording(s) matching {', '.join(patterns)}; {dropped} left out.")
    credited = {video.artist for video in videos}
    unmatched = {pattern for pattern in patterns
                 if not any(fnmatch.fnmatchcase(name, pattern.removeprefix(EXCLUDE)) for name in credited)}
    if unmatched:
        print(f"Warning: nothing on this board is credited to {', '.join(sorted(unmatched))}.", file=sys.stderr)
    return kept


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


def create_profile_parser() -> argparse.ArgumentParser:
    """
    Create the parser for the one option that has to be read before the rest.

    Which boards ``--board`` accepts depends on the profile, so ``--profile`` is
    parsed on its own first and then folded into the full parser as a parent.

    :return: Parser accepting only --profile, with help suppressed
    """
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "-P",
        "--profile",
        default=DEFAULT_PROFILE,
        help=f"Site profile to scrape: a name under config/ or a shipped one (default: {DEFAULT_PROFILE})",
    )
    return parser


def create_argument_parser(profile_name: str = DEFAULT_PROFILE) -> argparse.ArgumentParser:
    """
    Create and configure the argument parser for the CLI.

    :param profile_name: Site profile whose boards ``--board`` should accept
    :return: Configured argument parser
    """
    board_names = load_profile(profile_name).board_names

    parser = argparse.ArgumentParser(
        description="Scrape video links and metadata from a webpage",
        parents=[create_profile_parser()],
    )
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
        choices=["json", "text", "csv", "tsv"],
        default="text",
        help="Output format (default: text; tsv is the one to write to a file, since a tab needs no quoting)",
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
    parser.add_argument(
        "--preacher",
        action="append",
        metavar="NAME",
        help="Which preachers to keep, as a shell glob: '홍길동*' covers one credited under more "
             "than one title, and '!김영희 목사' means every preacher but that one. Without a "
             "wildcard a pattern matches exactly. Repeatable, and exclusions win over inclusions. "
             "Omit to keep every preacher on the board.",
    )

    return parser


def main() -> None:
    """
    Main function to handle command-line arguments and initiate scraping.

    :raises SystemExit: Exits with code 1 if scraping fails
    """
    try:
        # --profile decides which boards --board accepts, so read it first. An
        # unknown profile has to fail inside the try: it is reported here, not
        # by argparse.
        profile_args, _ = create_profile_parser().parse_known_args()
        parser = create_argument_parser(profile_args.profile)
        args = parser.parse_args()

        profile = load_profile(args.profile)
        scraper_class = scraper_for(profile)

        # Use board from arguments
        board = profile.board(args.board)
        url = scraper_class.board_url(board.name, profile.name)

        # Set cache duration to 0 if caching is disabled
        cache_duration = 0 if args.no_cache else args.cache_duration

        scraper = scraper_class(
            url,
            genre=board.genre,
            language=args.language,
            cache_dir=args.cache_dir,
            cache_duration=cache_duration,
        )
        videos = scraper.get_all_videos(max_pages=args.pages)

        if not videos:
            print("No videos found on the page.", file=sys.stderr)
            sys.exit(0)

        # Normalize video metadata, reporting what it rewrote
        report_changes(fix_video_metadata(videos, profile, board), args.verbose)

        # After normalization, so that --preacher is matched against the name the
        # profile settles on rather than whichever way the record happened to
        # spell it, and before validation, so the warnings are about what will
        # actually be published.
        videos = keep_preachers(videos, args.preacher)

        # Format output
        output = format_output(videos, args.format, args.verbose)

        # Output to file or stdout
        if args.output:
            with open(args.output, "w", encoding="utf-8", newline="\n") as f:
                f.write(output)
            print(f"\nResults saved to: {args.output}")

            # Validate video data
            validate_videos(videos, profile, board)
        else:
            print(output)

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
