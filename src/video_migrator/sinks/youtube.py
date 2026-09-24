#!/usr/bin/env python3
"""
YouTube destination platform.

Uploads videos to YouTube using the YouTube Data API v3, with resumable
uploads and exponential backoff so a large back-catalogue migration survives
transient network failures.
"""

import argparse
import http.client
import logging
import os
import pathlib
import random
import re
import sys
import time

import httplib2
from googleapiclient.discovery import Resource, build
from googleapiclient.errors import HttpError
from googleapiclient.http import HttpRequest, MediaFileUpload
from oauth2client.client import HttpAccessTokenRefreshError, flow_from_clientsecrets
from oauth2client.file import Storage
from oauth2client.tools import argparser as oauth_argparser
from oauth2client.tools import run_flow

from ..logs import configure
from ..metadata.language import LANGUAGE_TAGS

# Explicitly tell the underlying HTTP transport library not to retry, since
# we are handling retry logic ourselves.
httplib2.RETRIES = 1

# Maximum number of times to retry before giving up.
MAX_RETRIES = 10

# Always retry when these exceptions are raised.
RETRIABLE_EXCEPTIONS = (
    httplib2.HttpLib2Error,
    IOError,
    http.client.NotConnected,
    http.client.IncompleteRead,
    http.client.ImproperConnectionState,
    http.client.CannotSendRequest,
    http.client.CannotSendHeader,
    http.client.ResponseNotReady,
    http.client.BadStatusLine,
)

# Always retry when an apiclient.errors.HttpError with one of these status
# codes is raised.
RETRIABLE_STATUS_CODES = [500, 502, 503, 504]

# The CLIENT_SECRETS_FILE variable specifies the name of a file that contains
# the OAuth 2.0 information for this application, including its client_id and
# client_secret. You can acquire an OAuth 2.0 client ID and client secret from
# the Google API Console at
# https://console.cloud.google.com/.
# Please ensure that you have enabled the YouTube Data API for your project.
# For more information about using OAuth2 to access the YouTube Data API, see:
#   https://developers.google.com/youtube/v3/guides/authentication
# For more information about the client_secrets.json file format, see:
#   https://developers.google.com/api-client-library/python/guide/aaa_client_secrets


logger = logging.getLogger(__name__)


def _find_client_secrets_file() -> str:
    """
    Find client_secrets.json in multiple possible locations.

    Searches in order:
    1. Environment variable VIDEO_MIGRATOR_CLIENT_SECRETS
       (VIDEO_SCRAPER_CLIENT_SECRETS is still honored for backwards compatibility)
    2. Current working directory
    3. config/ subdirectory of current working directory
    4. User's config directory (~/.config/video-migrator/)

    :return: Absolute path to client_secrets.json
    """
    for env_var in ("VIDEO_MIGRATOR_CLIENT_SECRETS", "VIDEO_SCRAPER_CLIENT_SECRETS"):
        if env_var in os.environ:
            return os.path.abspath(os.environ[env_var])

    search_paths = [
        os.path.join(os.getcwd(), "client_secrets.json"),
        os.path.join(os.getcwd(), "config", "client_secrets.json"),
        os.path.expanduser("~/.config/video-migrator/client_secrets.json"),
    ]

    for path in search_paths:
        if os.path.exists(path):
            return os.path.abspath(path)

    # Default to config/client_secrets.json in current directory
    return os.path.abspath(os.path.join(os.getcwd(), "config", "client_secrets.json"))


CLIENT_SECRETS_FILE = _find_client_secrets_file()

# This OAuth 2.0 access scope allows an application to upload files to the
# authenticated user's YouTube channel, but doesn't allow other types of access.
YOUTUBE_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"

# Reading back which channel the credentials belong to is a read, which the
# upload scope alone does not permit. It is requested so that a migration can
# check where it is about to put several hundred videos.
YOUTUBE_READONLY_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"

# Editing a video that is already published — which is how a title format
# settled on after the fact reaches the back catalogue — is neither an upload
# nor a read, and neither scope above permits it.
YOUTUBE_MANAGE_SCOPE = "https://www.googleapis.com/auth/youtube"

YOUTUBE_SCOPES = (YOUTUBE_UPLOAD_SCOPE, YOUTUBE_READONLY_SCOPE, YOUTUBE_MANAGE_SCOPE)

YOUTUBE_API_SERVICE_NAME = "youtube"
YOUTUBE_API_VERSION = "v3"

#: Environment variable naming the channel uploads are expected to land on.
CHANNEL_ENV_VAR = "YOUTUBE_CHANNEL"

# This variable defines a message to display if the CLIENT_SECRETS_FILE is
# missing.
MISSING_CLIENT_SECRETS_MESSAGE = f"""
WARNING: Please configure OAuth 2.0

To make this sample run you will need to populate the client_secrets.json file
found at:

   {CLIENT_SECRETS_FILE}

with information from the API Console
https://console.cloud.google.com/

For more information about the client_secrets.json file format, please visit:
https://developers.google.com/api-client-library/python/guide/aaa_client_secrets
"""

#: How much of a video is sent per request.
#:
#: The obvious value is -1, meaning the whole file in one request, and the
#: Google sample recommends it. It is wrong for a migration: the request cannot
#: report progress, and a process that dies mid-upload leaves nothing to resume
#: from, so a gigabyte already sent is sent again. A finite chunk costs a few
#: extra round trips and makes an interrupted upload cost a chunk rather than a
#: file. Must be a multiple of 256 KiB.
UPLOAD_CHUNK_BYTES = 16 * 1024 * 1024

#: How often to say how far an upload has got. Every chunk is far too often — a
#: gigabyte in 16 MiB pieces is sixty-odd lines of nearly the same number — and
#: each report is its own timestamped line rather than one that overwrites
#: itself, so that afterwards it is possible to see where the time went.
PROGRESS_SECONDS = 30

#: The longest title YouTube accepts. A longer one is refused outright rather
#: than truncated, so it is cut here where the reason is visible.
MAX_TITLE_LENGTH = 100

VALID_PRIVACY_STATUSES = ("public", "private", "unlisted")

#: Whether a video is children's content, which YouTube requires every upload to
#: declare. Left undeclared it falls back to whatever the channel says, and a
#: channel that has never been asked says nothing — so it is stated here. A
#: sermon is not children's content: declaring it so would strip comments, end
#: screens and personalized recommendations from every recording.
DEFAULT_MADE_FOR_KIDS = "no"

#: The only privacy status a scheduled publication is accepted from. A video
#: given a publishAt while public or unlisted is rejected outright, since
#: scheduling means "private until then".
SCHEDULABLE_PRIVACY_STATUS = "private"

#: Time of day given to a recording date supplied as a bare ``YYYY-MM-DD``.
#: recordingDetails.recordingDate wants a full RFC 3339 timestamp.
RECORDING_TIME_OF_DAY = "T00:00:00Z"

# YouTube video categories
# Source: https://gist.github.com/dgp/1b24bf2961521bd75d6c
VIDEO_CATEGORIES = {
    1: "Film & Animation",
    2: "Autos & Vehicles",
    10: "Music",
    15: "Pets & Animals",
    17: "Sports",
    19: "Travel & Events",
    20: "Gaming",
    22: "People & Blogs",
    23: "Comedy",
    24: "Entertainment",
    25: "News & Politics",
    26: "Howto & Style",
    27: "Education",
    28: "Science & Technology",
    29: "Nonprofits & Activism",
}


def credentials_path(channel: str = "") -> str:
    """
    Where the OAuth token granted by the consent flow is cached.

    Kept beside ``client_secrets.json``, which is to say under ``config/``,
    which is gitignored: the token is a credential in its own right, and one
    that stays valid.

    One token authorizes one channel — which channel is settled at the consent
    screen and no later request can override it — so a migration publishing to
    more than one destination holds more than one token, and they cannot share a
    filename. Consenting for the second channel would otherwise overwrite the
    first, and the next batch would upload somewhere nobody chose.

    It is deliberately not derived from how the program was invoked. Consenting
    is a manual step, and a path that moved with ``sys.argv[0]`` meant
    ``python -m video_migrator.sinks.youtube`` and ``python src/…/youtube.py``
    cached to different files. Over a migration measured in months that reads as
    the token having expired, and the fix looks like consenting again rather
    than like a path bug.

    :param channel: The channel the token is for, empty for the default one
    :return: Path to the token file

    >>> credentials_path().endswith("youtube-oauth2.json")
    True
    >>> credentials_path("UC_x5XG1OV2P6uZZ5FSM9Ttw").endswith(
    ...     "youtube-oauth2-UC_x5XG1OV2P6uZZ5FSM9Ttw.json")
    True
    >>> credentials_path("New Life Church / 설교").endswith("youtube-oauth2-New-Life-Church-설교.json")
    True
    """
    suffix = re.sub(r"[^\w.-]+", "-", channel).strip("-")
    name = f"youtube-oauth2-{suffix}.json" if suffix else "youtube-oauth2.json"
    return os.path.join(os.path.dirname(CLIENT_SECRETS_FILE), name)


def resumable_http() -> httplib2.Http:
    """
    An HTTP client that will not mistake a part-finished upload for a redirect.

    A resumable upload answers each chunk but the last with ``308 Resume
    Incomplete``, carrying a ``Range`` header saying how much arrived. httplib2
    counts 308 among its redirect codes and so looks for a ``Location`` header
    that a resumable upload never sends, raising instead of returning the
    response the API client is waiting to read.

    It never came up while the whole file went in one request, because a single
    request is never part-finished. Sending in chunks — which is what makes an
    interrupted upload resumable — is what makes 308 arrive at all.

    ``googleapiclient.http.build_http`` does exactly this, and is bypassed here
    because the client has to be built around credentials.

    :return: The client, with 308 left to the API client to interpret
    """
    http = httplib2.Http()
    http.redirect_codes = http.redirect_codes - {308}
    return http


def existing_credentials(channel: str = "") -> str:
    """
    Find the token to use, preferring the one named but settling for the one there is.

    The path a token is stored under is derived from the channel asked for, so a
    run that names no channel looks for a differently-named file and finds
    nothing — and consenting is not a cheap thing to do by accident. It opens a
    browser, it asks a person to pick a channel correctly, and picking wrong
    binds every upload after it.

    So a run that names no channel uses the one token that exists, if exactly
    one does. Several, and it has to be told which: guessing there would mean
    guessing which channel to publish to.

    :param channel: The channel a token is wanted for, empty if unspecified
    :return: Path to the token to use, which may not exist yet
    :raises SystemExit: If no channel was named and several tokens exist
    """
    named = credentials_path(channel)
    if channel or os.path.exists(named):
        return named

    directory = os.path.dirname(CLIENT_SECRETS_FILE)
    found = sorted(
        os.path.join(directory, name)
        for name in os.listdir(directory)
        if name.startswith("youtube-oauth2") and name.endswith(".json")
    ) if os.path.isdir(directory) else []

    if len(found) == 1:
        # Worth saying out loud. The token decides which channel is published
        # to, and this branch is reached precisely when nobody said which — so
        # a token left behind for another channel is otherwise picked up in
        # silence, and the first sign of it is a video on the wrong channel.
        logger.info(f"no channel was named and {named} does not exist; using the one token there is, {found[0]}")
        return found[0]
    if len(found) > 1:
        raise SystemExit(
            "Several channels have been consented to and none was asked for:\n  "
            + "\n  ".join(found)
            + f"\nName one with --channel or ${CHANNEL_ENV_VAR}, so that this uploads where you mean it to."
        )
    return named


def refreshed(credentials) -> bool:
    """
    Try to put a stale access token back in date, and say whether it worked.

    A refresh token that has been revoked, or has gone unused long enough to
    expire, loads exactly like a working one: ``invalid`` is False, because
    nothing has asked Google about it yet. The refusal arrives at the first real
    request instead, as an exception from inside the transport — which is how a
    credential that simply needs consenting again comes to look like a failure
    of whatever the run was doing at the time.

    Asking here turns that into a question with an answer.

    :param credentials: The credentials to refresh, updated in place on success
    :return: Whether the token can still be refreshed
    """
    try:
        credentials.refresh(resumable_http())
    except HttpAccessTokenRefreshError as exc:
        logger.info(f"the cached token can no longer be refreshed ({exc}); consenting again")
        return False
    return True


def consented(flow, storage, args: argparse.Namespace):
    """
    Consent, without letting the flow reset this run's logging.

    ``oauth2client.tools.run_flow`` ends by setting the *root* logger to its own
    ``--logging_level``, which defaults to ``ERROR``. Nothing here ever passes
    that flag, so consenting part way through a migration silences every later
    line of a run that then carries on working for hours: an upload's progress, a
    transcription's, the warning when a video could not be confirmed. The run
    reads as having hung moments after saying ``Authentication successful.``,
    which is the one thing it has not done.

    The level is restored rather than forced, so a run asked for ``--verbose``
    keeps what it asked for.

    :param flow: The consent flow to run
    :param storage: Where the credentials it obtains are cached
    :param args: Command-line arguments, carrying oauth2client's own flags
    :return: The credentials consented to
    """
    root = logging.getLogger()
    level = root.level
    try:
        return run_flow(flow, storage, args)
    finally:
        root.setLevel(level)


def get_authenticated_service(args: argparse.Namespace) -> Resource:
    """
    Authenticate and build the YouTube service.

    The channel an upload lands on is fixed here rather than by any later
    request: the token this returns is bound to whichever account, or Brand
    Account, was chosen at the consent screen, and ``videos.insert`` has no say
    in it. :func:`verify_channel` is the check that the right one was chosen.

    :param args: Command-line arguments containing OAuth configuration
    :return: Authenticated YouTube service object
    """
    flow = flow_from_clientsecrets(
        CLIENT_SECRETS_FILE,
        scope=" ".join(YOUTUBE_SCOPES),
        message=MISSING_CLIENT_SECRETS_MESSAGE,
    )

    storage = Storage(existing_credentials(expected_channel(args)))
    credentials = storage.get()

    if credentials is not None and not credentials.invalid:
        # Two ways a token that loads cleanly is still no use, neither of which
        # sets ``invalid``. Both used to surface as a traceback from the first
        # request, several stages into a run, reading like a fault in the
        # request rather than in the credential behind it.
        missing = set(YOUTUBE_SCOPES) - set(credentials.scopes or ())
        if missing:
            logger.info(f"the cached token was granted before {', '.join(sorted(missing))} "
                        f"was asked for, so it cannot do everything this run needs; consenting again")
            credentials = None
        elif credentials.access_token_expired and not refreshed(credentials):
            credentials = None

    if credentials is None or credentials.invalid:
        credentials = consented(flow, storage, args)

    return build(
        YOUTUBE_API_SERVICE_NAME,
        YOUTUBE_API_VERSION,
        http=credentials.authorize(resumable_http()),
    )


def expected_channel(options: argparse.Namespace) -> str:
    """
    The channel uploads are meant to land on, if anything says.

    Stripped, because this usually arrives from a hand-edited ``.env`` read by a
    shell. Such a file written on Windows ends its lines with a carriage return,
    which survives ``cut`` and turns a correct configuration into a channel name
    that matches nothing — reported as belonging to the wrong channel, which is
    alarming and wrong.

    :param options: Options that may carry ``--channel``
    :return: The channel id or title to insist on, empty to skip the check

    >>> import argparse
    >>> expected_channel(argparse.Namespace(channel="a channel" + chr(13)))
    'a channel'
    """
    return (getattr(options, "channel", None) or os.environ.get(CHANNEL_ENV_VAR, "")).strip()


def authenticated_channel(youtube: Resource) -> tuple[str, str]:
    """
    Read back which channel the credentials actually belong to.

    :param youtube: Authenticated YouTube service object
    :return: The channel's id and its title
    :raises ValueError: If the credentials own no channel, or predate the read
        scope that this needs
    """
    try:
        response = youtube.channels().list(part="snippet", mine=True).execute()
    except HttpError as e:
        if e.resp.status != 403:
            raise
        raise ValueError(
            f"Cannot read back which channel these credentials belong to. The cached token under "
            f"{os.path.dirname(CLIENT_SECRETS_FILE)} was granted before {YOUTUBE_READONLY_SCOPE} "
            f"was asked for. Delete it and run again to consent afresh."
        ) from e

    items = response.get("items") or []
    if not items:
        raise ValueError("The authenticated account owns no YouTube channel to upload to.")
    return items[0]["id"], items[0]["snippet"]["title"]


def verify_channel(youtube: Resource, expected: str) -> None:
    """
    Refuse to upload unless the credentials belong to the expected channel.

    Which channel an upload lands on is decided at the consent screen and cannot
    be corrected afterwards, so a token minted against the wrong account sends
    every video of a migration somewhere that has to be emptied by hand. One
    read before the first byte moves is worth that.

    Either the channel's id or its title satisfies the check. An id is the
    reliable form: a title is a display name, and renaming the channel would
    turn a correct configuration into a failing one.

    :param youtube: Authenticated YouTube service object
    :param expected: Channel id or title to insist on, empty to skip the check
    :raises ValueError: If the credentials belong to some other channel
    """
    if not expected:
        return

    channel_id, title = authenticated_channel(youtube)
    if expected not in (channel_id, title):
        raise ValueError(
            f"These credentials belong to channel {title!r} (id {channel_id}), not {expected!r}. "
            f"Delete {credentials_path(expected)} and consent again as the right account, or correct "
            f"--channel / ${CHANNEL_ENV_VAR}."
        )


def as_recording_timestamp(recording_date: str) -> str:
    """
    Widen a recording date to the timestamp the API asks for.

    :param recording_date: Date as ``YYYY-MM-DD``, or a full RFC 3339 timestamp
    :return: The timestamp to send

    >>> as_recording_timestamp("2026-08-02")
    '2026-08-02T00:00:00Z'
    >>> as_recording_timestamp("2026-08-02T10:30:00Z")
    '2026-08-02T10:30:00Z'
    """
    return recording_date if "T" in recording_date else recording_date + RECORDING_TIME_OF_DAY


def build_body(options: argparse.Namespace) -> dict:
    """
    Assemble the resource describing the video being inserted.

    Only the parts actually being set are included, since the request's ``part``
    is derived from this mapping's keys: sending an empty ``recordingDetails``
    would ask the API to clear it.

    ``recordingDate`` is where the date a recording belongs to goes. It is the
    one date the API will accept in the past — ``publishAt`` schedules forwards
    only, and ``snippet.publishedAt`` is read-only and set to the moment the
    video goes public.

    :param options: Options carrying the video metadata
    :return: The request body
    :raises ValueError: If a publication is scheduled from a privacy status
        other than private, which the API rejects
    """
    tags = None
    if options.keywords:
        tags = options.keywords.split(",")

    status = {"privacyStatus": options.privacyStatus}
    status["selfDeclaredMadeForKids"] = getattr(options, "madeForKids", DEFAULT_MADE_FOR_KIDS) == "yes"
    # Stated either way rather than only when refused. These recordings are
    # embedded on the church's own pages, so whether a video may be embedded is
    # a requirement here, not a default worth inheriting from somewhere else.
    status["embeddable"] = getattr(options, "embeddable", "yes") == "yes"
    publish_at = getattr(options, "publishAt", None)
    if publish_at:
        if options.privacyStatus != SCHEDULABLE_PRIVACY_STATUS:
            raise ValueError(
                f"--publishAt schedules a video to become public later, so it is only accepted on a "
                f"{SCHEDULABLE_PRIVACY_STATUS!r} video; got {options.privacyStatus!r}. "
                f"Pass --privacyStatus {SCHEDULABLE_PRIVACY_STATUS}, or drop --publishAt."
            )
        status["publishAt"] = publish_at

    snippet = {
        "title": options.title,
        "description": options.description,
        "tags": tags,
        "categoryId": str(options.category),
    }
    spoken = getattr(options, "language", "") or ""
    if spoken:
        tag = LANGUAGE_TAGS.get(spoken, spoken)
        snippet["defaultLanguage"] = tag
        snippet["defaultAudioLanguage"] = tag

    body = {"snippet": snippet, "status": status}

    recording_date = getattr(options, "recordingDate", None)
    if recording_date:
        body["recordingDetails"] = {"recordingDate": as_recording_timestamp(recording_date)}
    return body


def initialize_upload(youtube: Resource, options: argparse.Namespace,
                      session_path: pathlib.Path | None = None) -> str | None:
    """
    Initialize and start the video upload process.

    :param youtube: Authenticated YouTube service object
    :param options: Command-line options containing video metadata
    :param session_path: Where to remember the upload session, so that a run
        interrupted part way can rejoin it rather than start again
    :return: The id of the video created, or None if the API returned none
    :raises ValueError: If the metadata combination is one the API rejects
    """
    body = build_body(options)

    # Call the API's videos.insert method to create and upload the video.
    # https://developers.google.com/youtube/v3/docs/videos/insert
    insert_request = youtube.videos().insert(
        part=",".join(body.keys()),
        body=body,
        # The chunksize parameter specifies the size of each chunk of data, in
        # bytes, that will be uploaded at a time. Set a higher value for
        # reliable connections as fewer chunks lead to faster uploads. Set a lower
        # value for better recovery on less reliable connections.
        #
        # Setting "chunksize" equal to -1 in the code below means that the entire
        # file will be uploaded in a single HTTP request. (If the upload fails,
        # it will still be retried where it left off.) This is usually a best
        # practice, but if you're using Python older than 2.6 or if you're
        # running on App Engine, you should set the chunksize to something like
        # 1024 * 1024 (1 megabyte).
        media_body=MediaFileUpload(options.file, chunksize=UPLOAD_CHUNK_BYTES, resumable=True),
    )

    return resumable_upload(insert_request, session_path)


def resumable_upload(insert_request: HttpRequest, session_path: pathlib.Path | None = None) -> str | None:
    """
    Send a video, surviving both a dropped connection and a dead process.

    Two different interruptions have to be survived here, and they need
    different answers. A transient error mid-upload is retried with a backoff,
    which the API client handles as long as the process is alive. A process that
    dies takes the upload session with it — unless the session's address was
    written down, which is what ``session_path`` is for.

    A remembered session that the server no longer recognises is not an error:
    sessions expire, and starting again is the correct response. So a refusal to
    resume falls back to a fresh upload rather than failing the recording.

    :param insert_request: YouTube API insert request object
    :param session_path: Where to remember the session, and where to look for
        one to rejoin
    :return: The id of the video created, or None if the API returned none
    """
    if session_path is not None and session_path.exists():
        insert_request.resumable_uri = session_path.read_text(encoding="utf-8").strip()
        logger.info(f"rejoining the upload session remembered in {session_path}")

    response = None
    error = None
    retry = 0
    reported = 0.0
    while response is None:
        try:
            try:
                status, response = insert_request.next_chunk()
            finally:
                # Written whether or not the chunk succeeded. The session, and
                # with it the video resource on YouTube, is created by the first
                # request; recording it only on success means a run that dies
                # during that first chunk leaves a video behind that the next run
                # cannot find and so uploads again. Two abandoned copies of a
                # sermon is how this came to be in a finally.
                if session_path is not None and insert_request.resumable_uri:
                    session_path.parent.mkdir(parents=True, exist_ok=True)
                    session_path.write_text(insert_request.resumable_uri, encoding="utf-8")
            if status is not None and time.monotonic() - reported >= PROGRESS_SECONDS:
                logger.info("%5.1f%% sent", status.progress() * 100)
                reported = time.monotonic()
            if response is not None:
                if "id" in response:
                    logger.info("uploaded as video %s", response["id"])
                    if session_path is not None:
                        session_path.unlink(missing_ok=True)
                    return response["id"]
                else:
                    raise RuntimeError(f"the upload failed with an unexpected response: {response}")
        except HttpError as e:
            # A session the server has forgotten cannot be rejoined, but the
            # video can still be sent. Forget it too and begin again.
            if e.resp.status in (404, 410) and insert_request.resumable_uri:
                logger.info("the remembered upload session has expired; starting the upload again")
                insert_request.resumable_uri = None
                insert_request.resumable_progress = 0
                if session_path is not None:
                    session_path.unlink(missing_ok=True)
                continue
            if e.resp.status in RETRIABLE_STATUS_CODES:
                error = f"A retriable HTTP error {e.resp.status} occurred:\n{e.content}"
            else:
                raise
        except RETRIABLE_EXCEPTIONS as e:
            error = f"A retriable error occurred: {e}"

        if error is not None:
            logger.warning("%s", error)
            retry += 1
            if retry > MAX_RETRIES:
                # Raised rather than exited. A caller carrying several hundred
                # recordings has to be able to record this one as failed and
                # decide for itself whether to go on; SystemExit is not an
                # Exception, so it goes straight past any handler and takes the
                # whole run with it.
                raise RuntimeError(f"gave up after {MAX_RETRIES} retries: {error}")

            max_sleep = 2**retry
            sleep_seconds = random.random() * max_sleep
            logger.info("retrying in %.1f seconds", sleep_seconds)
            time.sleep(sleep_seconds)


def create_argument_parser() -> argparse.ArgumentParser:
    """
    Create and configure the argument parser for the CLI.

    A *new* parser every time, borrowing oauth2client's flags as a parent rather
    than adding to its module-level one. Adding to that singleton works exactly
    once: the second call raises ``conflicting option string: --file``, which
    never happens while a run uploads a single recording and happens on the
    second recording of every batch.

    :return: Configured argument parser
    """
    argparser = argparse.ArgumentParser(parents=[oauth_argparser], add_help=True)
    argparser.add_argument("--file", help="Video file to upload; not needed with --verify-only")
    argparser.add_argument(
        "--verify-only", action="store_true",
        help="Consent if needed, report which channel the credentials belong to, and upload nothing. "
             "Worth running once before a migration: which channel an upload lands on is decided at "
             "the consent screen and cannot be corrected afterwards.")
    argparser.add_argument(
        "--madeForKids", choices=("yes", "no"), default=DEFAULT_MADE_FOR_KIDS,
        help=f"Whether this is children's content, which YouTube requires every upload to declare "
             f"(default: {DEFAULT_MADE_FOR_KIDS})")
    argparser.add_argument(
        "--embeddable", choices=("yes", "no"), default="yes",
        help="Whether other sites may embed the video (default: yes)")
    argparser.add_argument(
        "--language", default="",
        help="Language of the title, description and speech, as ISO 639-2/B or BCP-47")
    argparser.add_argument(
        "--session-file",
        help="Remember the upload session here, so that a run interrupted part way rejoins it "
             "instead of sending the file again from the start")
    argparser.add_argument("--title", help="Video title", default="Test Title")
    argparser.add_argument("--description", help="Video description", default="Test Description")
    # https://gist.github.com/dgp/1b24bf2961521bd75d6c
    argparser.add_argument(
        "--category",
        type=int,
        default=29,
        help="Numeric video category (default: 29 - Nonprofits & Activism). "
        + "See https://developers.google.com/youtube/v3/docs/videoCategories/list",
    )
    argparser.add_argument("--keywords", help="Video keywords, comma separated", default="")
    argparser.add_argument(
        "--privacyStatus",
        choices=VALID_PRIVACY_STATUSES,
        default=VALID_PRIVACY_STATUSES[0],
        help="Video privacy status.",
    )
    argparser.add_argument(
        "--publishAt",
        help="RFC 3339 datetime to make the video public at, in the future (e.g., 2026-12-31T23:59:00Z). "
        + f"Requires --privacyStatus {SCHEDULABLE_PRIVACY_STATUS}.",
        default=None,
    )
    argparser.add_argument(
        "--channel",
        default=None,
        help="Channel id, or title, that uploads must land on. The upload aborts if the credentials "
        + f"belong to any other channel. Defaults to ${CHANNEL_ENV_VAR}; omit both to skip the check. "
        + "An id is the reliable form, since a title can be renamed.",
    )
    argparser.add_argument(
        "--recordingDate",
        help="Date the video was recorded, YYYY-MM-DD or a full RFC 3339 datetime. May be in the past, "
        + "unlike --publishAt; it is the only place an original date is preserved, since the upload date "
        + "is set by YouTube and cannot be changed.",
        default=None,
    )
    return argparser


#: How many times to ask what became of an upload, and how long to wait between.
#:
#: ``videos.insert`` answers as soon as the bytes are received, which is before
#: YouTube has looked at them. A video too long for an unverified channel is
#: accepted, given an id, and rejected minutes later during processing — so a
#: run that trusts the insert records a success for a video nobody can watch,
#: and a systematic fault sails through a whole batch looking perfect.
#:
#: Waiting for processing to *finish* is not the goal and would take an hour.
#: The goal is to be present long enough to catch a refusal, which arrives
#: early. Each check costs one quota unit against a daily ten thousand.
CONFIRM_ATTEMPTS = 5
CONFIRM_SECONDS = 30

#: What ``status.uploadStatus`` says when YouTube has refused a video outright.
REFUSED = ("rejected", "failed")


def video_status(youtube: Resource, video_id: str) -> tuple[str, str, str]:
    """
    Ask what YouTube has made of a video.

    :param youtube: Authenticated YouTube service object
    :param video_id: The video to ask about
    :return: Its upload status, its processing status, and the reason for a
        refusal where there is one
    :raises ValueError: If YouTube does not know the video
    """
    items = youtube.videos().list(part="status,processingDetails", id=video_id).execute().get("items") or []
    if not items:
        raise ValueError(f"YouTube does not have a video {video_id}")
    status = items[0]["status"]
    processing = (items[0].get("processingDetails") or {}).get("processingStatus", "")
    reason = status.get("rejectionReason") or status.get("failureReason") or ""
    return status.get("uploadStatus", ""), processing, reason


def confirm_upload(youtube: Resource, video_id: str, attempts: int = CONFIRM_ATTEMPTS,
                   seconds: int = CONFIRM_SECONDS) -> tuple[str, str]:
    """
    Stay with an upload long enough to see whether YouTube refuses it.

    Returns as soon as the answer is settled either way. Still processing when
    the attempts run out is not a failure and is reported as such — a long
    recording can transcode for an hour, and the caller has better things to do
    than watch it.

    :param youtube: Authenticated YouTube service object
    :param video_id: The video to watch
    :param attempts: How many times to ask
    :param seconds: How long to wait between asking
    :return: The upload status, and the reason for a refusal where there is one
    """
    upload_status = processing = reason = ""
    for attempt in range(attempts):
        upload_status, processing, reason = video_status(youtube, video_id)
        if upload_status in REFUSED:
            logger.warning("%s was refused by YouTube: %s %s", video_id, upload_status, reason)
            return upload_status, reason
        if upload_status == "processed":
            logger.info("%s accepted and processed", video_id)
            return upload_status, ""
        if attempt < attempts - 1:
            time.sleep(seconds)
    logger.info("%s accepted; still %s after %d checks", video_id, processing or upload_status, attempts)
    return upload_status, ""


#: The snippet fields ``videos.update`` writes, and therefore the ones an edit
#: has to send back even when it is not changing them.
#:
#: An update replaces the whole part it names: a writable field left out of the
#: body is not left alone, it is *cleared*. So a pass that only rewrites a title
#: still has to return the description, tags, category and language it read, or
#: it publishes the new title over an emptied snippet.
#:
#: ``defaultAudioLanguage`` belongs here even though the API reference does not
#: list it among the properties an update writes. It is cleared like the rest
#: when left out, and YouTube then guesses the language afresh from the audio —
#: which on a Korean sermon it got wrong, marking it ``en-US`` and changing
#: which captions and translations the video is offered with. Found the only way
#: it could be: by reading a video back after editing it.
WRITABLE_SNIPPET_FIELDS = (
    "title", "description", "tags", "categoryId", "defaultLanguage", "defaultAudioLanguage",
)

#: How many videos one ``videos.list`` may be asked about. Reading the snippets
#: back costs one quota unit per call rather than per video, so a back-catalogue
#: pass reads several hundred for the price of a handful.
SNIPPET_BATCH = 50


def writable_snippet(snippet: dict) -> dict:
    """
    Take the part of a snippet that can be sent back, and drop the rest.

    A snippet read from the API also carries fields nobody may write —
    ``publishedAt``, ``channelId``, ``thumbnails`` — which are ignored on the
    way back in. The fields that matter are the writable ones, because those
    are the ones an update clears if they are missing. See
    :data:`WRITABLE_SNIPPET_FIELDS`.

    :param snippet: A snippet as ``videos.list`` returned it
    :return: Only the writable fields it actually had

    >>> writable_snippet({"title": "제목", "channelId": "UC1", "categoryId": "29"})
    {'title': '제목', 'categoryId': '29'}
    >>> writable_snippet({"title": "제목", "defaultAudioLanguage": "ko"})
    {'title': '제목', 'defaultAudioLanguage': 'ko'}
    """
    return {field: snippet[field] for field in WRITABLE_SNIPPET_FIELDS if field in snippet}


def fetch_snippets(youtube: Resource, video_ids: list[str]) -> dict[str, dict]:
    """
    Read back what a set of videos currently say.

    :param youtube: Authenticated YouTube service object
    :param video_ids: The videos to ask about, in any number
    :return: Video id -> its snippet, omitting any id YouTube does not know
    """
    snippets: dict[str, dict] = {}
    for start in range(0, len(video_ids), SNIPPET_BATCH):
        batch = video_ids[start:start + SNIPPET_BATCH]
        response = youtube.videos().list(part="snippet", id=",".join(batch), maxResults=SNIPPET_BATCH).execute()
        for item in response.get("items") or []:
            snippets[item["id"]] = item["snippet"]
    return snippets


def update_snippet(youtube: Resource, video_id: str, snippet: dict) -> None:
    """
    Publish an edited snippet over the one a video carries.

    :param youtube: Authenticated YouTube service object
    :param video_id: The video to edit
    :param snippet: The complete writable snippet, as
        :func:`writable_snippet` returns it with the edits applied
    :return: None
    :raises ValueError: If the title is longer than YouTube accepts, which it
        would refuse with a message naming neither the video nor the field
    :raises googleapiclient.errors.HttpError: If YouTube refuses the edit
    """
    title = snippet.get("title", "")
    if len(title) > MAX_TITLE_LENGTH:
        raise ValueError(
            f"{video_id}: a title of {len(title)} characters is longer than the {MAX_TITLE_LENGTH} "
            f"YouTube accepts: {title!r}"
        )
    youtube.videos().update(part="snippet", body={"id": video_id, "snippet": snippet}).execute()


def upload_arguments(file: str, title: str, description: str = "", privacy: str = "private",
                     category: int = 22, recording_date: str = "", channel: str = "",
                     session_file: str = "", made_for_kids: str = DEFAULT_MADE_FOR_KIDS,
                     embeddable: str = "yes", language: str = "") -> list[str]:
    """
    Build the arguments for one upload.

    Kept here beside the parser that reads them. A caller assembling this list
    somewhere else has no way to know when an option is renamed or missing, and
    finds out only when a real upload of a real recording fails on the argument
    line — which is exactly how this function came to exist.

    :param file: The video to send
    :param title: What to publish it as; YouTube refuses more than 100 characters
    :param description: The description to publish
    :param privacy: One of :data:`VALID_PRIVACY_STATUSES`
    :param category: YouTube category id
    :param recording_date: ``YYYY-MM-DD`` the recording belongs to, if known
    :param channel: Channel the upload must land on, if it must
    :param session_file: Where to remember the upload session
    :param made_for_kids: ``yes`` or ``no``, which YouTube requires to be stated
    :param embeddable: ``yes`` or ``no``, whether other sites may embed it
    :param language: Language of the title, description and speech
    :return: Arguments :func:`create_argument_parser` accepts

    >>> args = create_argument_parser().parse_args(
    ...     upload_arguments("a.mp4", "설교 제목", recording_date="2026-08-02"))
    >>> args.file, args.title, args.recordingDate, args.privacyStatus
    ('a.mp4', '설교 제목', '2026-08-02', 'private')
    """
    arguments = ["--file", file, "--title", title[:MAX_TITLE_LENGTH],
                 "--description", description, "--privacyStatus", privacy,
                 "--category", str(category), "--madeForKids", made_for_kids,
                 "--embeddable", embeddable]
    for flag, value in (("--recordingDate", recording_date), ("--channel", channel),
                        ("--session-file", session_file), ("--language", language)):
        if value:
            arguments += [flag, value]
    return arguments


def upload(options: argparse.Namespace) -> str | None:
    """
    Authenticate and upload a single video to YouTube.

    This is the entry point registered in :data:`video_migrator.sinks.SINKS`.

    :param options: Options carrying the file path, video metadata and OAuth settings
    :return: The id of the video created, or None if the API returned none
    :raises ValueError: If the metadata combination is one the API rejects, or the
        credentials belong to a channel other than the one asked for
    :raises googleapiclient.errors.HttpError: If the upload fails unrecoverably
    """
    youtube = get_authenticated_service(options)
    verify_channel(youtube, expected_channel(options))
    session = getattr(options, "session_file", None)
    return initialize_upload(youtube, options, pathlib.Path(session) if session else None)


def verify(options: argparse.Namespace) -> None:
    """
    Consent if the token is not cached yet, and report where uploads would land.

    :param options: Options carrying the OAuth settings
    :raises ValueError: If the credentials belong to a channel other than the
        one asked for
    """
    expected = expected_channel(options)
    youtube = get_authenticated_service(options)
    channel_id, title = authenticated_channel(youtube)
    logger.info(f"token cached at {credentials_path(expected)}")
    logger.info(f"uploads would land on {title!r} (channel {channel_id})")
    if expected:
        verify_channel(youtube, expected)
        logger.info(f"matches the {CHANNEL_ENV_VAR} asked for")
    else:
        logger.info(f"nothing set in {CHANNEL_ENV_VAR} or --channel, so no channel is being insisted on")


def main() -> None:
    """
    Main function to handle command-line arguments and upload a video.

    :raises SystemExit: Exits with an error if the file is missing
    """
    parser = create_argument_parser()
    args = parser.parse_args()
    # Everything this module says, it says through the logger. Without a
    # configured handler the command runs, succeeds, and prints nothing — which
    # is worst for --verify-only, whose entire output is the report.
    configure()

    if not args.verify_only and not (args.file and os.path.exists(args.file)):
        sys.exit("Please specify a valid file using the --file= parameter, or pass --verify-only.")

    try:
        verify(args) if args.verify_only else upload(args)
    except ValueError as e:
        sys.exit(str(e))
    except HttpError as e:
        logger.info(f"An HTTP error {e.resp.status} occurred:\n{e.content}")


if __name__ == "__main__":
    main()
