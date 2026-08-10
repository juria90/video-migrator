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


def check_verse(
    word: str,
    ask: str,
    books: Collection[str] = KOREAN_BOOKS,
    placeholders: Collection[str] = PLACEHOLDERS,
) -> tuple[str, str] | None:
    """
    Judge one bible reference.

    :param word: The reference as the site stores it
    :param ask: What to suggest when only a person can settle it
    :param books: The book names to accept
    :param placeholders: Values that mean the field was never filled in
    :return: (reason, suggested value), or None when nothing is wrong

    >>> check_verse("요한복음 3:16", "look it up")
    >>> check_verse("요한복은 3:16", "look it up")
    ('요한복은 is one letter from 요한복음', '요한복음 3:16')
    >>> check_verse("", "look it up")
    ('missing', 'look it up')
    """
    if word in placeholders:
        return "missing", ask
    if MISTYPED_COLON.search(word):
        return "semicolon where a colon belongs", MISTYPED_COLON.sub(r"\1:\2", word)

    found = BOOK.match(word)
    if found:
        spelled = found.group(1)
        closed = spelled.replace(" ", "")
        if closed in books and spelled != closed:
            return "space inside the book name", word.replace(spelled, closed, 1)
        if len(closed) > 2 and closed not in books and not closed.startswith(tuple(books)):
            near = sorted(book for book in books if one_edit_apart(closed, book))
            fixed = word.replace(spelled, near[0], 1) if len(near) == 1 else ""
            if fixed and cites_chapter(fixed):
                return f"{closed} is one letter from {near[0]}", fixed
            return f"{closed!r} is not a book of the bible", ask
    if not cites_chapter(word):
        return "no chapter:verse", ask
    return None
