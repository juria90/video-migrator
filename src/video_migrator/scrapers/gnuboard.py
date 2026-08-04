#!/usr/bin/env python3
"""
Scraper for GnuBoard board pages.

GnuBoard is the Korean bulletin-board CMS behind ``/bbs/board.php?bo_table=...``
URLs. This module targets its gallery skin — ``li.gall_li`` items holding
``div.list_cnt`` metadata, paginated by ``a.pg_page`` links — so it works
against any GnuBoard site using that skin, not just the one shipped in
``profiles/example.yaml``.

Walks a board's paginated gallery and extracts every embedded video it finds,
regardless of the platform hosting it (Vimeo, YouTube, SoundCloud).

Which site it points at comes from the site profile, not from this module.
"""

import hashlib
import re
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

from ..config import DEFAULT_PROFILE, load_profile
from ..models import Video
from ..utils.http_cache import HTTPCache

# Display names for the platforms a scraped video can live on. Unlike the board
# list this is not configuration: each label corresponds to a parser this module
# implements, so adding one here would produce a label, not a capability.
PLATFORM_LABELS = {
    "vimeo": "Vimeo",
    "youtube": "YouTube",
    "soundcloud": "SoundCloud",
}


def board_url(board: str, profile_name: str = DEFAULT_PROFILE) -> str:
    """
    Build the listing URL for a board on the profile's site.

    :param board: Board table name, one of ``load_profile(profile_name).board_names``
    :param profile_name: Site profile supplying the base URL
    :return: Fully qualified board listing URL

    >>> board_url("sunday_sermon")
    'https://example.com/bbs/board.php?bo_table=sunday_sermon'
    """
    return f"{load_profile(profile_name).board_url}?bo_table={board}"


class GnuBoardScraper:
    """
    Scrape video links and metadata from a GnuBoard gallery board.

    This class fetches a webpage and extracts video information including
    titles, URLs, thumbnails, and other metadata.
    """

    def __init__(self, url: str, genre: str, language: str = "", cache_dir: str = ".cache", cache_duration: int = 3600):
        """
        Initialize the scraper with a webpage URL.

        :param url: The webpage URL to scrape
        :param genre: Genre for the videos (e.g., "Sermon", "Praise")
        :param language: Language of the videos (e.g., "Korean", "English")
        :param cache_dir: Directory to store cached HTML pages
        :param cache_duration: Cache duration in seconds (default: 3600 = 1 hour)
        """
        self.url = url
        self.base_url = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
        self.http_cache = HTTPCache(cache_dir, cache_duration)
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
            }
        )
        self.genre = genre
        self.language = language

    def _get_cache_key(self, url: str) -> str:
        """
        Generate a unique cache key for a given URL.

        :param url: URL to generate cache key for
        :return: Cache key string
        """
        # Extract board name from the GnuBoard query string (bo_table / wr_id)
        parsed = urlparse(url)
        query_params = parse_qs(parsed.query)
        board_name = query_params.get("bo_table", ["unknown"])[0]
        page_num = query_params.get("page", ["1"])[0]
        wr_id = query_params.get("wr_id", [None])[0]

        # Create a cache key based on board name, page number, and wr_id (if exists)
        return f"{board_name}_page{page_num}_wr{wr_id}" if wr_id else f"{board_name}_page{page_num}"

    def fetch_page(self, url: str | None = None) -> str:
        """
        Fetch the HTML content of the webpage with caching support.

        :param url: URL to fetch. If None, uses self.url
        :return: HTML content of the page
        :raises requests.RequestException: If the page cannot be fetched
        """
        if url is None:
            url = self.url

        cache_key = self._get_cache_key(url)
        content, _ = self.http_cache.fetch_with_cache(url, cache_key, self.session)
        return content

    def extract_videos(self, html: str) -> list[Video]:
        """
        Extract video links and metadata from HTML content.

        :param html: HTML content to parse
        :return: List of Video objects
        """
        soup = BeautifulSoup(html, "html.parser")
        videos = []

        # Find all li elements with class 'gall_li' (gallery list items)
        gallery_items = soup.find_all("li", class_="gall_li")

        for li_item in gallery_items:
            # Check if this item contains an iframe (video) or span (audio/no image)
            iframe = li_item.find("iframe")
            no_image_span = li_item.find("span", string=re.compile(r"No\s+Image", re.I))

            if iframe:
                # Process iframe video (Vimeo or YouTube)
                src = iframe.get("src", "")
                if "vimeo.com" in src:
                    video_id_match = re.search(r"vimeo\.com/video/(\d+)", src)
                    if video_id_match:
                        video_id = video_id_match.group(1)
                        video_url = f"https://vimeo.com/{video_id}"
                        content = self._extract_metadata(li_item)

                        videos.append(
                            Video(
                                type="vimeo",
                                id=video_id,
                                url=video_url,
                                embed_url=src,
                                title=content["title"],
                                bible_verse=content["bible_verse"],
                                publish_date=content["publish_date"],
                                artist=content["preacher"],
                                genre=self.genre,
                                language=self.language,
                            )
                        )
                elif "youtube.com" in src or "youtu.be" in src:
                    video_id_match = re.search(r"(?:youtube\.com/embed/|youtu\.be/)([a-zA-Z0-9_-]+)", src)
                    if video_id_match:
                        video_id = video_id_match.group(1)
                        video_url = f"https://www.youtube.com/watch?v={video_id}"
                        content = self._extract_metadata(li_item)

                        videos.append(
                            Video(
                                type="youtube",
                                id=video_id,
                                url=video_url,
                                embed_url=src,
                                title=content["title"],
                                bible_verse=content["bible_verse"],
                                publish_date=content["publish_date"],
                                artist=content["preacher"],
                                genre=self.genre,
                                language=self.language,
                            )
                        )

            elif no_image_span:
                # Process "No Image" item - extract soundcloud link from onclick page
                list_cnt = li_item.find("div", class_="list_cnt")
                if list_cnt:
                    onclick = list_cnt.get("onclick", "")
                    # Extract URL from onclick attribute like: location.href='...'
                    url_match = re.search(r"location\.href\s*=\s*['\"]([^'\"]+)['\"]", onclick)
                    if url_match:
                        detail_url = url_match.group(1)
                        # Make URL absolute if it's relative
                        if detail_url.startswith("/") or not detail_url.startswith("http"):
                            detail_url = urljoin(self.base_url, detail_url)

                        # Fetch the detail page to extract soundcloud link
                        soundcloud_url = self._extract_soundcloud_from_page(detail_url)
                        if soundcloud_url:
                            content = self._extract_metadata(li_item)

                            videos.append(
                                Video(
                                    type="soundcloud",
                                    id=self._extract_soundcloud_id(soundcloud_url),
                                    url=soundcloud_url,
                                    embed_url=soundcloud_url,
                                    title=content["title"],
                                    bible_verse=content["bible_verse"],
                                    publish_date=content["publish_date"],
                                    artist=content["preacher"],
                                    genre=self.genre,
                                    language=self.language,
                                )
                            )

        return videos

    def _extract_soundcloud_from_page(self, url: str) -> str | None:
        """
        Extract SoundCloud link from a detail page.

        :param url: URL of the detail page
        :return: SoundCloud URL if found, None otherwise
        """
        try:
            html = self.fetch_page(url)
            soup = BeautifulSoup(html, "html.parser")

            # Look for iframe with soundcloud.com
            iframe = soup.find("iframe", src=re.compile(r"soundcloud\.com"))
            if iframe:
                src = iframe.get("src", "")
                # Extract the original soundcloud URL from the embed URL
                url_match = re.search(r"url=([^&]+)", src)
                if url_match:
                    return unquote(url_match.group(1))
                return src

            # Look for links containing soundcloud.com
            link = soup.find("a", href=re.compile(r"soundcloud\.com"))
            if link:
                return link.get("href")

            return None
        except Exception as e:
            print(f"Error extracting soundcloud from {url}: {e}")
            return None

    def _extract_soundcloud_id(self, url: str) -> str:
        """
        Extract a unique identifier from SoundCloud URL.

        :param url: SoundCloud URL
        :return: Extracted ID or hash of the URL
        """
        # Try to extract track ID or use URL hash
        track_match = re.search(r"soundcloud\.com/([^/]+/[^/?]+)", url)
        if track_match:
            return track_match.group(1).replace("/", "_")

        # Fallback to hash
        return hashlib.md5(url.encode()).hexdigest()[:12]

    def _extract_metadata(self, li_element) -> dict[str, str]:
        """
        Extract metadata from a gallery list item element.

        :param li_element: BeautifulSoup li element (with class 'gall_li')
        :return: Dictionary with 'title', 'bible_verse', 'publish_date', and 'preacher'
        """
        result = {
            "title": "Untitled",
            "bible_verse": "",
            "publish_date": "",
            "preacher": "",
        }

        try:
            # Extract metadata from div.list_cnt (GnuBoard gallery skin)
            list_cnt = li_element.find("div", class_="list_cnt")
            if list_cnt:
                # Look for span with class 'subject' (GnuBoard title location)
                subject_span = list_cnt.find("span", class_="subject")
                if subject_span:
                    text = subject_span.get_text(strip=True)
                    if text and len(text) < 300:
                        result["title"] = text

                # Look for span with class 'name' (contains bible_verse and publish_date as child spans)
                name_span = list_cnt.find("span", class_="name")
                if name_span:
                    child_spans = name_span.find_all("span")
                    if len(child_spans) >= 2:
                        result["bible_verse"] = child_spans[0].get_text(strip=True)
                        result["publish_date"] = child_spans[1].get_text(strip=True)

                # Look for span with class 'content' (preacher)
                content_span = list_cnt.find("span", class_="content")
                if content_span:
                    result["preacher"] = content_span.get_text(strip=True)
        except (AttributeError, IndexError) as e:
            print(f"Warning: Error extracting metadata: {e}")

        return result

    def _get_max_pages(self, html: str) -> int:
        """
        Extract the maximum number of pages from pagination links.

        :param html: HTML content to parse
        :return: Maximum page number found, or 1 if not found
        """
        soup = BeautifulSoup(html, "html.parser")

        # Look for the "맨끝" (last page) link
        last_page_link = soup.find("a", class_="pg_end")
        if last_page_link:
            href = last_page_link.get("href", "")
            # Extract page number from the URL
            page_match = re.search(r"[?&]page=(\d+)", href)
            if page_match:
                return int(page_match.group(1))

        # Alternative: find all pagination links and get the highest number
        page_links = soup.find_all("a", class_=re.compile(r"pg_page"))
        max_page = 1
        for link in page_links:
            href = link.get("href", "")
            page_match = re.search(r"[?&]page=(\d+)", href)
            if page_match:
                page_num = int(page_match.group(1))
                max_page = max(max_page, page_num)

        return max_page if max_page > 0 else 1

    def get_all_videos(self, max_pages: int | None = None) -> list[Video]:
        """
        Extract all video links from the webpage, supporting multiple pages.

        :param max_pages: Maximum number of pages to scrape.
            If None, automatically detects from pagination links.
        :return: List of Video objects
        :raises requests.RequestException: If the page cannot be fetched
        """
        all_videos = []

        # Fetch first page to determine max_pages if not specified
        if max_pages is None:
            print("Fetching first page to detect total pages...")
            html = self.fetch_page(self.url)
            max_pages = self._get_max_pages(html)
            print(f"Detected {max_pages} total pages")

            # Process the first page we already fetched
            videos_from_page = self.extract_videos(html)
            print(f"Page 1/{max_pages}: Found {len(videos_from_page)} videos ({_summarize_types(videos_from_page)})")
            all_videos.extend(videos_from_page)

            # Start from page 2
            start_page = 2
        else:
            start_page = 1

        for page_num in range(start_page, max_pages + 1):
            # Construct URL for the current page
            parsed = urlparse(self.url)
            query_params = parse_qs(parsed.query)
            query_params["page"] = [str(page_num)]

            # Rebuild the URL with the updated page parameter
            new_query = urlencode(query_params, doseq=True)
            page_url = urlunparse(
                (
                    parsed.scheme,
                    parsed.netloc,
                    parsed.path,
                    parsed.params,
                    new_query,
                    parsed.fragment,
                )
            )

            html = self.fetch_page(page_url)

            videos_from_page = self.extract_videos(html)

            if not videos_from_page:
                print(f"Page {page_num}/{max_pages}: No videos found.")
                continue

            print(
                f"Page {page_num}/{max_pages}: Found {len(videos_from_page)} videos ({_summarize_types(videos_from_page)})"
            )
            all_videos.extend(videos_from_page)

        print(f"\nTotal videos found: {len(all_videos)}")
        return all_videos


def _summarize_types(videos: list[Video]) -> str:
    """
    Summarize how many videos of each platform type were found.

    :param videos: Videos found on a single page
    :return: Comma-separated summary such as "12 Vimeo, 3 YouTube"
    """
    counts = []
    for video_type, label in PLATFORM_LABELS.items():
        count = sum(1 for v in videos if v.type == video_type)
        if count > 0:
            counts.append(f"{count} {label}")
    return ", ".join(counts)
