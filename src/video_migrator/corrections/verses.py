#!/usr/bin/env python3
"""
Judge a bible reference recorded against a sermon.

The books of the bible are a closed set, which is what makes this checkable at
all: a name that is not one of them is wrong, and a name one edit from exactly
one of them is a typo whose correction needs no lookup. Where two books are
equally close the rule refuses rather than guesses, because the character it
would have to guess is the one that distinguishes them.
"""

import re
from collections.abc import Collection

#: The books of the bible in Korean. A closed set, so anything outside it is a
#: misspelling — which is the only exhaustive check available here, there being
#: no Korean spell checker in this project.
#: Kept as wrapped text rather than 66 quoted strings: the books read in their
#: canonical order this way, which is how a reader checks the list is complete.
KOREAN_BOOKS = frozenset("""창세기 출애굽기 레위기 민수기 신명기 여호수아 사사기 룻기 사무엘상 사무엘하
열왕기상 열왕기하 역대상 역대하 에스라 느헤미야 에스더 욥기 시편 잠언 전도서 아가 이사야 예레미야
예레미야애가 에스겔 다니엘 호세아 요엘 아모스 오바댜 요나 미가 나훔 하박국 스바냐 학개 스가랴 말라기
마태복음 마가복음 누가복음 요한복음 사도행전 로마서 고린도전서 고린도후서 갈라디아서 에베소서 빌립보서
골로새서 데살로니가전서 데살로니가후서 디모데전서 디모데후서 디도서 빌레몬서 히브리서 야고보서
베드로전서 베드로후서 요한일서 요한이서 요한삼서 유다서 요한계시록""".split())  # noqa: SIM905

#: What a field holds when nobody filled it in.
PLACEHOLDERS = ("", ".", "-", "..", "?")

#: The leading book name, allowing the space that is sometimes typed inside it.
BOOK = re.compile(r"[\s(\[]*([가-힣]+(?:\s[가-힣]+)?)")

#: A chapter and verse, as ``3:16``, or a chapter alone, as ``23편``.
CHAPTER = re.compile(r"\d+\s*[:：]\s*\d+|\d+\s*(?:편|장)")

#: A semicolon typed where a colon belongs, between two digits. Only between
#: digits: a semicolon *is* how two separate passages are joined.
MISTYPED_COLON = re.compile(r"(\d);(\d)")

#: Brackets that wrap a reference, and the character each is closed with.
BRACKET_PAIRS = {"(": ")", "[": "]", "{": "}", "<": ">", "〈": "〉", "《": "》", "「": "」"}

#: Brackets round the book name alone, leaving the chapter outside them —
#: ``(John) 12:12-16``. The brackets say nothing the reference does not, and the
#: board's style has none, so the name comes out of them and stays put.
BRACKETED_BOOK = re.compile(r"^[(\[<]([^)\]>]+)[)\]>]")

#: Wrappers written the same at both ends, so a pair cannot be told from a
#: stray one by shape alone — only by appearing at both ends at once.
SYMMETRIC_WRAPPERS = "-–—\"'"

#: A tilde standing in for the hyphen a range is written with here. Between
#: digits only, for the same reason :data:`MISTYPED_COLON` is.
TILDE_RANGE = re.compile(r"(\d)\s*[~∼]\s*(\d)")

#: How a range is joined when it is written out in words.
SPELLED_RANGE = r"에서|부터|로|~|-|–|—"

#: A number standing for a verse: it says ``절``, or it says nothing and is not
#: a chapter. Without the second half, ``3장 1장에서 10장`` — a record that types
#: 장 where it means 절 — reads as verse 1 of chapter 3 and half-converts to
#: ``3:1장에서 10장``. Left whole it is merely wrong, which a reader can see.
VERSE_NUMBER = r"(\d+)\s*(?:절|(?![\s]*[장편]))"

#: A reference written out in words — ``2장 1절에서 10절``, ``2장 1-10절`` —
#: rather than as ``2:1-10``. Either ``절`` may be left implied; the range is
#: optional too, and so is a second chapter inside it, which is what tells
#: ``1장 26절에서 2장 3절`` from ``1장 26절에서 30절``.
SPELLED_REFERENCE = re.compile(
    rf"(\d+)\s*장\s*{VERSE_NUMBER}"
    rf"(?:\s*(?:{SPELLED_RANGE})\s*(?:(\d+)\s*장\s*)?{VERSE_NUMBER}(?:\s*까지)?)?"
)

#: A verse named on its own, after a chapter has already been given — the second
#: half of ``23장 9절, 29절``. Only read where the value already cites a chapter
#: in figures, so ``절`` is never stripped from a reference written entirely in
#: words and left half-converted.
TRAILING_VERSE = re.compile(r"(\d)\s*절")


def one_edit_apart(spelled: str, book: str) -> bool:
    """
    Is one string reachable from the other by a single character edit?

    Korean book names are mistyped a syllable at a time — a wrong vowel, a
    doubled or dropped syllable — so a single edit covers what is seen in
    practice without reaching far enough to confuse two real books.

    :param spelled: The name as it was written
    :param book: A book of the bible to compare it against
    :return: True when they differ by at most one substitution, insertion or deletion

    >>> one_edit_apart("요한복은", "요한복음")
    True
    >>> one_edit_apart("요한복음", "요한계시록")
    False
    """
    if abs(len(spelled) - len(book)) > 1:
        return False
    if len(spelled) == len(book):
        return sum(a != b for a, b in zip(spelled, book, strict=True)) == 1
    longer, shorter = (spelled, book) if len(spelled) > len(book) else (book, spelled)
    return any(longer[:i] + longer[i + 1:] == shorter for i in range(len(longer)))


def cites_chapter(word: str) -> bool:
    """
    Does a reference name a chapter, either as ``3:16`` or as ``23편``?

    :param word: The reference as the site stores it
    :return: True when a chapter reference is present

    >>> cites_chapter("요한복음 3:16")
    True
    >>> cites_chapter("요한복음")
    False
    """
    return bool(CHAPTER.search(word))


def unwrap(word: str) -> str:
    """
    Take punctuation off a reference that is wrapped in it.

    A bracket is only a wrapper where it has no partner to belong to. ``(John)``
    names a book, and taking its opening bracket off because the value happens
    not to end in one leaves a reference that reads as damage — so a bracket
    goes only as half of a matched pair, or when nothing in the value could have
    matched it.

    :param word: The reference as the site stores it
    :return: The same reference with any wrapping punctuation removed

    >>> unwrap("<요한복음 6:15-21>")
    '요한복음 6:15-21'
    >>> unwrap("(John) 12:12-16")
    '(John) 12:12-16'
    >>> unwrap("에베소서 3:6-13)")
    '에베소서 3:6-13'
    """
    text = word.strip()
    while len(text) > 1:
        first, last = text[0], text[-1]
        if BRACKET_PAIRS.get(first) == last or (first == last and first in SYMMETRIC_WRAPPERS):
            text = text[1:-1].strip()
        elif first in BRACKET_PAIRS and BRACKET_PAIRS[first] not in text:
            text = text[1:].strip()
        elif last in BRACKET_PAIRS.values() and not any(
                opener in text for opener, closer in BRACKET_PAIRS.items() if closer == last):
            text = text[:-1].strip()
        else:
            return text
    return text


def in_figures(match: re.Match[str]) -> str:
    """
    Rewrite one spelled-out reference as figures.

    :param match: A :data:`SPELLED_REFERENCE` match
    :return: The same reference as ``3:16``, or ``3:16-18`` for a range

    >>> SPELLED_REFERENCE.sub(in_figures, "2장 1절에서 10절")
    '2:1-10'
    >>> SPELLED_REFERENCE.sub(in_figures, "1장 26절에서 2장 3절")
    '1:26-2:3'
    >>> SPELLED_REFERENCE.sub(in_figures, "9장 14-29절")
    '9:14-29'
    """
    chapter, verse, end_chapter, end_verse = match.groups()
    if not end_verse:
        return f"{chapter}:{verse}"
    return f"{chapter}:{verse}-{end_chapter}:{end_verse}" if end_chapter else f"{chapter}:{verse}-{end_verse}"


def check_verse(
    word: str,
    ask: str,
    books: Collection[str] = KOREAN_BOOKS,
    placeholders: Collection[str] = PLACEHOLDERS,
) -> tuple[str, str] | None:
    """
    Judge one bible reference.

    A reference can be wrong in more than one way at once — ``마가복음9;1-8`` is
    three of them — so the mechanical rewrites are applied in turn and their
    reasons joined, rather than the first one found being reported alone. A row
    fixed one defect per pass would need one apply-and-rescrape round for each.

    :param word: The reference as the site stores it
    :param ask: What to suggest when only a person can settle it
    :param books: The book names to accept
    :param placeholders: Values that mean the field was never filled in
    :return: (reason, suggested value), or None when nothing is wrong

    >>> check_verse("요한복음 3:16", "look it up")
    >>> check_verse("요한복은 3:16", "look it up")
    ('요한복은 is one letter from 요한복음', '요한복음 3:16')
    >>> check_verse("출애굽기 2장 1절에서 10절", "look it up")
    ('chapter and verse spelled out', '출애굽기 2:1-10')
    >>> check_verse("", "look it up")
    ('missing', 'look it up')
    """
    if word in placeholders:
        return "missing", ask

    reasons: list[str] = []
    fixed = word

    unwrapped = unwrap(fixed)
    if unwrapped != fixed:
        fixed = unwrapped
        reasons.append("punctuation wrapped round the reference")

    # Run after unwrapping, so a reference wrapped whole is already bare and only
    # brackets holding the book name on its own are left to find.
    debracketed = BRACKETED_BOOK.sub(r"\1", fixed)
    if debracketed != fixed:
        fixed = debracketed
        reasons.append("brackets round the book name")

    if TILDE_RANGE.search(fixed):
        fixed = TILDE_RANGE.sub(r"\1-\2", fixed)
        reasons.append("tilde where a hyphen belongs")

    if MISTYPED_COLON.search(fixed):
        fixed = MISTYPED_COLON.sub(r"\1:\2", fixed)
        reasons.append("semicolon where a colon belongs")

    in_words = SPELLED_REFERENCE.sub(in_figures, fixed)
    if ":" in in_words:
        in_words = TRAILING_VERSE.sub(r"\1", in_words)
    if in_words != fixed:
        fixed = in_words
        reasons.append("chapter and verse spelled out")

    found = BOOK.match(fixed)
    if found:
        spelled = found.group(1)
        closed = spelled.replace(" ", "")
        if closed in books and spelled != closed:
            fixed = fixed.replace(spelled, closed, 1)
            reasons.append("space inside the book name")
        elif len(closed) > 2 and closed not in books and not closed.startswith(tuple(books)):
            near = sorted(book for book in books if one_edit_apart(closed, book))
            corrected = fixed.replace(spelled, near[0], 1) if len(near) == 1 else ""
            if not (corrected and cites_chapter(corrected)):
                return f"{closed!r} is not a book of the bible", ask
            fixed = corrected
            reasons.append(f"{closed} is one letter from {near[0]}")

    # Re-matched, because closing up a space inside the name moves where it ends.
    found = BOOK.match(fixed)
    if found and found.group(1) in books and fixed[found.end():found.end() + 1].isdigit():
        fixed = f"{fixed[:found.end()]} {fixed[found.end():]}"
        reasons.append("no space between the book and the chapter")

    if not cites_chapter(fixed):
        return "no chapter:verse", ask
    return ("; ".join(reasons), fixed) if reasons else None
