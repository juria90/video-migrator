#!/usr/bin/env python3
"""
Site profiles: the per-site data the pipeline needs, kept out of the code.

A profile describes one source website — which boards it has, and the vocabulary
used to normalize the metadata scraped from it. Adding a board or a preacher
alias is a YAML edit, not a code change.

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


@dataclass(frozen=True)
class Profile:
    """Everything site-specific for one source website."""

    name: str
    board_url: str
    boards: tuple[Board, ...]
    #: Regex -> replacement, applied to titles in declaration order.
    title_replacements: dict[str, str]
    preacher_names: dict[str, str]
    valid_preacher_titles: tuple[str, ...]
    title_spacing_words: tuple[str, ...]

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
        boards.append(
            Board(
                name=entry["name"],
                genre=entry.get("genre", ""),
                creation_time=entry.get("creation_time", DEFAULT_CREATION_TIME),
            )
        )
    if not boards:
        raise ValueError(f"profile {name!r} declares no boards")

    normalize = data.get("normalize") or {}
    return Profile(
        name=name,
        board_url=(data.get("site") or {}).get("board_url", ""),
        boards=tuple(boards),
        title_replacements=dict(normalize.get("title_replacements") or {}),
        preacher_names=dict(normalize.get("preacher_names") or {}),
        valid_preacher_titles=tuple(normalize.get("valid_preacher_titles") or ()),
        title_spacing_words=tuple(normalize.get("title_spacing_words") or ()),
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
            f"No profile named {name!r}. Looked in ${CONFIG_ENV_VAR}, ./config/{name}.yaml, and the packaged profiles."
        )
    return _parse(name, yaml.load(packaged.read_text(encoding="utf-8"), Loader=_StrictLoader))
