#!/usr/bin/env python3
"""
Write corrections back through the 교회사랑넷 admin, driving a real browser.

The admin encrypts credentials before posting and guards every save with a token
its own JavaScript negotiates. Reproducing either in a plain HTTP client means
reimplementing a security control and getting it exactly right; a browser simply
runs it. So this drives Chromium and lets the page do what it already does.

Requires the optional ``playwright`` dependency, so nothing imports this module
unless it is about to write. Which site is being written to, and with whose
credentials, arrives from a caller — a second church on this CMS needs no change
here.
"""

import re
import time

import requests
from playwright.sync_api import sync_playwright  # noqa: F401  (re-exported for callers)

#: Ledger field name -> the form input that holds it, and the tag the public
#: endpoint reports it under. The CMS happens to use one name for both.
FIELD_INPUT = {"title": "subject", "verse": "word", "preacher": "preacher"}

#: The fields worth reading back when confirming a save.
PUBLIC_FIELDS = ("subject", "word", "preacher", "date")

LOGIN_PATH = "/core/admin/login/login.php"
WRITE_PATH = "/core/admin/vod/write.php"


#: How many times to ask the public endpoint before giving up on a record, and
#: how long to wait after each failure. A correction run makes two of these reads
#: per record and takes hours over a few hundred, so a connection dropped once —
#: which this site does — must not be what decides the run is over.
READ_ATTEMPTS = 4
READ_BACKOFF = 5


def public_record(endpoint: str, num: str, page_code: str, vod_type: str = "1") -> dict[str, str]:
    """
    Read a record from the public endpoint, independently of the admin session.

    Reading back from where visitors read means a change is confirmed against
    what the site actually serves, rather than against the status code of the
    request that made it.

    A dropped connection is retried rather than raised, because the read is not
    the work — it is the confirmation of work already done, and a run that dies
    on one is a run whose completed edits go unrecorded.

    :param endpoint: The public record endpoint to POST to
    :param num: Record id
    :param page_code: Board the record belongs to
    :param vod_type: The board's media type
    :return: The record's public fields
    :raises requests.RequestException: If the endpoint cannot be reached at all
    """
    for attempt in range(1, READ_ATTEMPTS + 1):
        try:
            body = requests.post(
                endpoint,
                data={"pageCode": page_code, "num": num, "vodType": vod_type},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=30,
            ).text
            break
        except requests.RequestException as error:
            if attempt == READ_ATTEMPTS:
                raise
            print(f"      reading num={num} failed ({type(error).__name__}), "
                  f"retrying in {READ_BACKOFF * attempt}s")
            time.sleep(READ_BACKOFF * attempt)
    out = {}
    for tag in PUBLIC_FIELDS:
        found = re.search(rf"<{tag}><!\[CDATA\[(.*?)\]\]></{tag}>", body, re.S)
        out[tag] = found.group(1).strip() if found else ""
    return out


class AdminSession:
    """A logged-in admin session backed by a real browser."""

    def __init__(self, page, admin_url: str, page_code: str):
        """
        :param page: The Playwright page to drive
        :param admin_url: Root of the admin site
        :param page_code: Board every edit belongs to
        """
        self.page = page
        self.admin_url = admin_url.rstrip("/")
        self.page_code = page_code

    def login(self, admin_id: str, password: str) -> None:
        """
        Sign in, letting the page's own script encrypt the credentials.

        :param admin_id: Admin account id
        :param password: Admin account password
        :raises SystemExit: If the admin form is still out of reach afterwards
        """
        self.page.goto(f"{self.admin_url}{LOGIN_PATH}", wait_until="domcontentloaded")
        self.page.fill('input[name="id"]', admin_id)
        self.page.fill('input[name="password"]', password)
        self.page.evaluate("loginCheck()")
        self.page.wait_for_timeout(3000)
        if "login" in self.page.url.lower():
            raise SystemExit("still on the login page — check the credentials")

    def open_record(self, num: str) -> None:
        """
        Load one record's edit form.

        :param num: Record id
        :raises SystemExit: If the form does not appear, meaning the session lapsed
        """
        self.page.goto(
            f"{self.admin_url}{WRITE_PATH}?page=&num={num}&pageCode={self.page_code}&category=&keyfield=&key=",
            wait_until="domcontentloaded")
        if not self.page.query_selector('form[name="Write_Form"]'):
            raise SystemExit(f"no edit form for num={num} — session may have expired")

    def read_field(self, input_name: str) -> str:
        """
        Read one field as the form currently holds it.

        :param input_name: The form input's name
        :return: Its value
        """
        return self.page.input_value(f'[name="{input_name}"]')

    def save(self, num: str, changes: dict[str, str]) -> None:
        """
        Set every named field and submit once.

        A record often needs two or three fields corrected. Setting them in one
        pass means one page load and, more importantly, one save — three saves
        against a live site to fix one record is needless load and three chances
        for the token handshake to fail midway.

        :param num: Record id, for the message on failure
        :param changes: Form input name -> new value
        :raises SystemExit: If the admin rejects the save
        """
        rejected = {}
        self.page.once("dialog", lambda d: (rejected.setdefault("why", d.message), d.accept()))
        for input_name, value in changes.items():
            # The page nests its two forms badly, so the inputs are not DOM
            # descendants of the form that owns them. Select by name alone.
            self.page.fill(f'[name="{input_name}"]', value)
        self.page.evaluate("checkValue()")   # the page's own save handler
        self.page.wait_for_timeout(4000)
        if rejected:
            raise SystemExit(f"num={num}: the admin refused the save — {rejected['why'].strip()}")
