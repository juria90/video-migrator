#!/usr/bin/env python3
"""
Vimeo source platform.

Downloads videos from Vimeo using yt-dlp, reusing the browser's cookies so
private and password-protected videos on the authenticated account work.
"""

import argparse
import sys

import yt_dlp


class VimeoDownloader:
    """
    A class to handle downloading videos from Vimeo using yt-dlp.

    This class provides a simple interface to download videos from Vimeo
    with various quality and format options.
    """

    def __init__(self, url: str) -> None:
        """
        Initialize the VimeoDownloader with a Vimeo URL.

        :param url: The Vimeo video URL to download
        """
        self.url = url

    def get_video_info(self) -> dict:
        """
        Fetch video information without downloading.

        :return: Video information including title, description, formats, and metadata
        :raises yt_dlp.utils.DownloadError: If video information cannot be extracted
        """
        ydl_opts = {
            "cookiesfrombrowser": ("chrome",),
            "quiet": True,
            "no_warnings": True,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(self.url, download=False)
            return info

    def download(self, output_path: str = None, quality: str = "best", format_type: str = "mp4") -> str:
        """
        Download the Vimeo video.

        :param output_path: Path where the video will be saved.
            If None, saves to current directory with video title as filename
        :param quality: Video quality preference ('best', 'worst', or specific height like '720').
            Defaults to 'best'
        :param format_type: Output format type. Defaults to 'mp4'
        :return: Path to the downloaded file
        :raises yt_dlp.utils.DownloadError: If download fails
        """
        # Configure yt-dlp options
        ydl_opts = {
            "cookiesfrombrowser": ("chrome",),
            "format": self._get_format_string(quality, format_type),
            "outtmpl": output_path if output_path else "%(title)s.%(ext)s",
            "progress_hooks": [self._progress_hook],
        }

        print(f"Downloading video from: {self.url}")

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(self.url, download=True)
            downloaded_file = ydl.prepare_filename(info)

        print(f"\nDownload complete: {downloaded_file}")
        return downloaded_file

    def _get_format_string(self, quality: str, format_type: str) -> str:
        """
        Generate yt-dlp format string based on quality and format preferences.

        :param quality: Video quality preference
        :param format_type: Output format type
        :return: Format string for yt-dlp
        """
        if quality == "best":
            return f"bestvideo[ext={format_type}]+bestaudio[ext=m4a]/best[ext={format_type}]/best"
        elif quality == "worst":
            return f"worstvideo[ext={format_type}]+worstaudio[ext=m4a]/worst[ext={format_type}]/worst"
        else:
            # Specific height (e.g., '720', '1080')
            return f"bestvideo[height<={quality}][ext={format_type}]+bestaudio[ext=m4a]/best[height<={quality}][ext={format_type}]/best"

    def _progress_hook(self, d: dict) -> None:
        """
        Hook function to display download progress.

        :param d: Progress information dictionary from yt-dlp
        """
        if d["status"] == "downloading":
            if "total_bytes" in d:
                percent = (d["downloaded_bytes"] / d["total_bytes"]) * 100
                print(f"\rProgress: {percent:.1f}%", end="")
            elif "downloaded_bytes" in d:
                mb_downloaded = d["downloaded_bytes"] / (1024 * 1024)
                print(f"\rDownloaded: {mb_downloaded:.1f} MB", end="")
        elif d["status"] == "finished":
            print("\rDownload finished, processing...")

    def list_formats(self) -> None:
        """
        List all available formats for the video.

        :raises yt_dlp.utils.DownloadError: If video information cannot be extracted
        """
        ydl_opts = {
            "cookiesfrombrowser": ("chrome",),
            "listformats": True,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(self.url, download=False)


def main() -> None:
    """
    Main function to handle command-line arguments and initiate download.

    :raises SystemExit: Exits with code 1 if download fails
    """
    parser = argparse.ArgumentParser(description="Download videos from Vimeo using yt-dlp")
    parser.add_argument("url", help="Vimeo video URL")
    parser.add_argument("-o", "--output", help="Output file path")
    parser.add_argument(
        "-q",
        "--quality",
        default="best",
        help="Video quality (best, worst, or specific height like 720, 1080)",
    )
    parser.add_argument("-f", "--format", default="mp4", help="Output format type (default: mp4)")
    parser.add_argument(
        "-i",
        "--info",
        action="store_true",
        help="Show video information without downloading",
    )
    parser.add_argument("-l", "--list-formats", action="store_true", help="List all available formats")

    args = parser.parse_args()

    try:
        downloader = VimeoDownloader(args.url)

        if args.list_formats:
            downloader.list_formats()
        elif args.info:
            info = downloader.get_video_info()
            print(f"Title: {info.get('title')}")
            print(f"Duration: {info.get('duration')} seconds")
            print(f"Uploader: {info.get('uploader')}")
            print(f"Description: {info.get('description', 'N/A')[:200]}")
        else:
            downloader.download(output_path=args.output, quality=args.quality, format_type=args.format)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
