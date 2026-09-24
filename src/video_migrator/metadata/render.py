#!/usr/bin/env python3
"""
Fill a profile's template, dropping the separator in front of every empty slot.

Both the upload title and the upload description are assembled this way, from
templates a profile supplies over slots a recording may or may not have. The
rule they share is what lives here: a record with no preacher, no service or no
summary should not publish carrying the punctuation that would have set one off.
"""

from string import Formatter

#: Characters that count as separator rather than as content, and so are dropped
#: from the front of a result whose leading slots were all empty. A label is not
#: among them: the literal before ``{verse}`` in ``"{summary}\n\n본문: {verse}"``
#: is a blank line *and* a label, and only the blank line goes.
SEPARATORS = " \t\n\r|-–—:·,;/\\]})>"


def render(template: str, values: dict[str, str]) -> str:
    """
    Fill a template, dropping the separator in front of every empty slot.

    The literal before a slot belongs to that slot and goes with it. Where the
    slots that would have come first are all empty, the literal introducing the
    first surviving slot keeps whatever of it is not separator — so a template
    that labels its slots keeps the label and loses only the punctuation that
    was joining it to what is no longer there. The literal opening the template
    is a prefix on the whole result and survives only while its own slot does.

    :param template: Format string over the keys of ``values``
    :param values: Slot name -> its rendered value, empty where it has none
    :return: The filled template, stripped

    >>> render("{date} | {service} | {title}", {"date": "2026.8.2", "service": "", "title": "설교 제목"})
    '2026.8.2 | 설교 제목'

    The opening literal goes with the slot it introduces:

    >>> render("[{church}] {title}", {"church": "", "title": "설교 제목"})
    '설교 제목'
    >>> render("[{church}] {title}", {"church": "예시교회", "title": "설교 제목"})
    '[예시교회] 설교 제목'

    A label in front of a slot is content, not separator, and stays even when
    everything before it went:

    >>> render("{summary}\\n\\n본문: {verse}", {"summary": "", "verse": "요 21:15"})
    '본문: 요 21:15'

    A template every slot of which is empty renders as nothing at all:

    >>> render("{a} | {b}", {"a": "", "b": ""})
    ''
    """
    kept: list[tuple[str, str]] = []
    trailing = ""
    for index, (literal, field, _spec, _conversion) in enumerate(Formatter().parse(template)):
        if field is None:
            trailing = literal
            continue
        value = values.get(field, "").strip()
        if value:
            # The first slot to survive opens the result. A literal that opened
            # the template opens it too and is kept whole; one that was joining
            # this slot to an earlier, empty one keeps only its non-separator
            # part, which is where a label survives and its punctuation does not.
            opening = kept or index == 0
            kept.append((literal if opening else literal.lstrip(SEPARATORS), value))

    if not kept:
        return ""
    return "".join(literal + value for literal, value in kept).strip() + trailing.rstrip()
