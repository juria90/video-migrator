#!/usr/bin/env python3
"""
Download videos from aninamu.com.

The page hides the real media URL in a hidden form field that is posted to
glamov.com, so the downloader scrapes the form first and then fetches the
media directly.
"""

import re
import sys

import requests
from bs4 import BeautifulSoup

BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class AninamuDownloader:
    """
    A class to handle downloading videos from aninamu.com.

    Scrapes the episode page for the hidden video URL, then streams the media
    file to disk.
    """

    def __init__(self, url: str) -> None:
        """
        Initialize the downloader with an aninamu episode URL.

        :param url: The aninamu episode page URL
        """
        self.url = url

    def get_video_url(self) -> tuple[str, dict[str, str]]:
        """
        Extract the video URL from the aninamu page.

        :return: Tuple of (video URL, hidden form parameters)
        :raises ValueError: If video URL cannot be found
        :raises requests.exceptions.RequestException: If the page cannot be fetched
        """
        headers = {
            "User-Agent": BROWSER_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
            "Referer": "https://aninamu.com/",
        }

        response = requests.get(self.url, headers=headers)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")

        # Find the form with the video URL
        form = soup.find("form", {"action": re.compile(r"glamov\.com")})
        if not form:
            raise ValueError("Could not find video form on page")

        # Extract the video URL from hidden input
        vurl_input = form.find("input", {"name": "vurl"})
        if not vurl_input or not vurl_input.get("value"):
            raise ValueError("Could not find video URL in form")

        video_url = vurl_input["value"]
        print(f"Found video URL: {video_url}")

        # Also get other form parameters
        form_data = {}
        for input_tag in form.find_all("input", {"type": "hidden"}):
            name = input_tag.get("name")
            value = input_tag.get("value")
            if name and value:
                form_data[name] = value

        return video_url, form_data

    def download(self, output_path: str, video_url: str | None = None) -> str:
        """
        Download the video to disk.

        :param output_path: Path where to save the video
        :param video_url: Direct media URL. If None, it is scraped from the page
        :return: Path to the downloaded file
        :raises requests.exceptions.RequestException: If download fails
        """
        if video_url is None:
            video_url, _ = self.get_video_url()

        headers = {
            "User-Agent": BROWSER_USER_AGENT,
            "Accept": "*/*",
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
            "Referer": self.url,
        }

        print(f"Downloading video from: {video_url}")
        response = requests.get(video_url, headers=headers, stream=True)
        response.raise_for_status()

        # Check if it's actually a video file
        content_type = response.headers.get("Content-Type", "")
        print(f"Content-Type: {content_type}")

        total_size = int(response.headers.get("Content-Length", 0))
        print(f"File size: {total_size / (1024 * 1024):.2f} MB")

        with open(output_path, "wb") as f:
            downloaded = 0
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total_size > 0:
                        percent = (downloaded / total_size) * 100
                        print(f"\rProgress: {percent:.1f}%", end="", flush=True)

        print(f"\n✓ Video downloaded successfully to: {output_path}")
        return output_path


def main() -> None:
    """
    Main function to download a video from aninamu.

    :raises SystemExit: Exits with code 1 if download fails
    """
    if len(sys.argv) < 2:
        print("Usage: python -m video_migrator.sources.aninamu <aninamu_url> [output_filename]")
        print("Example: python -m video_migrator.sources.aninamu 'https://aninamu.com/...' video.mp4")
        sys.exit(1)

    page_url = sys.argv[1]
    output_filename = sys.argv[2] if len(sys.argv) > 2 else "video.mp4"

    try:
        downloader = AninamuDownloader(page_url)
        video_url, form_data = downloader.get_video_url()
        print(f"Form data: {form_data}")

        # Try downloading directly first
        try:
            downloader.download(output_filename, video_url=video_url)
        except Exception as e:
            print(f"Direct download failed: {e}")
            print("\nThe video might require browser-based access or may use a streaming protocol (HLS/m3u8).")
            print("You may need to:")
            print("1. Use a browser extension like 'Video DownloadHelper' or 'Stream Recorder'")
            print("2. Use yt-dlp if it's a streaming video: yt-dlp <video_url>")
            print(f"\nVideo URL found: {video_url}")

    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
