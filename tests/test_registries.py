#!/usr/bin/env python3
"""Tests for the source/sink/scraper registries."""

import pytest

from video_migrator.scrapers import SCRAPERS, GnuBoardScraper, get_scraper
from video_migrator.sinks import SINKS, get_uploader
from video_migrator.sources import SOURCES, get_downloader


def test_get_scraper_returns_registered_class() -> None:
    """A registered site name resolves to its scraper class."""
    assert get_scraper("example") is GnuBoardScraper


def test_get_downloader_resolves_every_registered_source() -> None:
    """Every registered source can be imported and is callable."""
    for source in SOURCES:
        assert callable(get_downloader(source))


def test_get_uploader_resolves_every_registered_sink() -> None:
    """Every registered sink can be imported and is callable."""
    for sink in SINKS:
        assert callable(get_uploader(sink))


def test_registries_are_not_empty() -> None:
    """The pipeline is useless without at least one of each stage."""
    assert SCRAPERS and SOURCES and SINKS


@pytest.mark.parametrize(
    "lookup,name",
    [
        (get_scraper, "nosuchsite"),
        (get_downloader, "nosuchsource"),
        (get_uploader, "nosuchsink"),
    ],
)
def test_unknown_name_raises_keyerror(lookup, name) -> None:
    """
    Unknown names fail loudly and list the known options.

    :param lookup: Registry lookup function under test
    :param name: Name that is not registered
    """
    with pytest.raises(KeyError):
        lookup(name)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
