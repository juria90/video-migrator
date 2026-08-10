#!/usr/bin/env python3
"""Tests for the resource the YouTube sink sends to videos.insert, and the channel guard."""

import argparse

import pytest
from googleapiclient.errors import HttpError

from video_migrator.sinks.youtube import (
    CHANNEL_ENV_VAR,
    as_recording_timestamp,
    build_body,
    expected_channel,
    verify_channel,
)


def options(**overrides) -> argparse.Namespace:
    """
    Build the options an upload is described by.

    :param overrides: Any field to differ from a plain public upload
    :return: The options namespace
    """
    fields = {
        "title": "2026.8.2 | 주일예배 | 설교 제목 | 홍길동 목사",
        "description": "설교 제목",
        "keywords": "",
        "category": 29,
        "privacyStatus": "public",
        "publishAt": None,
        "recordingDate": None,
        "channel": None,
    }
    return argparse.Namespace(**{**fields, **overrides})


def test_a_plain_upload_sends_no_publish_at() -> None:
    """
    An unscheduled upload must not carry a null publishAt it never asked for.

    The request's part is derived from the body's keys, so a field present but
    empty is a field the API is being told to apply.
    """
    body = build_body(options())
    assert body["status"] == {"privacyStatus": "public"}
    assert "recordingDetails" not in body


def test_scheduling_from_public_is_refused() -> None:
    """
    The API only schedules a private video, so the combination fails here first.

    Catching it before the upload starts matters: the file is uploaded before
    the metadata is rejected, so a late failure costs the whole transfer.
    """
    with pytest.raises(ValueError, match="private"):
        build_body(options(publishAt="2026-12-31T23:59:00Z"))


def test_scheduling_from_private_is_accepted() -> None:
    """A private video is the one case publishAt is valid on."""
    body = build_body(options(privacyStatus="private", publishAt="2026-12-31T23:59:00Z"))
    assert body["status"] == {"privacyStatus": "private", "publishAt": "2026-12-31T23:59:00Z"}


def test_a_recording_date_is_widened_to_a_timestamp() -> None:
    """
    A back-catalogue date is carried as recordingDate, the one past date allowed.

    publishAt schedules forwards only and snippet.publishedAt is read-only, so
    this is where the date a recording belongs to is preserved.
    """
    # The key has to be present for recordingDetails to reach the request's
    # part list, which videos.insert builds from this mapping's keys.
    body = build_body(options(recordingDate="2008-02-24"))
    assert body["recordingDetails"] == {"recordingDate": "2008-02-24T00:00:00Z"}


def test_a_recording_timestamp_is_left_as_given() -> None:
    """A caller that knows the time of day keeps it."""
    assert as_recording_timestamp("2008-02-24T10:30:00Z") == "2008-02-24T10:30:00Z"


def test_keywords_become_tags_only_when_given() -> None:
    """An empty keyword string is no tags, not one empty tag."""
    assert build_body(options())["snippet"]["tags"] is None
    assert build_body(options(keywords="설교,주일예배"))["snippet"]["tags"] == ["설교", "주일예배"]


class FakeChannels:
    """The channels() collection of a YouTube service, answering one canned reply."""

    def __init__(self, response: dict | None = None, error: Exception | None = None) -> None:
        """
        :param response: What ``execute()`` should return
        :param error: Raised by ``execute()`` instead, to stand in for an API refusal
        """
        self.response = response or {}
        self.error = error

    def list(self, **_kwargs) -> "FakeChannels":
        """
        Accept and ignore the request parameters.

        :return: Self, standing in for the request object
        """
        return self

    def execute(self) -> dict:
        """
        Answer the canned reply.

        :return: The canned response
        :raises Exception: The canned error, where one was given
        """
        if self.error:
            raise self.error
        return self.response


class FakeYouTube:
    """A YouTube service exposing only the collection the channel guard uses."""

    def __init__(self, channels: FakeChannels) -> None:
        """
        :param channels: The stand-in channels() collection
        """
        self._channels = channels

    def channels(self) -> FakeChannels:
        """
        :return: The stand-in channels() collection
        """
        return self._channels


def channel_service(channel_id: str, title: str) -> FakeYouTube:
    """
    Build a service whose credentials belong to one named channel.

    :param channel_id: The channel's id
    :param title: The channel's display name
    :return: The stand-in service
    """
    return FakeYouTube(FakeChannels({"items": [{"id": channel_id, "snippet": {"title": title}}]}))


@pytest.mark.parametrize("expected", ["UC_placeholder_channel_id", "예시교회"])
def test_the_right_channel_passes_by_id_or_title(expected) -> None:
    """
    Either form identifies the channel, since either is what someone has to hand.

    :param expected: The channel as the configuration names it
    """
    verify_channel(channel_service("UC_placeholder_channel_id", "예시교회"), expected)


def test_the_wrong_channel_aborts_before_anything_uploads() -> None:
    """
    A token minted against the wrong account is caught before the first byte.

    The channel is fixed at the consent screen and no later request can redirect
    an upload, so this is the only place the mistake is cheap to fix.
    """
    service = channel_service("UC_placeholder_channel_id", "예시교회")
    with pytest.raises(ValueError, match="예시교회"):
        verify_channel(service, "UC_some_other_channel")


def test_no_expected_channel_skips_the_check() -> None:
    """
    Nothing is read back when nothing said where the video should go.

    The stand-in would raise if it were consulted.
    """
    service = FakeYouTube(FakeChannels(error=AssertionError("channels() should not be consulted")))
    verify_channel(service, "")


def test_an_account_owning_no_channel_is_refused() -> None:
    """An account with nowhere to upload to fails plainly rather than at insert."""
    with pytest.raises(ValueError, match="no YouTube channel"):
        verify_channel(FakeYouTube(FakeChannels({"items": []})), "예시교회")


class FakeStatus:
    """The response half of an HttpError, carrying just the status the guard reads."""

    def __init__(self, status: int) -> None:
        """
        :param status: HTTP status code
        """
        self.status = status
        self.reason = "Forbidden"


def test_a_token_predating_the_read_scope_explains_itself() -> None:
    """
    Adding a scope does not invalidate a cached token; it just starts failing.

    Every existing token was granted before the read scope was asked for, so
    this is the first thing a working installation hits, and a bare 403 would
    not say what to do about it.
    """
    refused = HttpError(FakeStatus(403), b'{"error": {"message": "insufficient authentication scopes"}}')
    with pytest.raises(ValueError, match="Delete that file"):
        verify_channel(FakeYouTube(FakeChannels(error=refused)), "예시교회")


def test_another_api_failure_is_not_disguised_as_a_scope_problem() -> None:
    """A server error is a server error, and is left to the caller's retry logic."""
    with pytest.raises(HttpError):
        verify_channel(FakeYouTube(FakeChannels(error=HttpError(FakeStatus(500), b"{}"))), "예시교회")


def test_the_flag_wins_over_the_environment(monkeypatch) -> None:
    """
    An explicit channel overrides the ambient one.

    :param monkeypatch: Fixture used to set the environment variable
    """
    monkeypatch.setenv(CHANNEL_ENV_VAR, "예시교회")
    assert expected_channel(options(channel="UC_placeholder_channel_id")) == "UC_placeholder_channel_id"


def test_the_environment_supplies_the_channel_by_default(monkeypatch) -> None:
    """
    The variable already kept beside the other credentials is read.

    :param monkeypatch: Fixture used to set the environment variable
    """
    monkeypatch.setenv(CHANNEL_ENV_VAR, "예시교회")
    assert expected_channel(options(channel=None)) == "예시교회"


def test_neither_source_means_no_check(monkeypatch) -> None:
    """
    The guard stays optional, so an existing invocation keeps working unchanged.

    :param monkeypatch: Fixture used to clear the environment variable
    """
    monkeypatch.delenv(CHANNEL_ENV_VAR, raising=False)
    assert expected_channel(options(channel=None)) == ""
