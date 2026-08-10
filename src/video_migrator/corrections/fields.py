#!/usr/bin/env python3
"""
Judge the preacher and title fields recorded against a sermon.

Neither rule knows a site's conventions. Which honorifics count as a title, and
how a service part is written into a title, are decisions a church made and are
passed in from its profile — so the same code serves the next church on the same
CMS without being edited.
"""

import re
from collections.abc import Collection

from ..metadata.normalize import SERVICE_PART_TITLE_FORMAT
from .verses import PLACEHOLDERS

#: An affiliation in brackets at the end of a name, which is not part of it.
AFFILIATION = re.compile(r"\s*\([^()]*\)$")

#: A service part left on the end of the preacher field, where it does not belong.
PART_IN_PREACHER = re.compile(r"[_\s]*([123]부|영상)(?:설교|예배)?\s*$")

#: Ways a service part gets written into a title instead of the agreed form.
#: Each captures the part and the rest of the title, in whichever order.
OFF_STYLE = (
    re.compile(r"^\(?([123]부)(?:예배)?\)\s*(.+)$"),
    re.compile(r"^([123]부)\s*-\s*(.+)$"),
    re.compile(r"^([123]부)\s+(.+)$"),
    re.compile(r"^(.+?)\s+([123]부)$"),
)

#: A service part on its own, used to tell which capture group holds which.
SERVICE_PART = re.compile(r"[123]부")


def check_preacher(
    preacher: str,
    ask: str,
    titles: Collection[str],
    prefixes: Collection[str],
    placeholders: Collection[str] = PLACEHOLDERS,
) -> tuple[str, str] | None:
    """
    Judge one preacher field.

    A name is accepted on the strength of its honorific alone. That is a weak
    test, and deliberately so: it catches a field holding something that is not
    a name at all, without pretending to know who may preach.

    :param preacher: The name as the site stores it
    :param ask: What to suggest when only a person can settle it
    :param titles: Honorifics a name may end with, as the profile lists them
    :param prefixes: Honorifics a name may begin with, as the profile lists them
    :param placeholders: Values that mean the field was never filled in
    :return: (reason, suggested value), or None when nothing is wrong

    >>> check_preacher("홍길동 목사", "look it up", (" 목사",), ("Rev.",))
    >>> check_preacher("홍길동", "look it up", (" 목사",), ("Rev.",))
    ('name carries no title', 'look it up')
    """
    if preacher in placeholders:
        return "missing", ask
    name = AFFILIATION.sub("", preacher).strip()
    if not (name.endswith(tuple(titles)) or name.startswith(tuple(prefixes))):
        return "name carries no title", ask
    return None


def check_title(
    title: str,
    preacher: str,
    part_format: str = SERVICE_PART_TITLE_FORMAT,
) -> tuple[str, str] | None:
    """
    Judge one title, and where the service part sits in it.

    A church holding more than one service on a Sunday distinguishes the
    recordings by part. Where that part is written varies by whoever typed it —
    into the preacher field, or into the title in any of several forms — and
    this settles it into one.

    :param title: The title as the site stores it
    :param preacher: The preacher field, which sometimes holds the service part
    :param part_format: How a part is written into a title, given ``title`` and ``part``
    :return: (reason, suggested value), or None when nothing is wrong

    >>> check_title("설교 제목 (1부)", "홍길동 목사")
    >>> check_title("1부 - 설교 제목", "홍길동 목사")
    ('service part not in the agreed form', '설교 제목 (1부)')
    >>> check_title("설교 제목", "홍길동 목사 1부설교")
    ('service part is in the preacher field', '설교 제목 (1부)')
    """
    if "  " in title:
        return "double space", re.sub(r"\s{2,}", " ", title)

    stray = PART_IN_PREACHER.search(preacher)
    if stray and stray.group(1) not in title:
        return "service part is in the preacher field", part_format.format(title=title, part=stray.group(1))

    for pattern in OFF_STYLE:
        found = pattern.match(title)
        if found:
            part, rest = ((found[1], found[2]) if SERVICE_PART.fullmatch(found[1]) else (found[2], found[1]))
            return "service part not in the agreed form", part_format.format(title=rest, part=part)
    return None
