#!/usr/bin/env python3
"""
Scraper for the VOD module of the 교회사랑넷 (church-love.net) church CMS.

The CMS serves every page through one script: a board is a ``pageCode`` on
``/main/sub.html`` and one recording is a ``num`` within it. Its chrome is drawn
by JavaScript, but the part that matters is not — the listing arrives as plain
server-rendered HTML, a grid of ``div.jmBroad_box`` tiles, so no browser is
needed to walk a board.

The tile is only half the record. It carries the ``num`` and ``vodType`` of a
recording but not the video URL, which the player fetches separately from
``/core/xml/vod/vodInfo.xml.html`` — an XML endpoint that answers POST only.
That response is authoritative for the metadata as well (title, passage,
preacher, date), so a page costs one listing fetch plus one POST per recording,
and nothing is read out of the tile except the id.

Which site it points at comes from the site profile, not from this module.
"""

import re
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ..config import DEFAULT_PROFILE, load_profile
from ..models import Video
from ..utils.http_cache import HTTPCache
from .platforms import identify, summarize_types

#: Path every page of the CMS is served from, boards and recordings alike.
BOARD_PATH = "/main/sub.html"

#: Path of the XML endpoint that resolves one recording to its video URL.
VOD_INFO_PATH = "/core/xml/vod/vodInfo.xml.html"

#: Pulls the recording id and its board's media type out of a tile's onclick URL.
_ENTRY_LINK = re.compile(r"num=(\d+)[^'\"]*?vodType=(\d+)")


@dataclass(frozen=True)
class Entry:
    """One tile on a board listing: enough to fetch the recording behind it."""

    num: str
    vod_type: str
    #: The tile's title, used only to name the recording in warnings.
    label: str


class ChurchLoveScraper:
    """
    Scrape video links and metadata from a church-love.net VOD board.

    Walks a board's paginated tile grid and resolves every recording it finds to
    the platform hosting it (YouTube, Vimeo, SoundCloud).
    """

    @staticmethod
    def board_url(board: str, profile_name: str = DEFAULT_PROFILE) -> str:
        """
        Build the listing URL for a board on the profile's site.

        :param board: Board name, one of ``load_profile(profile_name).board_names``
        :param profile_name: Site profile supplying the base URL and page codes
        :return: Fully qualified board listing URL
        :raises ValueError: If the board declares no ``page_code``, which this
            CMS needs to address a board at all
        """
        profile = load_profile(profile_name)
        page_code = profile.board(board).page_code
        if not page_code:
            raise ValueError(
                f"profile {profile_name!r}: board {board!r} declares no 'page_code'. "
                f"This CMS addresses a board by page code, not by name."
            )
        return f"{profile.board_url}?pageCode={page_code}"

    def __init__(self, url: str, genre: str, language: str = "", cache_dir: str = ".cache", cache_duration: int = 3600):
        """
        Initialize the scraper with a board listing URL.

        :param url: Board listing URL, as built by :func:`board_url`
        :param genre: Genre for the videos (e.g., "Sermon", "Praise")
        :param language: Language of the videos (e.g., "kor", "eng")
        :param cache_dir: Directory to store cached responses
        :param cache_duration: Cache duration in seconds (default: 3600 = 1 hour)
        """
        self.url = url
        parsed = urlparse(url)
        self.base_url = f"{parsed.scheme}://{parsed.netloc}"
        self.page_code = parse_qs(parsed.query).get("pageCode", [""])[0]
        self.http_cache = HTTPCache(cache_dir, cache_duration)
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
            }
        )
        self.genre = genre
        self.language = language

    def page_url(self, page_num: int) -> str:
        """
        Build the URL of one page of this board's listing.

        :param page_num: 1-based page number
        :return: Fully qualified listing URL for that page

        >>> scraper = ChurchLoveScraper("https://example.org/main/sub.html?pageCode=9", genre="Sermon")
        >>> scraper.page_url(3)
        'https://example.org/main/sub.html?pageCode=9&page=3'
        """
        query = urlencode({"pageCode": self.page_code, "page": page_num})
        return f"{urljoin(self.base_url, BOARD_PATH)}?{query}"

    def fetch_page(self, url: str | None = None) -> str:
        """
        Fetch a board listing page, with caching support.

        :param url: URL to fetch. If None, uses self.url
        :return: HTML content of the page
        :raises requests.RequestException: If the page cannot be fetched
        """
        if url is None:
            url = self.url

        query = parse_qs(urlparse(url).query)
        page_code = query.get("pageCode", ["unknown"])[0]
        page_num = query.get("page", ["1"])[0]
        content, _ = self.http_cache.fetch_with_cache(url, f"pageCode{page_code}_page{page_num}", self.session)
        return content

    def fetch_vod_info(self, entry: Entry) -> dict[str, str] | None:
        """
        Resolve one recording to its video URL and metadata.

        :param entry: The listing tile identifying the recording
        :return: The endpoint's fields (``vodFile``, ``subject``, ``word``,
            ``preacher``, ``date``, ...), or None if it returned no record
        :raises requests.RequestException: If the endpoint cannot be reached
        """
        payload = {"pageCode": self.page_code, "num": entry.num, "vodType": entry.vod_type}
        content, _ = self.http_cache.fetch_with_cache(
            urljoin(self.base_url, VOD_INFO_PATH),
            f"pageCode{self.page_code}_num{entry.num}",
            self.session,
            data=payload,
        )
        return _parse_vod_info(content)

    def extract_videos(self, html: str) -> list[Video]:
        """
        Extract every video on one board listing page.

        Each tile costs one request to the VOD endpoint, which is where the
        video URL lives.

        :param html: HTML content of a listing page
        :return: List of Video objects
        """
        return [video for entry in extract_entries(html) if (video := self.resolve(entry))]

    def resolve(self, entry: Entry) -> Video | None:
        """
        Turn one listing tile into a :class:`~video_migrator.models.Video`.

        :param entry: The listing tile to resolve
        :return: The video, or None if the recording is gone or is hosted
            somewhere this package cannot resolve
        """
        try:
            info = self.fetch_vod_info(entry)
        except requests.RequestException as e:
            print(f"Warning: could not fetch recording {entry.num} ({entry.label}): {e}")
            return None

        if info is None:
            print(f"Warning: recording {entry.num} ({entry.label}) has no VOD record; skipping")
            return None

        vod_file = info.get("vodFile", "")
        platform = identify(vod_file)
        if platform is None:
            print(f"Warning: recording {entry.num} ({entry.label}) has no resolvable video URL {vod_file!r}; skipping")
            return None

        video_type, video_id, video_url = platform
        return Video(
            type=video_type,
            id=video_id,
            url=video_url,
            embed_url=vod_file,
            title=info.get("subject", ""),
            bible_verse=info.get("word", ""),
            publish_date=info.get("date", ""),
            artist=info.get("preacher", ""),
            genre=self.genre,
            language=self.language,
            num=entry.num,
        )

    def get_all_videos(self, max_pages: int | None = None) -> list[Video]:
        """
        Extract all videos from the board, walking its pages in order.

        The CMS paginator only ever links the current block of page numbers, so
        the total is not knowable up front: pages are walked until one comes
        back empty.

        :param max_pages: Maximum number of pages to scrape. If None, walks to
            the end of the board
        :return: List of Video objects
        :raises requests.RequestException: If a listing page cannot be fetched
        """
        all_videos: list[Video] = []
        seen: set[str] = set()
        page_num = 1

        while max_pages is None or page_num <= max_pages:
            position = f"Page {page_num}" if max_pages is None else f"Page {page_num}/{max_pages}"
            html = self.fetch_page(self.page_url(page_num))
            entries = extract_entries(html)

            if not entries:
                print(f"{position}: No entries found; end of board.")
                break

            # A page past the end can come back as a repeat of an earlier one
            # rather than as an empty page; that is still the end of the board.
            fresh = [entry for entry in entries if entry.num not in seen]
            if not fresh:
                print(f"{position}: Only already-seen entries; end of board.")
                break
            seen.update(entry.num for entry in entries)

            videos_from_page = [video for entry in fresh if (video := self.resolve(entry))]
            print(f"{position}: Found {len(videos_from_page)} videos ({summarize_types(videos_from_page)})")
            all_videos.extend(videos_from_page)
            page_num += 1

        print(f"\nTotal videos found: {len(all_videos)}")
        return all_videos


def board_url(board: str, profile_name: str = DEFAULT_PROFILE) -> str:
    """
    Build the listing URL for a board on the profile's site.

    :param board: Board name, one of ``load_profile(profile_name).board_names``
    :param profile_name: Site profile supplying the base URL and page codes
    :return: Fully qualified board listing URL
    :raises ValueError: If the board declares no ``page_code``

    >>> board_url("sunday_sermon", "churchlove")
    'https://example.org/main/sub.html?pageCode=9'
    """
    return ChurchLoveScraper.board_url(board, profile_name)


def extract_entries(html: str) -> list[Entry]:
    """
    Read the recording ids off one board listing page.

    :param html: HTML content of a listing page
    :return: One entry per tile, in the order the page lists them

    >>> tile = '''
    ...   <div class="jmBroad_box"><ul><div><p>
    ...     <strong onclick="javascript:document.location.href='/main/sub.html?num=1234&pageCode=9&Mode=view&vodType=1&page=1';">설교 제목</strong>
    ...   </p></div></ul></div>'''
    >>> extract_entries(tile)
    [Entry(num='1234', vod_type='1', label='설교 제목')]
    """
    soup = BeautifulSoup(html, "html.parser")
    entries = []

    for box in soup.find_all("div", class_="jmBroad_box"):
        # The tile has no href — every link is an onclick assigning location.href.
        match = _ENTRY_LINK.search(str(box))
        if not match:
            continue
        subject = box.find("strong")
        entries.append(
            Entry(
                num=match.group(1),
                vod_type=match.group(2),
                label=subject.get_text(strip=True) if subject else "",
            )
        )

    return entries


def _parse_vod_info(xml: str) -> dict[str, str] | None:
    """
    Parse a ``vodInfo.xml.html`` response into its fields.

    A recording that has been deleted answers with a JavaScript ``alert()``
    rather than an XML error, so unparseable content means "no such recording".

    :param xml: Raw response body
    :return: Field name -> text, or None if the response holds no record

    >>> _parse_vod_info('<result><data><vodInfo><subject>제목</subject></vodInfo></data></result>')
    {'subject': '제목'}
    >>> _parse_vod_info("<script> window.alert('deleted'); </script>") is None
    True
    """
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        return None

    info = root.find(".//vodInfo")
    if info is None:
        return None
    return {child.tag: (child.text or "").strip() for child in info}
