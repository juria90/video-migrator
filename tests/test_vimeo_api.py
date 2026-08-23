#!/usr/bin/env python3
"""Tests for choosing which of a recording's download links to pull."""

import pytest

from video_migrator.sources.vimeo_api import ENV_TOKEN, best_download, load_token, preferred_downloads


def test_a_video_offering_nothing_yields_nothing() -> None:
    """A recording with downloads switched off has no link to choose between."""
    assert best_download([]) is None


def test_the_original_upload_wins_over_any_rendition() -> None:
    """
    The original is taken even when a rendition is larger.

    Size is the tie-breaker among renditions, never a reason to prefer one over
    the source: a rendition has been re-encoded, and this archive is measured
    for artifacts that re-encoding resamples.
    """
    downloads = [{"quality": "hd", "size": 900}, {"quality": "source", "size": 1}]
    assert best_download(downloads)["quality"] == "source"


def test_the_largest_rendition_stands_in_for_a_missing_original() -> None:
    """Where no original is offered, the least-processed rendition is the largest."""
    downloads = [{"quality": "hd", "size": 10}, {"quality": "sd", "size": 40}]
    assert best_download(downloads)["size"] == 40


def test_an_entry_without_a_size_is_not_preferred_by_accident() -> None:
    """
    A missing size sorts as zero rather than raising.

    Vimeo omits keys it has nothing for, so a response is not a promise that
    every field is present.
    """
    downloads = [{"quality": "hd"}, {"quality": "sd", "size": 40}]
    assert best_download(downloads)["quality"] == "sd"


def test_a_missing_environment_file_says_what_it_was_for(tmp_path) -> None:
    """
    The token lives outside the repository, so its absence is the normal first run.

    :param tmp_path: Fixture supplying a directory with no ``.env`` in it
    """
    with pytest.raises(SystemExit) as raised:
        load_token(str(tmp_path / ".env"))
    assert ENV_TOKEN in str(raised.value)


def test_a_file_without_the_token_is_reported_as_such(tmp_path) -> None:
    """
    A ``.env`` holding other credentials is not the same as no ``.env``.

    :param tmp_path: Fixture supplying a directory to write into
    """
    env = tmp_path / ".env"
    env.write_text("# a comment\nSOMETHING_ELSE=value\n", encoding="utf-8")
    with pytest.raises(SystemExit) as raised:
        load_token(str(env))
    assert ENV_TOKEN in str(raised.value)


def test_the_token_is_read_past_comments_and_whitespace(tmp_path) -> None:
    """
    The file is hand-edited, so it is read the way the other loaders read it.

    :param tmp_path: Fixture supplying a directory to write into
    """
    env = tmp_path / ".env"
    env.write_text(f"# Vimeo.\n{ENV_TOKEN} = abc123 \n", encoding="utf-8")
    assert load_token(str(env)) == "abc123"


def test_the_original_is_first_but_not_the_only_choice() -> None:
    """
    Every link is kept in preference order, because the first is not always served.

    Recordings from this archive's early years advertise an original and then
    redirect nowhere when asked for it, so the caller has to be able to walk on
    to the next one rather than being handed a single answer.
    """
    downloads = [{"quality": "sd", "size": 40}, {"quality": "source", "size": 4}, {"quality": "hd", "size": 90}]
    assert [entry["quality"] for entry in preferred_downloads(downloads)] == ["source", "hd", "sd"]


def test_preference_order_is_total_even_without_an_original() -> None:
    """Without a source entry the order is simply largest first."""
    downloads = [{"quality": "sd", "size": 40}, {"quality": "hd"}, {"quality": "hd", "size": 90}]
    assert [entry.get("size") for entry in preferred_downloads(downloads)] == [90, 40, None]
