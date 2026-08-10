#!/usr/bin/env python3
"""
Site profiles: the per-site data the pipeline needs, kept out of the code.

A profile describes one source website — which software it runs, which boards it
has, and the vocabulary used to normalize the metadata scraped from it. Adding a
board or a preacher alias is a YAML edit, not a code change; so is pointing a new
site at a scraper this package already has, via ``site.scraper``.

``load_profile()`` resolves a profile by name, first hit wins:

1. ``$VIDEO_MIGRATOR_CONFIG`` — full path to a YAML file, overrides everything
2. ``./config/<name>.yaml`` — per-checkout override, relative to the cwd
3. ``video_migrator/profiles/<name>.yaml`` — the copy shipped in the package

The packaged copy is the one maintainers edit; it is data that happens to live
in the package so that ``pip install`` users get a working CLI without a
checkout. The first two entries let a deployment deviate without touching it.

Loading is strict on purpose. Plain YAML accepts a duplicate mapping key and
keeps the last one, so forgetting the leading ``- `` on a new board silently
merges it into its predecessor; :class:`_StrictLoader` raises instead.
"""

import os
from dataclasses import dataclass
from functools import cache
from importlib.resources import files
from pathlib import Path

import yaml

#: Stamped as the time-of-day when a board declares no ``creation_time``.
DEFAULT_CREATION_TIME = "00:00:00"

#: Environment variable holding a full path to a profile YAML file.
CONFIG_ENV_VAR = "VIDEO_MIGRATOR_CONFIG"

DEFAULT_PROFILE = "example"

#: Weekday names a board may declare, in ``datetime.date.weekday()`` order.
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

#: Title an upload is given when a profile names no template of its own. The
#: date leads because the platform stamps its own upload date on the video and
#: will not accept the original one, so the title is the only place the date a
#: recording belongs to survives where a viewer will see it.
DEFAULT_UPLOAD_TITLE_TEMPLATE = "{date} | {service} | {title} | {artist}"

#: How the ``{date}`` slot is written. Kept as a ``str.format`` template rather
#: than a ``strftime`` one so that an unpadded month reads the same everywhere:
#: ``%-m`` is a GNU extension that Windows does not have. Pad with ``{month:02d}``.
DEFAULT_UPLOAD_DATE_FORMAT = "{year}.{month}.{day}"


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate mapping keys instead of silently merging."""


def _no_duplicate_keys(loader: _StrictLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    """
    Construct a mapping, raising if any key appears twice.

    :param loader: The active loader
    :param node: Mapping node being constructed
    :param deep: Whether to construct child objects eagerly
    :return: The constructed mapping
    :raises yaml.constructor.ConstructorError: If a key is repeated
    """
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"duplicate key {key!r} (did you forget a '- ' before a new entry?)",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicate_keys)


@dataclass(frozen=True)
class Board:
    """One board (category) on a source site."""

    name: str
    genre: str = ""
    creation_time: str = DEFAULT_CREATION_TIME
    #: How the site addresses this board, for CMSes that key boards by an opaque
    #: code rather than by the name used here. Empty when the name is enough.
    page_code: str = ""
    #: Weekday this board's service is held on, lowercase. Empty for a board with
    #: no fixed day, such as a daily prayer meeting or an occasional series.
    weekday: str = ""
    #: What this board's service is called, for the service segment of an upload
    #: title. Empty leaves the segment out.
    service_name: str = ""
    #: :attr:`service_name` for a record that names which of the day's services
    #: it is, with a ``{part}`` slot for the marker. Empty falls back to
    #: :attr:`service_name`, as does a record carrying no part.
    service_template: str = ""
    #: Preacher titles accepted on this board, overriding the profile's list. A
    #: praise board credits a performer, not a preacher, so it needs either its
    #: own vocabulary or none at all. None inherits the profile's list; an empty
    #: list, with no prefixes either, turns the check off for this board.
    valid_preacher_titles: tuple[str, ...] | None = None
    #: Per-board counterpart of :attr:`Profile.valid_preacher_prefixes`, on the
    #: same None-inherits rule as :attr:`valid_preacher_titles`.
    valid_preacher_prefixes: tuple[str, ...] | None = None

    def service_label(self, part: str = "") -> str:
        """
        Name this board's service, qualified by which of the day's services it is.

        :param part: The service marker the record carries, empty if it names none
        :return: The service segment of an upload title, empty when the board
            declares no name

        >>> sunday = load_profile().board("sunday_sermon")
        >>> sunday.service_label("2부")
        '주일 2부 예배'
        >>> sunday.service_label()
        '주일예배'
        >>> load_profile().board("no_such_board").service_label("2부")
        ''
        """
        if part and self.service_template:
            return self.service_template.format(part=part)
        return self.service_name

    @property
    def weekday_index(self) -> int | None:
        """
        The board's meeting day as a :meth:`datetime.date.weekday` ordinal.

        :return: 0 for Monday through 6 for Sunday, or None if the board keeps
            no fixed day

        >>> load_profile("churchlove").board("sunday_sermon").weekday_index
        6
        >>> load_profile("churchlove").board("early_morning_prayer").weekday_index is None
        True
        """
        return WEEKDAYS.index(self.weekday) if self.weekday else None


@dataclass(frozen=True)
class Profile:
    """Everything site-specific for one source website."""

    name: str
    board_url: str
    boards: tuple[Board, ...]
    #: Regex -> replacement, applied to titles in declaration order.
    title_replacements: dict[str, str]
    #: Regex -> replacement, applied to bible verses in declaration order, before
    #: the generic tidying every site needs.
    bible_verse_replacements: dict[str, str]
    preacher_names: dict[str, str]
    valid_preacher_titles: tuple[str, ...]
    title_spacing_words: tuple[str, ...]
    #: Name of the profile whose scraper parses this site, for a site running
    #: software some other profile already describes. Empty means this profile's
    #: own name is registered in :data:`~video_migrator.scrapers.SCRAPERS`.
    scraper: str = ""
    #: Titles that come *before* the name instead of after it, as English ones
    #: do. A name qualifies on either this list or :attr:`valid_preacher_titles`.
    valid_preacher_prefixes: tuple[str, ...] = ()
    #: How many days late a publish date may be and still be pulled back onto its
    #: board's weekday. 1 corrects only the routine posted-the-next-day case; 6
    #: treats the weekday as an invariant and always corrects to it.
    max_publish_date_drift: int = 1
    #: Markers naming which service of the day a title belongs to, so that the
    #: several recordings a single day produces can be told apart.
    service_parts: tuple[str, ...] = ()
    #: Regex matching a service part recorded at the end of a preacher name, with
    #: the part itself as its one capture group. Such a part describes the
    #: service rather than the person, so it is moved into the title.
    preacher_service_pattern: str = ""
    #: Title an upload is given, over ``{date}``, ``{service}``, ``{title}`` and
    #: ``{artist}``. A slot resolving to nothing takes its separator with it.
    upload_title_template: str = DEFAULT_UPLOAD_TITLE_TEMPLATE
    #: How the template's ``{date}`` slot is written, over ``{year}``,
    #: ``{short_year}``, ``{month}`` and ``{day}``.
    upload_date_format: str = DEFAULT_UPLOAD_DATE_FORMAT

    def preacher_titles(self, board: Board | None = None) -> tuple[str, ...]:
        """
        The preacher titles that count as valid on a board.

        :param board: The board being validated, or None for the profile's own list
        :return: The titles to accept, the board's own where it declares any

        >>> load_profile().preacher_titles()[0]
        ' 목사'
        >>> load_profile().preacher_titles(load_profile().board("choir_praise"))
        ()
        """
        if board is not None and board.valid_preacher_titles is not None:
            return board.valid_preacher_titles
        return self.valid_preacher_titles

    def preacher_prefixes(self, board: Board | None = None) -> tuple[str, ...]:
        """
        The preacher name prefixes that count as valid on a board.

        :param board: The board being validated, or None for the profile's own list
        :return: The prefixes to accept, the board's own where it declares any
        """
        if board is not None and board.valid_preacher_prefixes is not None:
            return board.valid_preacher_prefixes
        return self.valid_preacher_prefixes

    @property
    def board_names(self) -> tuple[str, ...]:
        """
        Board names in declaration order; the first is the CLI default.

        :return: Tuple of board names
        """
        return tuple(board.name for board in self.boards)

    def board(self, name: str) -> Board:
        """
        Look up a board by name.

        An unknown name yields a default :class:`Board` — empty genre, midnight
        creation time — so callers never need to special-case it.

        :param name: Board table name
        :return: The matching board, or a default one

        >>> profile = load_profile()
        >>> profile.board("sunday_sermon").creation_time
        '10:30:00'
        >>> profile.board("no_such_board").creation_time
        '00:00:00'
        """
        for board in self.boards:
            if board.name == name:
                return board
        return Board(name=name)


def _optional_tuple(entry: dict, key: str) -> tuple[str, ...] | None:
    """
    Read a list a board may override, telling "not declared" from "declared empty".

    The difference is the whole point of the override: an absent key inherits the
    profile's list, while an empty one turns the check off for that board.

    :param entry: The board's parsed YAML mapping
    :param key: Key to read
    :return: The declared values, or None where the board declares none
    """
    if key not in entry:
        return None
    return tuple(entry[key] or ())


def _parse(name: str, data: dict) -> Profile:
    """
    Build a :class:`Profile` from a parsed YAML document.

    :param name: Profile name, used in error messages
    :param data: Parsed YAML document
    :return: The profile
    :raises ValueError: If the document declares no boards, a board has no name,
        or two boards share a name
    """
    boards = []
    seen: set[str] = set()
    for index, entry in enumerate(data.get("boards") or []):
        if "name" not in entry:
            raise ValueError(f"profile {name!r}: board entry {index} has no 'name'")
        if entry["name"] in seen:
            raise ValueError(f"profile {name!r}: duplicate board {entry['name']!r}")
        seen.add(entry["name"])
        weekday = str(entry.get("weekday", "")).lower()
        if weekday and weekday not in WEEKDAYS:
            raise ValueError(
                f"profile {name!r}: board {entry['name']!r} declares weekday {weekday!r}; "
                f"expected one of {', '.join(WEEKDAYS)}"
            )
        boards.append(
            Board(
                name=entry["name"],
                genre=entry.get("genre", ""),
                creation_time=entry.get("creation_time", DEFAULT_CREATION_TIME),
                # A page code is written unquoted in YAML, so it arrives as an int.
                page_code=str(entry.get("page_code", "")),
                weekday=weekday,
                service_name=entry.get("service_name", ""),
                service_template=entry.get("service_template", ""),
                valid_preacher_titles=_optional_tuple(entry, "valid_preacher_titles"),
                valid_preacher_prefixes=_optional_tuple(entry, "valid_preacher_prefixes"),
            )
        )
    if not boards:
        raise ValueError(f"profile {name!r} declares no boards")

    site = data.get("site") or {}
    normalize = data.get("normalize") or {}
    upload = data.get("upload") or {}
    drift = int(normalize.get("max_publish_date_drift", 1))
    if not 0 <= drift <= 6:
        raise ValueError(f"profile {name!r}: max_publish_date_drift must be 0-6, got {drift}")
    return Profile(
        name=name,
        board_url=site.get("board_url", ""),
        boards=tuple(boards),
        title_replacements=dict(normalize.get("title_replacements") or {}),
        bible_verse_replacements=dict(normalize.get("bible_verse_replacements") or {}),
        preacher_names=dict(normalize.get("preacher_names") or {}),
        valid_preacher_titles=tuple(normalize.get("valid_preacher_titles") or ()),
        title_spacing_words=tuple(normalize.get("title_spacing_words") or ()),
        scraper=site.get("scraper", ""),
        valid_preacher_prefixes=tuple(normalize.get("valid_preacher_prefixes") or ()),
        max_publish_date_drift=drift,
        service_parts=tuple(normalize.get("service_parts") or ()),
        preacher_service_pattern=normalize.get("preacher_service_pattern", ""),
        upload_title_template=upload.get("title_template", DEFAULT_UPLOAD_TITLE_TEMPLATE),
        upload_date_format=upload.get("date_format", DEFAULT_UPLOAD_DATE_FORMAT),
    )


def _override_paths(name: str) -> list[Path]:
    """
    Filesystem locations checked before falling back to the packaged profile.

    :param name: Profile name
    :return: Candidate paths, highest precedence first
    """
    paths = []
    if env_path := os.environ.get(CONFIG_ENV_VAR):
        paths.append(Path(env_path))
    # A site keeps everything about itself in one directory, its profile
    # included. config/ is where profiles used to live, and still works.
    paths.append(Path("sites") / name / f"{name}.yaml")
    paths.append(Path("config") / f"{name}.yaml")
    return paths


@cache
def load_profile(name: str = DEFAULT_PROFILE) -> Profile:
    """
    Load a site profile by name, cached for the life of the process.

    :param name: Profile name (e.g. 'example', or your own under config/)
    :return: The parsed profile
    :raises FileNotFoundError: If no profile of that name can be found
    :raises ValueError: If the profile is malformed

    >>> load_profile().board_names[0]
    'early_morning_prayer'
    """
    for path in _override_paths(name):
        if path.is_file():
            return _parse(name, yaml.load(path.read_text(encoding="utf-8"), Loader=_StrictLoader))

    packaged = files("video_migrator").joinpath("profiles", f"{name}.yaml")
    if not packaged.is_file():
        raise FileNotFoundError(
            f"No profile named {name!r}. Looked in ${CONFIG_ENV_VAR}, ./sites/{name}/{name}.yaml, "
            f"./config/{name}.yaml, and the packaged profiles."
        )
    return _parse(name, yaml.load(packaged.read_text(encoding="utf-8"), Loader=_StrictLoader))
