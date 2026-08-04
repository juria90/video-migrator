#!/usr/bin/env python3
"""
YouTube destination platform.

Uploads videos to YouTube using the YouTube Data API v3, with resumable
uploads and exponential backoff so a large back-catalogue migration survives
transient network failures.
"""

import argparse
import http.client
import os
import random
import sys
import time

import httplib2
from googleapiclient.discovery import Resource, build
from googleapiclient.errors import HttpError
from googleapiclient.http import HttpRequest, MediaFileUpload
from oauth2client.client import flow_from_clientsecrets
from oauth2client.file import Storage
from oauth2client.tools import argparser, run_flow

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
YOUTUBE_API_SERVICE_NAME = "youtube"
YOUTUBE_API_VERSION = "v3"

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

VALID_PRIVACY_STATUSES = ("public", "private", "unlisted")

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


def get_authenticated_service(args: argparse.Namespace) -> Resource:
    """
    Authenticate and build the YouTube service.

    :param args: Command-line arguments containing OAuth configuration
    :return: Authenticated YouTube service object
    """
    flow = flow_from_clientsecrets(
        CLIENT_SECRETS_FILE,
        scope=YOUTUBE_UPLOAD_SCOPE,
        message=MISSING_CLIENT_SECRETS_MESSAGE,
    )

    storage = Storage(f"{sys.argv[0]}-oauth2.json")
    credentials = storage.get()

    if credentials is None or credentials.invalid:
        credentials = run_flow(flow, storage, args)

    return build(
        YOUTUBE_API_SERVICE_NAME,
        YOUTUBE_API_VERSION,
        http=credentials.authorize(httplib2.Http()),
    )


def initialize_upload(youtube: Resource, options: argparse.Namespace) -> None:
    """
    Initialize and start the video upload process.

    :param youtube: Authenticated YouTube service object
    :param options: Command-line options containing video metadata
    """
    tags = None
    if options.keywords:
        tags = options.keywords.split(",")

    body = {
        "snippet": {
            "title": options.title,
            "description": options.description,
            "tags": tags,
            "categoryId": str(options.category),
        },
        "status": {"privacyStatus": options.privacyStatus, "publishAt": options.publishAt},
    }

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
        media_body=MediaFileUpload(options.file, chunksize=-1, resumable=True),
    )

    resumable_upload(insert_request)


def resumable_upload(insert_request: HttpRequest) -> None:
    """
    Implement an exponential backoff strategy to resume a failed upload.

    :param insert_request: YouTube API insert request object
    """
    response = None
    error = None
    retry = 0
    while response is None:
        try:
            print("Uploading file...")
            status, response = insert_request.next_chunk()
            if response is not None:
                if "id" in response:
                    print("Video id '{}' was successfully uploaded.".format(response["id"]))
                else:
                    sys.exit(f"The upload failed with an unexpected response: {response}")
        except HttpError as e:
            if e.resp.status in RETRIABLE_STATUS_CODES:
                error = f"A retriable HTTP error {e.resp.status} occurred:\n{e.content}"
            else:
                raise
        except RETRIABLE_EXCEPTIONS as e:
            error = f"A retriable error occurred: {e}"

        if error is not None:
            print(error)
            retry += 1
            if retry > MAX_RETRIES:
                sys.exit("No longer attempting to retry.")

            max_sleep = 2**retry
            sleep_seconds = random.random() * max_sleep
            print(f"Sleeping {sleep_seconds:f} seconds and then retrying...")
            time.sleep(sleep_seconds)


def create_argument_parser() -> argparse.ArgumentParser:
    """
    Create and configure the argument parser for the CLI.

    :return: Configured argument parser
    """
    argparser.add_argument("--file", required=True, help="Video file to upload")
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
        help="ISO 8601 format datetime to publish the video (e.g., 2024-12-31T23:59:00Z)",
        default=None,
    )
    return argparser


def upload(options: argparse.Namespace) -> None:
    """
    Authenticate and upload a single video to YouTube.

    This is the entry point registered in :data:`video_migrator.sinks.SINKS`.

    :param options: Options carrying the file path, video metadata and OAuth settings
    :raises googleapiclient.errors.HttpError: If the upload fails unrecoverably
    """
    youtube = get_authenticated_service(options)
    initialize_upload(youtube, options)


def main() -> None:
    """
    Main function to handle command-line arguments and upload a video.

    :raises SystemExit: Exits with an error if the file is missing
    """
    parser = create_argument_parser()
    args = parser.parse_args()

    if not os.path.exists(args.file):
        sys.exit("Please specify a valid file using the --file= parameter.")

    try:
        upload(args)
    except HttpError as e:
        print(f"An HTTP error {e.resp.status} occurred:\n{e.content}")


if __name__ == "__main__":
    main()
