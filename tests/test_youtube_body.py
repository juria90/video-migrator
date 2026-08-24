#!/usr/bin/env python3
"""Tests for the resource the YouTube sink sends to videos.insert, and the channel guard."""

import argparse

import pytest
from googleapiclient.errors import HttpError

import video_migrator.sinks.youtube as youtube_module
from video_migrator.sinks.youtube import (
    CHANNEL_ENV_VAR,
    DEFAULT_MADE_FOR_KIDS,
    MAX_TITLE_LENGTH,
    REFUSED,
    as_recording_timestamp,
    build_body,
    confirm_upload,
    create_argument_parser,
    expected_channel,
    resumable_http,
    resumable_upload,
    upload_arguments,
    verify_channel,
    video_status,
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
        "madeForKids": DEFAULT_MADE_FOR_KIDS,
        "embeddable": "yes",
        "language": "",
    }
    return argparse.Namespace(**{**fields, **overrides})


def test_a_plain_upload_sends_no_publish_at() -> None:
    """
    An unscheduled upload must not carry a null publishAt it never asked for.

    The request's part is derived from the body's keys, so a field present but
    empty is a field the API is being told to apply.
    """
    body = build_body(options())
    assert body["status"]["privacyStatus"] == "public"
    assert "publishAt" not in body["status"]
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
    assert body["status"]["privacyStatus"] == "private"
    assert body["status"]["publishAt"] == "2026-12-31T23:59:00Z"


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
    with pytest.raises(ValueError, match="Delete it and run again"):
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


def test_every_argument_an_upload_builds_is_one_the_parser_accepts() -> None:
    """
    The two halves must agree, and only a test holds them together.

    A caller assembling this list by hand finds out an option is missing when a
    real upload of a real recording dies on the argument line, several stages
    into work that cannot be cheaply repeated.
    """
    arguments = upload_arguments(
        file="a.mp4", title="설교 제목", description="본문", privacy="unlisted",
        category=29, recording_date="2026-08-02", channel="예시교회",
        session_file="/tmp/session")
    parsed = create_argument_parser().parse_args(arguments)
    assert parsed.file == "a.mp4"
    assert parsed.privacyStatus == "unlisted"
    assert parsed.recordingDate == "2026-08-02"
    assert parsed.channel == "예시교회"
    assert parsed.session_file == "/tmp/session"


def test_an_upload_omits_what_it_has_nothing_for() -> None:
    """
    An empty value is left out rather than passed as an empty string.

    ``--recordingDate ''`` is not the same as no recording date: the first asks
    the API to record that the video belongs to no particular day.
    """
    arguments = upload_arguments(file="a.mp4", title="설교 제목")
    assert "--recordingDate" not in arguments
    assert "--channel" not in arguments
    assert "--session-file" not in arguments


def test_a_title_too_long_for_youtube_is_cut_before_it_is_refused() -> None:
    """
    YouTube rejects a title over 100 characters rather than truncating it.

    Cutting it here costs the tail of a title; not cutting it costs the upload.
    """
    arguments = upload_arguments(file="a.mp4", title="설교 제목 " * 30)
    assert len(arguments[arguments.index("--title") + 1]) == MAX_TITLE_LENGTH


def test_a_part_finished_upload_is_not_mistaken_for_a_redirect() -> None:
    """
    308 means "resume incomplete" here, and httplib2 would read it as a redirect.

    A resumable upload answers every chunk but the last with 308 and a Range
    header, and no Location. httplib2 counts 308 among its redirect codes, looks
    for the Location that is not there, and raises — so the upload fails on its
    second chunk, retries, and fails again the same way.

    It cannot be caught by uploading a small file: one request is never
    part-finished, so a single-chunk upload never sees a 308 at all.
    """
    http = resumable_http()
    assert 308 not in http.redirect_codes
    # Ordinary redirects are still followed; only 308 is handed back.
    assert {301, 302, 303, 307} <= http.redirect_codes


def test_every_upload_declares_whether_it_is_children_s_content() -> None:
    """
    YouTube requires the declaration, and silence defers to the channel.

    A channel that has never been asked declares nothing, so an upload that says
    nothing either is left to be decided by something nobody set. Sermons are
    not children's content, and saying so is not the same as not saying.
    """
    body = build_body(options(title="설교 제목"))
    assert body["status"]["selfDeclaredMadeForKids"] is False

    body = build_body(options(title="설교 제목", madeForKids="yes"))
    assert body["status"]["selfDeclaredMadeForKids"] is True


def test_a_language_is_stated_rather_than_guessed_at() -> None:
    """
    An unstated language is inferred, and that decides who the video reaches.

    The scrape records ISO 639-2/B, which YouTube does not take, so it is
    translated rather than passed through and quietly ignored.
    """
    body = build_body(options(title="설교 제목", language="kor"))
    assert body["snippet"]["defaultLanguage"] == "ko"
    assert body["snippet"]["defaultAudioLanguage"] == "ko"

    # A tag YouTube already understands is left alone.
    body = build_body(options(title="설교 제목", language="ko"))
    assert body["snippet"]["defaultLanguage"] == "ko"

    # Nothing said, nothing sent — rather than an empty string.
    assert "defaultLanguage" not in build_body(options(title="설교 제목"))["snippet"]


def test_every_upload_states_whether_it_may_be_embedded() -> None:
    """
    These recordings are embedded on the church's own pages, so it is required.

    Relying on YouTube's default would leave a requirement resting on something
    nobody set and anybody could change.
    """
    assert build_body(options(title="설교 제목"))["status"]["embeddable"] is True
    assert build_body(options(title="설교 제목", embeddable="no"))["status"]["embeddable"] is False


class FakeVideos:
    """A videos() resource that answers a scripted sequence of statuses."""

    def __init__(self, answers):
        """
        :param answers: One ``status`` dict per call, in order
        """
        self.answers = list(answers)
        self.asked = 0

    def list(self, **_kwargs):
        """
        :return: Something with an execute(), as the API client returns
        """
        answer = self.answers[min(self.asked, len(self.answers) - 1)]
        self.asked += 1
        return type("Request", (), {"execute": staticmethod(lambda: {"items": [answer]})})()


class FakeVideoApi:
    """Enough of the API client for the confirmation to talk to."""

    def __init__(self, answers):
        """
        :param answers: One ``status`` dict per call, in order
        """
        self._videos = FakeVideos(answers)

    def videos(self):
        """
        :return: The scripted videos resource
        """
        return self._videos


def test_a_refused_upload_is_reported_rather_than_recorded_as_done() -> None:
    """
    An insert answers before YouTube has looked at the file.

    A recording too long for an unverified channel is accepted, given an id, and
    refused minutes later. Trusting the insert records a success for a video
    nobody can watch — and a systematic fault sails through a whole batch.
    """
    youtube = FakeVideoApi([{"status": {"uploadStatus": "rejected", "rejectionReason": "tooLong"}}])
    assert confirm_upload(youtube, "aBcDeFgHiJk", attempts=3, seconds=0) == ("rejected", "tooLong")


def test_confirmation_stops_as_soon_as_the_answer_is_settled() -> None:
    """Once YouTube has accepted a video there is nothing left to wait for."""
    youtube = FakeVideoApi([
        {"status": {"uploadStatus": "uploaded"}, "processingDetails": {"processingStatus": "processing"}},
        {"status": {"uploadStatus": "processed"}, "processingDetails": {"processingStatus": "succeeded"}},
    ])
    assert confirm_upload(youtube, "aBcDeFgHiJk", attempts=5, seconds=0) == ("processed", "")
    assert youtube.videos().asked == 2


def test_still_processing_is_not_a_failure() -> None:
    """
    A long recording transcodes for an hour, and waiting that out helps nobody.

    The point of confirming is to catch a refusal, which arrives early; anything
    still in progress after that is accepted and left to finish on its own.
    """
    youtube = FakeVideoApi([{"status": {"uploadStatus": "uploaded"},
                            "processingDetails": {"processingStatus": "processing"}}])
    status, reason = confirm_upload(youtube, "aBcDeFgHiJk", attempts=2, seconds=0)
    assert status not in REFUSED and reason == ""


def test_a_video_youtube_does_not_have_is_an_error() -> None:
    """An id that resolves to nothing means something has gone wrong upstream."""
    youtube = FakeVideoApi([])
    youtube._videos.answers = []
    youtube._videos.list = lambda **_: type("R", (), {"execute": staticmethod(lambda: {"items": []})})()
    with pytest.raises(ValueError, match="does not have"):
        video_status(youtube, "aBcDeFgHiJk")


def test_a_second_upload_in_one_run_can_still_build_its_arguments() -> None:
    """
    Every call returns its own parser, and a batch calls it once per recording.

    oauth2client exposes a module-level parser, and adding options to *that* one
    works exactly once — the second call raises ``conflicting option string:
    --file``. A run uploading a single recording never sees it. A batch sees it
    on the second recording, after the first has already been encoded, and on
    every recording after that.
    """
    first = create_argument_parser().parse_args(["--file", "one.mp4"])
    second = create_argument_parser().parse_args(["--file", "two.mp4"])
    third = create_argument_parser().parse_args(["--file", "three.mp4"])
    assert [first.file, second.file, third.file] == ["one.mp4", "two.mp4", "three.mp4"]
    # The OAuth flags the consent flow needs are still there, borrowed as a parent.
    assert hasattr(first, "noauth_local_webserver")


def test_arguments_survive_the_round_trip_on_a_repeated_call() -> None:
    """
    The bug showed as a parser error, so the parser is what a test must exercise.

    Building the arguments and parsing them is what every upload does; doing it
    twice is what a batch does.
    """
    for name in ("first.mp4", "second.mp4"):
        parsed = create_argument_parser().parse_args(
            upload_arguments(file=name, title="설교 제목", language="kor", session_file="/tmp/s"))
        assert parsed.file == name
        assert parsed.session_file == "/tmp/s"
        assert build_body(parsed)["snippet"]["defaultLanguage"] == "ko"


def test_an_upload_session_is_remembered_even_when_the_chunk_fails(tmp_path, monkeypatch) -> None:
    """
    The first request creates the video; recording it only on success loses it.

    A run that dies during that first chunk leaves a video on YouTube that the
    next run cannot find, so it uploads another. Two abandoned copies of one
    sermon is how this came to be in a ``finally``.

    :param tmp_path: Fixture supplying a directory to remember the session in
    :param monkeypatch: Fixture for removing the retry backoff, which would
        otherwise make this test sleep for several minutes
    """
    monkeypatch.setattr(youtube_module.time, "sleep", lambda _seconds: None)
    session = tmp_path / "a.upload-session"

    class DyingRequest:
        """A request that establishes a session and then fails mid-chunk."""

        resumable_uri = "https://upload.example.org/session/abc"
        resumable_progress = 0

        def next_chunk(self):
            """
            :raises OSError: As a dropped connection would
            """
            raise OSError("the connection went away")

    # RuntimeError rather than SystemExit: a caller carrying several hundred
    # recordings must be able to record this one as failed and go on.
    with pytest.raises(RuntimeError, match="gave up after"):
        resumable_upload(DyingRequest(), session)

    assert session.exists(), "the session must survive the failure that created it"
    assert session.read_text(encoding="utf-8") == "https://upload.example.org/session/abc"


def test_a_finished_upload_forgets_its_session(tmp_path) -> None:
    """
    A session left behind would be rejoined by a later run for a different file.

    :param tmp_path: Fixture supplying a directory to remember the session in
    """
    session = tmp_path / "a.upload-session"
    session.write_text("https://upload.example.org/session/abc", encoding="utf-8")

    class FinishingRequest:
        """A request that completes on its first chunk."""

        resumable_uri = "https://upload.example.org/session/abc"
        resumable_progress = 0

        def next_chunk(self):
            """
            :return: No status, and the finished video
            """
            return None, {"id": "aBcDeFgHiJk"}

    assert resumable_upload(FinishingRequest(), session) == "aBcDeFgHiJk"
    assert not session.exists()
