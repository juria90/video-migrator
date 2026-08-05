#!/usr/bin/env python3
"""
HTTP Cache Manager

This module provides HTTP caching with support for Cache-Control headers,
conditional requests, and cache revalidation using ETag and Last-Modified headers.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path

import requests


def decode_body(response: requests.Response) -> str:
    """
    Decode a response body, sniffing the charset when the server omits one.

    ``requests`` falls back to ISO-8859-1 for a ``text/*`` response whose
    Content-Type carries no charset, as HTTP/1.1 once required. A CMS that
    declares UTF-8 only in a ``<meta>`` tag therefore decodes to mojibake, so
    sniff the body instead of trusting that default.

    :param response: The response to decode
    :return: The body as text
    """
    if response.encoding and "charset" not in response.headers.get("Content-Type", "").lower():
        response.encoding = response.apparent_encoding
    return response.text


class HTTPCache:
    """
    HTTP cache manager with support for Cache-Control and conditional requests.

    This class handles caching of HTTP responses with proper support for:
    - Cache-Control headers (max-age, must-revalidate, no-cache)
    - Conditional requests using ETag and Last-Modified
    - Cache revalidation with 304 Not Modified responses
    """

    def __init__(self, cache_dir: str | Path = ".cache", cache_duration: int = 43200):
        """
        Initialize the HTTP cache.

        :param cache_dir: Directory to store cached files
        :param cache_duration: Default cache duration in seconds (default: 43200 = 12 hours)
        """
        self.cache_dir = Path(cache_dir)
        self.cache_duration = cache_duration
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def get_cache_path(self, cache_key: str) -> Path:
        """
        Generate cache file path for a given cache key.

        :param cache_key: Unique identifier for the cached content
        :return: Path to the cache file
        """
        return self.cache_dir / f"{cache_key}.html"

    def get_metadata_path(self, cache_path: Path) -> Path:
        """
        Generate metadata file path for a given cache file.

        :param cache_path: Path to the cache file
        :return: Path to the metadata file
        """
        return cache_path.with_suffix(".meta.json")

    def is_cache_valid(self, cache_path: Path) -> bool:
        """
        Check if cached file exists and is still valid based on HTTP Cache-Control headers.

        :param cache_path: Path to the cache file
        :return: True if cache is valid, False otherwise
        """
        if not cache_path.exists():
            return False

        metadata_path = self.get_metadata_path(cache_path)
        if not metadata_path.exists():
            return False

        try:
            with open(metadata_path, encoding="utf-8") as f:
                metadata = json.load(f)

            # Check if cache has expired based on stored expiry time
            expiry_time = datetime.fromisoformat(metadata.get("expiry_time", ""))
            return datetime.now() < expiry_time
        except (json.JSONDecodeError, ValueError, KeyError):
            return False

    def get_metadata(self, cache_path: Path) -> dict | None:
        """
        Load cache metadata from file.

        :param cache_path: Path to the cache file
        :return: Metadata dictionary or None if not available
        """
        metadata_path = self.get_metadata_path(cache_path)
        if not metadata_path.exists():
            return None

        try:
            with open(metadata_path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            return None

    def should_revalidate(self, metadata: dict | None) -> bool:
        """
        Check if cache should be revalidated based on Cache-Control directives.

        :param metadata: Cache metadata dictionary
        :return: True if revalidation is needed, False otherwise
        """
        if not metadata:
            return False

        cache_control = metadata.get("cache_control", "")
        directives = [d.strip().lower() for d in cache_control.split(",")]

        # Check for must-revalidate or no-cache directives
        return "must-revalidate" in directives or "no-cache" in directives

    def fetch_with_cache(
        self,
        url: str,
        cache_key: str,
        session: requests.Session | None = None,
        data: dict[str, str] | None = None,
    ) -> tuple[str, bool]:
        """
        Fetch content with caching support and revalidation.

        Passing ``data`` turns the request into a POST. Conditional revalidation
        is skipped in that case: ETag and Last-Modified describe a URL, and a
        POST body is part of the request rather than of the URL, so a 304 would
        say nothing about the response actually wanted.

        :param url: URL to fetch
        :param cache_key: Unique cache key for this request; must distinguish
            POST bodies too, since they are not part of the URL
        :param session: Optional requests Session to use
        :param data: Form fields to POST. If None, the URL is fetched with GET
        :return: Tuple of (content, from_cache) where from_cache indicates if content was from cache
        :raises requests.RequestException: If the request fails
        """
        if session is None:
            session = requests.Session()

        cache_path = self.get_cache_path(cache_key)
        metadata = self.get_metadata(cache_path)
        revalidatable = data is None

        # Check if we have a valid cached version
        if self.is_cache_valid(cache_path):
            # Check if revalidation is required
            if revalidatable and self.should_revalidate(metadata):
                content = self.revalidate_cache(url, cache_path, metadata, session)
                if content is not None:
                    return content, True

            print(f"Using cached content: {cache_path.name}")
            with open(cache_path, encoding="utf-8") as f:
                return f.read(), True

        # If cache exists but expired, try revalidation
        if revalidatable and cache_path.exists() and metadata:
            content = self.revalidate_cache(url, cache_path, metadata, session)
            if content is not None:
                return content, True

        # Fetch fresh content
        print(f"Fetching content: {url}")
        response = session.get(url) if data is None else session.post(url, data=data)
        response.raise_for_status()

        # Save response with metadata
        self.save_cache(cache_path, url, response)

        return decode_body(response), False

    def revalidate_cache(self, url: str, cache_path: Path, metadata: dict, session: requests.Session) -> str | None:
        """
        Revalidate cached content using conditional requests (ETag/Last-Modified).

        :param url: URL to revalidate
        :param cache_path: Path to cached file
        :param metadata: Cache metadata dictionary
        :param session: Requests session to use
        :return: Content if revalidation successful, None if cache is stale
        """
        headers = {}

        # Add If-None-Match header if ETag is available
        etag = metadata.get("etag")
        if etag:
            headers["If-None-Match"] = etag

        # Add If-Modified-Since header if Last-Modified is available
        last_modified = metadata.get("last_modified")
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        if not headers:
            # No validation headers available, cannot revalidate
            return None

        try:
            print(f"Revalidating cached content: {cache_path.name}")
            response = session.get(url, headers=headers)

            if response.status_code == 304:
                # Not Modified - cache is still valid
                print(f"Cache validated (304 Not Modified): {cache_path.name}")

                # Update cache metadata with new expiry time
                cache_control = response.headers.get("Cache-Control", metadata.get("cache_control", ""))
                max_age = self.parse_max_age(cache_control)
                if max_age is None or self.cache_duration > 0:
                    max_age = self.cache_duration

                expiry_time = datetime.now() + timedelta(seconds=max_age)
                metadata.update(
                    {
                        "cached_at": datetime.now().isoformat(),
                        "expiry_time": expiry_time.isoformat(),
                        "cache_control": cache_control,
                        "max_age": max_age,
                    }
                )

                metadata_path = self.get_metadata_path(cache_path)
                with open(metadata_path, "w", encoding="utf-8") as f:
                    json.dump(metadata, f, indent=2)

                # Return cached content
                with open(cache_path, encoding="utf-8") as f:
                    return f.read()

            elif response.status_code == 200:
                # Content has changed, save new version
                print(f"Cache invalidated, fetched new content: {cache_path.name}")
                self.save_cache(cache_path, url, response)
                return decode_body(response)

            response.raise_for_status()
        except requests.RequestException as e:
            print(f"Revalidation failed: {e}, using cached version if available")
            if cache_path.exists():
                with open(cache_path, encoding="utf-8") as f:
                    return f.read()

        return None

    def save_cache(self, cache_path: Path, url: str, response: requests.Response) -> None:
        """
        Save response content and metadata to cache.

        :param cache_path: Path to cache file
        :param url: URL that was fetched
        :param response: Response object from requests
        """
        # Parse Cache-Control header to determine cache duration
        cache_control = response.headers.get("Cache-Control", "")
        max_age = self.parse_max_age(cache_control)

        # Use configured cache_duration if no max-age in header or if cache_duration is set
        if max_age is None or self.cache_duration > 0:
            max_age = self.cache_duration

        # Calculate expiry time
        expiry_time = datetime.now() + timedelta(seconds=max_age)

        # Save to cache
        with open(cache_path, "w", encoding="utf-8") as f:
            f.write(decode_body(response))

        # Save cache metadata including validation headers
        metadata_path = self.get_metadata_path(cache_path)
        metadata = {
            "url": url,
            "cached_at": datetime.now().isoformat(),
            "expiry_time": expiry_time.isoformat(),
            "cache_control": cache_control,
            "max_age": max_age,
            "etag": response.headers.get("ETag"),
            "last_modified": response.headers.get("Last-Modified"),
        }
        with open(metadata_path, "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2)

    def parse_max_age(self, cache_control: str) -> int | None:
        """
        Parse max-age directive from Cache-Control header.

        :param cache_control: Cache-Control header value
        :return: max-age value in seconds, or None if not found
        """
        if not cache_control:
            return None

        # Parse Cache-Control directives
        directives = [d.strip() for d in cache_control.split(",")]
        for directive in directives:
            if directive.startswith("max-age="):
                try:
                    return int(directive.split("=")[1])
                except (ValueError, IndexError):
                    return None

        return None

    def clear_cache(self, cache_key: str | None = None) -> None:
        """
        Clear cached content and metadata.

        :param cache_key: Specific cache key to clear, or None to clear all cache
        """
        if cache_key:
            cache_path = self.get_cache_path(cache_key)
            metadata_path = self.get_metadata_path(cache_path)
            cache_path.unlink(missing_ok=True)
            metadata_path.unlink(missing_ok=True)
        else:
            # Clear all cache files
            for file_path in self.cache_dir.glob("*"):
                file_path.unlink(missing_ok=True)
