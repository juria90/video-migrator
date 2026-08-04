#!/usr/bin/env python3
"""
Fallback aninamu.com strategy: extract the media URL, then hand it to yt-dlp.

Use this when the direct download in :mod:`video_migrator.sources.aninamu`
fails because the media is served as a stream rather than a plain file.
"""

import subprocess
import sys

import requests
from bs4 import BeautifulSoup


def extract_video_info(page_url: str) -> dict:
    """
    Extract video information from aninamu page.

    :param page_url: The URL of the aninamu episode page
    :return: Dictionary with video information
    :raises Exception: If extraction fails
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    }

    response = requests.get(page_url, headers=headers)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    # Find the form with video URL
    form = soup.find("form", {"action": lambda x: x and "glamov.com" in x})
    if not form:
        raise ValueError("Could not find video form")

    # Extract form data
    form_data = {}
    for input_tag in form.find_all("input"):
        name = input_tag.get("name")
        value = input_tag.get("value")
        if name and value:
            form_data[name] = value

    return form_data


def try_download_with_ytdlp(video_url: str, output_file: str, referer: str = None) -> bool:
    """
    Try to download using yt-dlp with various options.

    :param video_url: The video URL to download
    :param output_file: Output filename
    :param referer: Optional referer header
    :return: True if successful, False otherwise
    """
    print("\nAttempting to download with yt-dlp...")
    print(f"Video URL: {video_url}")

    cmd = [
        sys.executable.replace("python", "yt-dlp"),
        video_url,
        "-o",
        output_file,
        "--user-agent",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    ]

    if referer:
        cmd.extend(["--referer", referer])

    # Add various options to help with protected videos
    cmd.extend(
        [
            "--no-check-certificate",
            "--verbose",
        ]
    )

    try:
        result = subprocess.run(cmd, capture_output=False, text=True)
        return result.returncode == 0
    except Exception as e:
        print(f"yt-dlp failed: {e}")
        return False


def main():
    """Main function."""
    if len(sys.argv) < 2:
        print("Usage: python -m video_migrator.sources.aninamu_ytdlp <aninamu_url> [output_filename]")
        sys.exit(1)

    page_url = sys.argv[1]
    output_filename = sys.argv[2] if len(sys.argv) > 2 else "video.mp4"

    print("=" * 70)
    print("Aninamu Video Downloader")
    print("=" * 70)

    try:
        # Extract video info
        print(f"\n1. Extracting video information from: {page_url}")
        form_data = extract_video_info(page_url)

        print("\nFound video information:")
        for key, value in form_data.items():
            if key == "vurl":
                print(f"  {key}: {value}")
            else:
                print(f"  {key}: {value[:50] + '...' if len(value) > 50 else value}")

        video_url = form_data.get("vurl")
        if not video_url:
            print("\n❌ Could not find video URL in page")
            sys.exit(1)

        # Try downloading with yt-dlp
        print("\n2. Attempting download...")
        success = try_download_with_ytdlp(video_url, output_filename, referer=page_url)

        if success:
            print(f"\n✓ Successfully downloaded to: {output_filename}")
        else:
            print("\n" + "=" * 70)
            print("❌ Download failed. The video may be:")
            print("  • Protected and requires browser access")
            print("  • Expired or temporarily unavailable")
            print("  • Using a streaming protocol that needs special handling")
            print("\nSuggested alternatives:")
            print(
                "  1. Use a browser extension like 'Video DownloadHelper' or 'Stream Recorder'"
            )
            print("  2. Open the page in a browser and use browser dev tools")
            print(f"  3. Video URL found: {video_url}")
            print("=" * 70)

    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
