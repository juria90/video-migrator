#!/usr/bin/env python3
"""
Fetch a recording's original file from Vimeo, through the API rather than the player.

The player route is the one every scraper reaches for first, and it does not
survive contact with this archive: the recordings are not public, so the embed
returns 401 without a session, and a browser session is both awkward to obtain
from WSL and expired by the time a migration of this length is half done.

The API asks for none of that. A personal access token is issued once, does not
lapse, and answers with links to the *original* uploads — the files as they were
delivered to Vimeo, not the transcodes the player streams. That matters here
beyond convenience: the artifacts this archive carries are judged on the master,
and a transcode has already resampled them.

The links themselves are short-lived and signed, so they are resolved one
recording at a time, immediately before the bytes are pulled.
"""

import argparse
import logging
import pathlib
import sys
from collections.abc import Iterator

import requests

from ..logs import Ticker

#: Where the API lives, and the representation this module is written against.
#: Vimeo versions its API by Accept header; pinning it means a later default
#: cannot quietly change the shape of a response this code reads.
API_ROOT = "https://api.vimeo.com"
API_VERSION = "application/vnd.vimeo.*+json;version=3.4"

#: The fields a download needs. Asking for the whole resource returns several
#: hundred keys, most of a recording's metadata among them, so the request names
#: what it uses and nothing else.
VIDEO_FIELDS = "uri,name,duration,created_time,download"

#: Where the token is read from, and under what name.
ENV_FILE = ".env"
ENV_TOKEN = "VIMEO_ACCESS_TOKEN"

#: What Vimeo calls the untouched upload, as opposed to a rendition it made.
SOURCE_QUALITY = "source"

#: How much of a response body to hold in memory at once.
CHUNK_BYTES = 1 << 20


logger = logging.getLogger(__name__)


def load_token(env_path: str = ENV_FILE) -> str:
    """
    Read the API token from the environment file.

    :param env_path: Path to the ``.env`` holding it
    :return: The token
    :raises SystemExit: If the file or the token is missing
    """
    path = pathlib.Path(env_path)
    if not path.exists():
        raise SystemExit(f"{env_path} does not exist; it holds {ENV_TOKEN}, and is not in the repository")

    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == ENV_TOKEN:
            return value.strip()
    raise SystemExit(f"{env_path} has no {ENV_TOKEN}")


def preferred_downloads(downloads: list[dict]) -> list[dict]:
    """
    Order a recording's download links by how much we want each.

    Vimeo offers the original upload alongside the renditions it derived from
    it. The original comes first — a rendition has been re-encoded, and on this
    archive that means a capture artifact has been resampled before anything
    gets the chance to measure it. The rest follow largest first, as the least
    re-processed of what is left.

    The order matters because the first choice is not always served. On this
    archive 122 recordings — every one of them published between December 2010
    and September 2013 — advertise an original and then answer the request for
    it with a redirect to nowhere.

    Every one of those falls back. The rendition carries the same frame size
    (640x480 throughout, bar a single 400x300) at about two thirds the bitrate,
    so what is lost is bitrate rather than resolution, and no recording in the
    archive is left unfetchable. Worth stating plainly because the word
    "rendition" suggests a downscale and here it is not one.

    :param downloads: The ``download`` array of a video resource
    :return: The entries, most wanted first

    >>> preferred_downloads([])
    []
    >>> [e["quality"] for e in preferred_downloads(
    ...     [{"quality": "hd", "size": 10}, {"quality": "source", "size": 4}])]
    ['source', 'hd']
    >>> [e["size"] for e in preferred_downloads(
    ...     [{"quality": "hd", "size": 10}, {"quality": "sd", "size": 40}])]
    [40, 10]
    """
    return sorted(
        downloads,
        key=lambda entry: (entry.get("quality") != SOURCE_QUALITY, -(entry.get("size") or 0)),
    )


def best_download(downloads: list[dict]) -> dict | None:
    """
    Choose which of a recording's download links to pull.

    :param downloads: The ``download`` array of a video resource
    :return: The entry to fetch, or None when the video offers none

    >>> best_download([])

    >>> best_download([{"quality": "hd", "size": 10}, {"quality": "source", "size": 4}])["quality"]
    'source'
    """
    ordered = preferred_downloads(downloads)
    return ordered[0] if ordered else None


def serves(link: str) -> bool:
    """
    Does a download link actually lead to a file?

    An unserved link is not an error: it answers 302, as a served one does, and
    differs only in redirecting to the empty string. Followed, that reads as an
    I/O error somewhere deep in whatever was going to read the file.

    :param link: A download link from the API
    :return: True when the redirect names somewhere to go
    """
    head = requests.head(link, allow_redirects=False, timeout=30)
    return bool(head.headers.get("location", ""))


class VimeoAPI:
    """A Vimeo API session, authenticated by a personal access token."""

    def __init__(self, token: str) -> None:
        """
        Open a session against the API.

        :param token: A personal access token carrying the ``private`` and
            ``video_files`` scopes; without the latter no download link is
            returned, however the account is subscribed
        """
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": API_VERSION,
        })

    def get(self, path: str, **params: str) -> dict:
        """
        Ask the API for one resource, reporting a refusal in its own words.

        Vimeo answers a rejected request with a ``developer_message`` saying
        which credential it took exception to. Raising the bare status instead
        would replace that with a traceback ending in ``401``, which is the same
        for a token that is missing, expired, wrong, or merely lacking a scope.

        :param path: Path under the API root, beginning with a slash
        :param params: Query parameters
        :return: The decoded resource
        :raises SystemExit: If the API refuses the request
        :raises requests.HTTPError: If it fails for any other reason
        """
        response = self.session.get(f"{API_ROOT}{path}", params=params, timeout=30)
        if response.status_code in (401, 403):
            body = response.json() if response.headers.get("content-type", "").startswith("application/") else {}
            raise SystemExit(
                f"Vimeo refused the request for {path}: {body.get('developer_message') or response.reason}\n"
                f"{ENV_TOKEN} must be a personal access token — 64 hexadecimal characters, generated under "
                f"'Personal Access Tokens' on the app's page — carrying the 'private' and 'video_files' scopes. "
                f"An app's client secret is a different, longer credential and is not accepted here."
            )
        response.raise_for_status()
        return response.json()

    def video(self, video_id: str) -> dict:
        """
        Read one recording's metadata.

        :param video_id: The numeric id, as the board records it
        :return: The video resource, holding only :data:`VIDEO_FIELDS`
        :raises SystemExit: If the API refuses the request
        :raises requests.HTTPError: If the recording is missing or not ours
        """
        return self.get(f"/videos/{video_id}", fields=VIDEO_FIELDS)

    def download(self, video_id: str, destination: pathlib.Path) -> pathlib.Path:
        """
        Pull one recording's original file to disk.

        Written to a ``.part`` file and moved into place only once the whole
        body has arrived, so an interrupted run leaves nothing that looks like a
        finished download. A link that has expired between being issued and
        being followed fails here rather than producing a truncated file.

        :param video_id: The numeric id, as the board records it
        :param destination: Where to write the file
        :return: The path written
        :raises RuntimeError: If the recording offers no download link, or the
            body that arrives is not the length that was promised. Raised
            rather than exited: a caller carrying several hundred recordings
            has to be able to record this one as failed and go on to the rest.
            The archive holds one 2012 recording Vimeo will not serve, and
            because the queue runs oldest first it was the first thing every
            run met — ``SystemExit`` there ended the run before it had fetched
            anything, three hundred fetchable recordings included.
        :raises requests.HTTPError: If the recording or the link is unreachable
        """
        offered = preferred_downloads(self.video(video_id).get("download") or [])
        if not offered:
            raise RuntimeError(
                f"Vimeo offers no download for {video_id}. The token needs the 'video_files' scope, "
                f"and the account's plan has to expose downloads at all."
            )
        chosen = next((entry for entry in offered if serves(entry["link"])), None)
        if chosen is None:
            raise RuntimeError(f"Vimeo lists {len(offered)} download(s) for {video_id} and serves none of them.")
        if chosen is not offered[0]:
            logger.info(f"  {video_id}: the original is listed but not served; taking the "
                  f"{chosen.get('width')}x{chosen.get('height')} {chosen.get('quality')} rendition instead")

        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_suffix(destination.suffix + ".part")
        expected = chosen.get("size")

        # Carry on from whatever a previous run managed. These are gigabytes
        # each over an hour-long run, so starting again from nothing after an
        # interruption is most of the cost of the interruption. Only ever
        # resumed short of the expected size: a part-file at or beyond it is
        # from some other rendition and cannot be continued.
        already = partial.stat().st_size if partial.exists() else 0
        if expected and already >= expected:
            already = 0
        headers = {"Range": f"bytes={already}-"} if already else {}

        # The link is pre-signed, so it carries its own authorization; sending
        # the token to a CDN that did not ask for it would leak it.
        with requests.get(chosen["link"], stream=True, timeout=60, headers=headers) as response:
            response.raise_for_status()
            # 206 means the range was honoured. Anything else means the whole
            # file is coming, whatever was asked for, so the part-file goes.
            resuming = already and response.status_code == 206
            if already and not resuming:
                already = 0
            ticker = Ticker()
            written_now = already
            with partial.open("ab" if resuming else "wb") as handle:
                for chunk in response.iter_content(CHUNK_BYTES):
                    handle.write(chunk)
                    written_now += len(chunk)
                    if ticker.due():
                        share = f" ({written_now / expected:.0%})" if expected else ""
                        logger.info("%s: %.0f MiB fetched%s", video_id, written_now / (1 << 20), share)

        written = partial.stat().st_size
        if expected and written != expected:
            partial.unlink()
            raise RuntimeError(f"{video_id}: expected {expected} bytes, received {written}; leaving nothing behind")
        if already:
            logger.info(f"  {video_id}: resumed at {already / (1 << 20):.0f} MiB of {written / (1 << 20):.0f}")

        partial.replace(destination)
        return destination


def iter_pages(api: VimeoAPI, path: str = "/me/videos") -> Iterator[dict]:
    """
    Walk every video the token can see, a page at a time.

    :param api: An authenticated session
    :param path: The collection to walk
    :return: Each video resource in turn
    """
    url = f"{API_ROOT}{path}"
    params = {"fields": VIDEO_FIELDS, "per_page": 100}
    while url:
        response = api.session.get(url, params=params, timeout=30)
        response.raise_for_status()
        body = response.json()
        yield from body.get("data", [])
        following = (body.get("paging") or {}).get("next")
        url = f"{API_ROOT}{following}" if following else ""
        params = {}


def main() -> int:
    """
    Fetch one recording, or report what the token can see.

    :return: 0 on success
    """
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("video_id", nargs="?", help="Numeric Vimeo id; omit to only check the token")
    parser.add_argument("-o", "--output", type=pathlib.Path, help="Where to write the file")
    parser.add_argument("--env", default=ENV_FILE, help=f"File holding {ENV_TOKEN} (default: {ENV_FILE})")
    args = parser.parse_args()

    api = VimeoAPI(load_token(args.env))

    if not args.video_id:
        me = api.get("/me", fields="name,account")
        logger.info(f"token works: account {me.get('name')!r}, plan {me.get('account')!r}")
        return 0

    video = api.video(args.video_id)
    downloads = video.get("download") or []
    logger.info(f"{args.video_id}: {video.get('duration')}s, {len(downloads)} download(s) offered")
    for entry in downloads:
        size = (entry.get("size") or 0) / (1 << 20)
        logger.info(f"   {entry.get('quality'):8} {entry.get('width')}x{entry.get('height')}  {size:8.1f} MiB")

    if args.output:
        logger.info(f"\nfetching -> {args.output}")
        api.download(args.video_id, args.output)
        logger.info(f"wrote {args.output.stat().st_size / (1 << 20):.1f} MiB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
