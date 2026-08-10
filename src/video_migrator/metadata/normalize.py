#!/usr/bin/env python3
"""
Normalize and validate scraped video metadata.

Scraped titles, dates and artist names are inconsistent; these helpers clean
them up before they are written into media files or uploaded.
"""

import collections
import datetime
import re
from dataclasses import dataclass

from ..config import Board, Profile, load_profile
from ..models import Video
from ..utils.multi_regex_replace import multi_replace

#: Fields normalization may rewrite, and what to call them in a report. The
#: labels match the CSV headings rather than the attribute names, since that is
#: where the reader will go looking.
_TRACKED_FIELDS = {
    "title": "title",
    "bible_verse": "bible verse",
    "artist": "preacher",
    "publish_date": "date",
}


@dataclass(frozen=True)
class Change:
    """One field that a normalization rule rewrote."""

    #: 1-based position in the scraped list, matching :func:`validate_videos`.
    index: int
    #: The video's title after normalization, to identify it in a report.
    title: str
    field: str
    before: str
    after: str


#: How many days after a service its publish date may fall and still be pulled
#: back onto it, when a profile names no policy of its own. One day covers the
#: routine posted-the-next-day case and nothing else.
DEFAULT_MAX_PUBLISH_DATE_DRIFT = 1

#: A correction of a day is the ordinary late upload and not worth reporting.
#: Beyond that the move is worth seeing, even when the profile asks for it.
_QUIET_CORRECTION = 1

#: How a service part taken off the preacher field is written back into the title.
#: The topic leads and the part qualifies it, so that what someone would search
#: for is not buried behind a number.
SERVICE_PART_TITLE_FORMAT = "{title} ({part})"

#: A marker naming which numbered service of the day a recording is, including a
#: joint service of two of them. It deliberately does not match 영상, which says
#: a recording is a video of a service rather than which service it was.
#:
#: A series part ("설교 제목 강해 2부") is indistinguishable from a service part
#: by shape alone and is read as one. That is the same assumption
#: :func:`service_key` already makes, and it errs towards telling two recordings
#: apart rather than merging them.
NUMBERED_SERVICE_PART = re.compile(r"\d[\d,]*부(?:\s*연합)?")


def numbered_service_part(title: str) -> str:
    """
    Read which of the day's services a title names, if it names one.

    :param title: Title, already normalized
    :return: The marker as the title writes it, or empty where it names none

    >>> numbered_service_part("설교 제목 (2부)")
    '2부'
    >>> numbered_service_part("설교 제목 (2부 연합)")
    '2부 연합'
    >>> numbered_service_part("설교 제목 1부 영상")
    '1부'
    >>> numbered_service_part("설교 제목 영상")
    ''
    """
    match = NUMBERED_SERVICE_PART.search(title)
    return match.group(0) if match else ""


def take_service_part(video: Video, pattern: re.Pattern[str]) -> None:
    """
    Move a service part recorded against the preacher into the title.

    Some rows name the service in the preacher field — ``홍길동 선교사_2부설교`` —
    where it describes the service rather than the person. Moving it to the front
    of the title puts it where the rest of the board keeps it, and it is what
    lets two services sharing a date be told apart.

    A title already naming the part keeps the one it has, so a row that was only
    half mis-entered does not end up saying it twice.

    :param video: The video to rewrite, in place
    :param pattern: Regex matching the part at the end of a preacher name, with
        the part itself as its one capture group
    """
    match = pattern.search(video.artist)
    if not match:
        return

    part = match.group(1)
    video.artist = video.artist[: match.start()].strip()
    if part not in video.title:
        video.title = SERVICE_PART_TITLE_FORMAT.format(part=part, title=video.title)


def snap_to_weekday(publish_date: str, weekday: int, max_drift: int = DEFAULT_MAX_PUBLISH_DATE_DRIFT) -> str:
    """
    Move a publish date back to the weekday the service is actually held on.

    Corrections run backwards only: a recording is filed when it is posted, so
    its date lands on or after the service, never before. Only a date at most
    ``max_drift`` days late is moved; anything further is left for
    :func:`validate_videos` to report.

    :param publish_date: Date as scraped, ``YYYY-MM-DD``
    :param weekday: Meeting day as a :meth:`datetime.date.weekday` ordinal
    :param max_drift: Largest number of days a date may be moved back
    :return: The corrected date, or the original where there is nothing to
        correct or too little to go on

    >>> snap_to_weekday("2016-10-17", 6)   # a Monday, one day after the service
    '2016-10-16'
    >>> snap_to_weekday("2016-10-16", 6)   # already the Sunday
    '2016-10-16'
    >>> snap_to_weekday("2016-11-24", 6)   # a Thursday: too far off to guess at
    '2016-11-24'
    >>> snap_to_weekday("sometime in 2016", 6)
    'sometime in 2016'
    """
    try:
        date = datetime.date.fromisoformat(publish_date)
    except ValueError:
        return publish_date

    drift = (date.weekday() - weekday) % 7
    if 0 < drift <= max_drift:
        return (date - datetime.timedelta(days=drift)).isoformat()
    return publish_date


def fix_video_metadata(videos: list[Video], profile: Profile | None = None, board: Board | None = None) -> list[Change]:
    """
    Normalize video metadata including preacher names, titles and dates.

    The title rewrite rules, preacher aliases and title-spacing words all come
    from the site profile, so tuning them is a YAML edit rather than a code
    change.

    Every rewrite is returned rather than printed, so the caller decides how
    loudly to report it. A rule that misfires rewrites just as quietly as one
    that works, which is the whole reason to hand the list back.

    :param videos: List of Video objects to normalize
    :param profile: Site profile supplying the vocabulary; defaults to the
        profile named by :data:`~video_migrator.config.DEFAULT_PROFILE`
    :param board: The board these videos came from, supplying the weekday its
        service is held on. Without it, publish dates are left as scraped
    :return: Every field this changed, in the order the videos were given
    """
    profile = profile or load_profile()
    scraped = [tuple(getattr(video, field) for field in _TRACKED_FIELDS) for video in videos]
    # First pass comes straight from the profile, applied in declaration order
    title_replacements_first = profile.title_replacements

    # Build title replacements dictionary - second pass
    title_replacements_second = {}
    # Add space before specific words if not at start and no space before them
    for word in profile.title_spacing_words:
        title_replacements_second[rf"(\B){re.escape(word)}"] = rf" {word}"
    # Collapse consecutive spaces (must be last)
    title_replacements_second[r"\s{2,}"] = " "

    preacher_service_pattern = (
        re.compile(profile.preacher_service_pattern) if profile.preacher_service_pattern else None
    )

    # The profile's own rules run first — they fix what one site got wrong —
    # then the generic tidying every site needs.
    bible_verse_replacements = {
        **profile.bible_verse_replacements,
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

        # A service part belongs to the service, so take it off the preacher and
        # put it in the title before the name is matched against the aliases.
        if preacher_service_pattern and video.artist:
            take_service_part(video, preacher_service_pattern)

        # Normalize preacher names
        if video.artist in profile.preacher_names:
            video.artist = profile.preacher_names[video.artist]

        # Read back out of the title, which is where the part ends up whether the
        # site put it there or take_service_part just moved it.
        video.service_part = numbered_service_part(video.title)

    if board is not None and board.weekday_index is not None:
        correct_publish_dates(videos, board, profile.max_publish_date_drift, profile.service_parts)

    changes = []
    for index, (was, video) in enumerate(zip(scraped, videos, strict=True), 1):
        for field, before in zip(_TRACKED_FIELDS, was, strict=True):
            after = getattr(video, field)
            if before != after:
                changes.append(Change(index, video.title, _TRACKED_FIELDS[field], before, after))
    return changes


def report_changes(changes: list[Change], verbose: bool = False) -> None:
    """
    Print what normalization rewrote.

    The counts always print: a rule that quietly rewrote 800 titles is worth
    noticing even when every rewrite was correct. The rewrites themselves print
    only when asked for, since a board whose dates all drifted would otherwise
    bury everything else in the output.

    :param changes: The changes :func:`fix_video_metadata` reported
    :param verbose: Whether to list every rewrite rather than only count them
    """
    if not changes:
        return

    counts = collections.Counter(change.field for change in changes)
    summary = ", ".join(f"{count} {field}" for field, count in sorted(counts.items()))
    print(f"\nNormalized {len(changes)} fields: {summary}")

    if verbose:
        for change in changes:
            print(f"  {change.index}. {change.title}: {change.field} {change.before!r} -> {change.after!r}")


def _move_publish_date(video: Video, publish_date: str) -> None:
    """
    Set a video's publish date, keeping the year it derived from it in step.

    Both fields are written into the media file, and a Monday 1 January belongs
    to the year before, so neither can move without the other.

    :param video: The video to redate
    :param publish_date: The corrected date, ``YYYY-MM-DD``
    """
    video.publish_date = publish_date
    video.year = publish_date[:4]


def service_key(title: str, service_parts: tuple[str, ...]) -> frozenset[str]:
    """
    Identify which of a day's services a title belongs to.

    A single day yields several recordings — a first service, a second, a video
    of one of them — so a date alone does not identify a service. The markers a
    title carries do. They are collected as a set rather than matched in order,
    since a title may carry two ("1부 영상" is the video of the first service,
    which is neither the first service nor another day's video).

    :param title: Title, already normalized
    :param service_parts: Markers the profile recognizes
    :return: The markers this title carries; empty when it names no service, which
        is itself a distinguishing answer

    >>> parts = ("1부", "2부", "영상")
    >>> sorted(service_key("찬양 제목 1부 영상", parts))
    ['1부', '영상']
    >>> sorted(service_key("(1부예배) 설교 제목 여섯", parts))
    ['1부']
    >>> sorted(service_key("설교 제목 일곱", parts))
    []
    """
    return frozenset(part for part in service_parts if part in title)


def correct_publish_dates(
    videos: list[Video],
    board: Board,
    max_drift: int = DEFAULT_MAX_PUBLISH_DATE_DRIFT,
    service_parts: tuple[str, ...] = (),
) -> None:
    """
    Pull late publish dates back onto the day of the board's service.

    A correction of a single day is the routine posted-the-next-day case and is
    always applied; a day's services drift together, so several rows sharing a
    date afterwards is expected rather than suspicious.

    A larger correction is a guess, and is refused where something contradicts
    it: another row already holding that date *for the same service*, or a second
    guess competing for it. A different service on that date contradicts nothing
    — a second service belongs beside the first. Refused rows keep their scraped
    date and are reported, because a wrong date that still looks wrong can be
    fixed later, while one made to look right cannot.

    :param videos: Videos from a single board, normalized in place
    :param board: The board they came from, supplying its meeting weekday
    :param max_drift: Largest number of days a date may be moved back
    :param service_parts: Markers distinguishing a day's services from each other
    """
    weekday = board.weekday_index
    if weekday is None:
        return

    routine: list[tuple[Video, str, int]] = []
    guesses: list[tuple[Video, str, int]] = []
    for video in videos:
        if not video.publish_date:
            continue
        snapped = snap_to_weekday(video.publish_date, weekday, max_drift)
        if snapped == video.publish_date:
            continue
        moved = (datetime.date.fromisoformat(video.publish_date) - datetime.date.fromisoformat(snapped)).days
        (routine if moved <= _QUIET_CORRECTION else guesses).append((video, snapped, moved))

    for video, snapped, _ in routine:
        _move_publish_date(video, snapped)

    # Only now is it clear which services the board really holds, since the
    # routine corrections above have moved rows onto their true dates.
    taken = collections.Counter(
        (video.publish_date, service_key(video.title, service_parts)) for video in videos if video.publish_date
    )
    wanted = collections.Counter((snapped, service_key(video.title, service_parts)) for video, snapped, _ in guesses)

    applied, refused = [], []
    for video, snapped, moved in guesses:
        slot = (snapped, service_key(video.title, service_parts))
        if taken[slot] or wanted[slot] > 1:
            refused.append((video, snapped, moved))
        else:
            _move_publish_date(video, snapped)
            applied.append((video, snapped, moved))

    day = board.weekday.capitalize()
    if applied:
        print(f"\nNote: Moved {len(applied)} publish dates more than a day back onto {day}:")
        for video, snapped, moved in applied:
            print(f"  {video.title}: -> {snapped} (-{moved}d)")

    if refused:
        print(f"\nWarning: Left {len(refused)} publish dates alone — that {day}'s service is already taken:")
        for video, snapped, moved in refused:
            print(f"  {video.title}: '{video.publish_date}' would become {snapped} (-{moved}d)")


def validate_videos(videos: list[Video], profile: Profile | None = None, board: Board | None = None) -> None:
    """
    Validate video data and print warnings for any issues found.

    Nothing here is corrected — every warning names something only a human can
    decide, whether that is a missing preacher, a date too far off to guess at,
    or the same recording filed twice.

    :param videos: List of Video objects to validate
    :param profile: Site profile supplying the valid preacher titles; defaults
        to the profile named by :data:`~video_migrator.config.DEFAULT_PROFILE`
    :param board: The board these videos came from, supplying the weekday its
        service is held on. Without it, publish dates are not checked against one
    """
    profile = profile or load_profile()
    meeting_day = board.weekday_index if board else None
    invalid_dates = []
    invalid_preachers = []
    invalid_brackets = []
    off_weekday = []
    # Keyed on the video rather than on title and date: a two-service Sunday
    # files the same sermon twice under one title, as two separate recordings.
    # Only a repeated video means the same recording was filed twice.
    filed_under = collections.defaultdict(list)
    date_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}$")

    for i, video in enumerate(videos, 1):
        title = video.title
        if title and not _has_matching_brackets(title):
            invalid_brackets.append((i, title))

        if video.url:
            filed_under[video.url].append((i, title))

        publish_date = video.publish_date
        if publish_date and not date_pattern.match(publish_date):
            invalid_dates.append((i, video.title, publish_date))
        elif publish_date and meeting_day is not None:
            # Normalization has already pulled back the dates it could account
            # for, so anything still off its weekday needs a human.
            date = datetime.date.fromisoformat(publish_date)
            if date.weekday() != meeting_day:
                off_weekday.append((i, video.title, publish_date, date.strftime("%A")))

        preacher = video.artist
        if preacher and not has_preacher_title(preacher, profile, board):
            invalid_preachers.append((i, video.title, preacher))

    if invalid_brackets:
        print("\nWarning: Found videos with mismatched brackets in title:")
        for idx, title in invalid_brackets:
            print(f"  {idx}. {title}")

    if invalid_dates:
        print("\nWarning: Found videos with invalid publish_date format (expected YYYY-mm-dd):")
        for idx, title, date in invalid_dates:
            print(f"  {idx}. {title}: '{date}'")

    if off_weekday:
        expected = board.weekday.capitalize()
        print(f"\nWarning: Found videos whose publish_date is not a {expected} and is too far off to correct:")
        for idx, title, date, day in off_weekday:
            print(f"  {idx}. {title}: '{date}' is a {day}")

    if invalid_preachers:
        expected = f"ending with {', '.join(repr(t) for t in profile.preacher_titles(board))}"
        if profile.preacher_prefixes(board):
            expected += f" or starting with {', '.join(repr(p) for p in profile.preacher_prefixes(board))}"
        print(f"\nWarning: Found videos with preacher not {expected}:")
        for idx, title, preacher in invalid_preachers:
            print(f"  {idx}. {title}: '{preacher}'")

    repeated = {url: entries for url, entries in filed_under.items() if len(entries) > 1}
    if repeated:
        print(f"\nWarning: Found {len(repeated)} videos filed under more than one record:")
        for url, entries in repeated.items():
            print(f"  {url}")
            for idx, title in entries:
                print(f"    {idx}. {title}")


#: A preacher's home church, appended in parentheses. It qualifies the name
#: rather than standing in for the title, so it is set aside before looking.
_TRAILING_AFFILIATION = re.compile(r"\s*\([^()]*\)$")


def has_preacher_title(preacher: str, profile: Profile, board: Board | None = None) -> bool:
    """
    Check whether a name carries a title its board recognizes.

    Korean titles follow the name and English ones precede it, so a name
    qualifies on either of the two lists. A board that credits performers rather
    than preachers overrides both; where the two lists come back empty there is
    no vocabulary to hold a name to, and every name passes.

    :param preacher: Preacher or performer name, already normalized
    :param profile: Site profile supplying the recognized titles
    :param board: The board the name came from, where it overrides the profile
    :return: True if the name carries a recognized title, or if the board asks
        for no check at all

    >>> profile = load_profile("churchlove")
    >>> has_preacher_title("홍길동 목사", profile)
    True
    >>> has_preacher_title("김영희 목사(예시교회 담임)", profile)
    True
    >>> has_preacher_title("Rev. John Doe", profile)
    True
    >>> has_preacher_title("Jane Roe", profile)
    False
    >>> has_preacher_title("Jane Roe", profile, profile.board("choir_praise"))
    True
    """
    titles = profile.preacher_titles(board)
    prefixes = profile.preacher_prefixes(board)
    if not titles and not prefixes:
        return True

    name = _TRAILING_AFFILIATION.sub("", preacher).strip()
    if any(name.endswith(title) for title in titles):
        return True
    return any(name.startswith(prefix) for prefix in prefixes)


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
