#!/usr/bin/env python3
"""Tests for the HTTP cache's decoding and POST support."""

import pytest
import requests

from video_migrator.utils.http_cache import HTTPCache, decode_body

KOREAN = "설교 제목, 부제"


def make_response(body: bytes, content_type: str) -> requests.Response:
    """
    Build a response with a given body and Content-Type, without a server.

    :param body: Raw response body
    :param content_type: Value of the Content-Type header
    :return: A response ready to be decoded
    """
    response = requests.Response()
    response.status_code = 200
    response._content = body
    response.headers["Content-Type"] = content_type
    return response


def test_decode_body_sniffs_when_the_server_omits_a_charset() -> None:
    """A UTF-8 page served as bare `text/html` must not decode as ISO-8859-1."""
    response = make_response(KOREAN.encode("utf-8"), "text/html")
    assert decode_body(response) == KOREAN


def test_decode_body_trusts_a_declared_charset() -> None:
    """A declared charset is authoritative and is not second-guessed."""
    response = make_response(KOREAN.encode("utf-8"), "text/xml; charset=utf-8")
    assert decode_body(response) == KOREAN


class RecordingSession:
    """A stand-in for requests.Session that records how it was called."""

    def __init__(self, body: str):
        """
        :param body: Body every response should carry
        """
        self.body = body
        self.calls: list[tuple[str, str, dict | None]] = []

    def get(self, url: str, headers: dict | None = None) -> requests.Response:
        """
        Record a GET and answer with the canned body.

        :param url: URL requested
        :param headers: Request headers, ignored
        :return: The canned response
        """
        self.calls.append(("GET", url, None))
        return make_response(self.body.encode("utf-8"), "text/html; charset=utf-8")

    def post(self, url: str, data: dict | None = None) -> requests.Response:
        """
        Record a POST and answer with the canned body.

        :param url: URL requested
        :param data: Form fields posted
        :return: The canned response
        """
        self.calls.append(("POST", url, data))
        return make_response(self.body.encode("utf-8"), "text/xml; charset=utf-8")


def test_fetch_with_cache_posts_when_given_form_data(tmp_path) -> None:
    """
    An endpoint that answers POST only is reached with POST, and cached.

    :param tmp_path: Pytest fixture supplying a scratch cache directory
    """
    cache = HTTPCache(tmp_path, cache_duration=3600)
    session = RecordingSession("<result/>")

    content, from_cache = cache.fetch_with_cache("https://example.org/vod", "vod_1", session, data={"num": "1"})
    assert content == "<result/>"
    assert from_cache is False
    assert session.calls == [("POST", "https://example.org/vod", {"num": "1"})]

    # The second read is served from disk: one POST reaches the site, not two.
    content, from_cache = cache.fetch_with_cache("https://example.org/vod", "vod_1", session, data={"num": "1"})
    assert content == "<result/>"
    assert from_cache is True
    assert len(session.calls) == 1


def test_fetch_with_cache_defaults_to_get(tmp_path) -> None:
    """
    Without form data the request stays a GET, as every existing caller expects.

    :param tmp_path: Pytest fixture supplying a scratch cache directory
    """
    cache = HTTPCache(tmp_path, cache_duration=3600)
    session = RecordingSession("<html/>")

    cache.fetch_with_cache("https://example.org/page", "page_1", session)
    assert session.calls == [("GET", "https://example.org/page", None)]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
