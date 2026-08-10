#!/usr/bin/env python3
"""
Check that no data identifying the real source site can reach a commit.

The rule this enforces is in CLAUDE.md; nothing else checks it, and three manual
reviews in a row still missed leaks. Every one arrived the same way — reaching
for a real example while writing docs or a test, because the real one was to
hand and illustrated the point better.

Three checks, all offline so that running them stays free:

1. **Korean text against an allowlist.** The repo needs Korean — placeholder
   names, the titles a profile matches on, the CMS field labels a fixture has to
   reproduce — so "any Hangul" would be unusable. An allowlist inverts it:
   introducing a new Korean word becomes a deliberate act, which is exactly the
   moment to ask whether it came off the real site.

2. **Terms from a file this repo cannot contain.** The site's own domain and the
   names on it cannot be listed here, or this file becomes the leak. They live in
   an ignored file instead, and the check quietly skips when it is absent so a
   fresh clone is never blocked by something it cannot have.

3. **Platform ids that could resolve.** An id can look invented and still belong
   to a stranger: 111222333 does. Requiring ids to sit outside the range any
   platform has assigned makes "does not resolve" a property of the shape rather
   than something to remember to verify.

Run by ``make lint``. Exits non-zero on the first file with a finding.
"""

import pathlib
import re
import subprocess
import sys

#: Directories holding the real site's data, ignored by git and never inspected.
EXCLUDED_PREFIXES = ("config/", "data/")

#: Korean this repo is expected to contain: placeholder names, the vocabulary a
#: profile matches on, and the CMS field labels a fixture has to reproduce.
#: Anything outside this list has to be justified before being added to it.
ALLOWED_HANGUL = {
    # Placeholder people and places
    "홍길동",
    "김영희",
    "홍길동목사",
    "김영희목사",
    "예시교회",
    # Placeholder titles, built from these pieces
    "설교",
    "제목",
    "시리즈",
    "찬양",
    "부제",
    "하나",
    "둘",
    "셋",
    "넷",
    "다섯",
    "여섯",
    "일곱",
    # Vocabulary a profile matches on
    "목사",
    "협동목사",
    "선교사",
    "장로",
    "교수",
    "총장",
    "담임",
    "강해",
    "제",
    "장",
    "편",
    "부",
    "예배",
    "부예배",
    "부설교",
    "영상",
    "주일",
    "주일예배",
    "감사",
    "연합",
    # Generic names for the services a Korean church holds. These describe the
    # kind of meeting, not who holds it, so they identify no one.
    "새벽예배",
    "수요기도회",
    "금요성령집회",
    "특별집회",
    "워십",
    "봉헌송",
    "성가대",
    # CMS labels and strings a fixture or scraper has to reproduce
    "본문",
    "설교자",
    "날짜",
    "영상보기",
    "삭제된",
    "데이터입니다",
    "맨끝",
    "바로",
    "보기",
    # Software named in prose
    "교회사랑넷",
    # The books of the bible, which a verse rule checks against as a closed set.
    # They name scripture, not anybody on the site, so they identify no one.
    "창세기", "출애굽기", "레위기", "민수기", "신명기", "여호수아", "사사기", "룻기",
    "사무엘상", "사무엘하", "열왕기상", "열왕기하", "역대상", "역대하", "에스라",
    "느헤미야", "에스더", "욥기", "시편", "잠언", "전도서", "아가", "이사야",
    "예레미야", "예레미야애가", "에스겔", "다니엘", "호세아", "요엘", "아모스",
    "오바댜", "요나", "미가", "나훔", "하박국", "스바냐", "학개", "스가랴", "말라기",
    "마태복음", "마가복음", "누가복음", "요한복음", "사도행전", "로마서",
    "고린도전서", "고린도후서", "갈라디아서", "에베소서", "빌립보서", "골로새서",
    "데살로니가전서", "데살로니가후서", "디모데전서", "디모데후서", "디도서",
    "빌레몬서", "히브리서", "야고보서", "베드로전서", "베드로후서", "요한일서",
    "요한이서", "요한삼서", "유다서", "요한계시록",
    # Malformations of one of them, invented to exercise the near-miss rule:
    # a substitution, an insertion and a deletion.
    "요한복은",
    "요한복음서",
    "한복음",
    # The halves a book name splits into when a space is typed inside it, which
    # is the case the rule closes back up.
    "요한",
    "복음",
    "사무엘",
    "상",
    # The words a chapter and verse are spelled out in — "2장 1절에서 10절" — as
    # the runs they leave once the digits between them are taken out.
    "절",
    "절에서",
    "절부터",
    "절로",
    "장에서",
    "장편",
    "에서",
    "부터",
    "로",
    "까지",
    # A book named by its abbreviation, which is still scripture and still nobody.
    "요",
    # A name equidistant from two real books, which the rule must refuse to guess.
    "사무엘장",
    # Two edits from a real book, which is beyond what the rule will correct.
    "예배소서",
    # Hangul syllable range bounds in models.py
    "가",
    "힣",
}

#: Where the site-specific terms live. Ignored by git, so this file never names
#: the real site; absent on a fresh clone, where the check simply does not run.
FORBIDDEN_TERMS_FILES = sorted(pathlib.Path("sites").glob("*/forbidden-terms.txt"))

#: Where the terms used to live, before a site kept everything about itself in
#: one directory. Still read, so an older checkout keeps working.
FORBIDDEN_TERMS_FILE = pathlib.Path("config/forbidden-terms.txt")

#: A Vimeo id below this is inside the range Vimeo has handed out, so it may well
#: resolve to a real video belonging to somebody. Placeholders must sit above it.
MIN_UNASSIGNED_VIMEO_ID = 10**11

#: YouTube ids carry no orderable range, so placeholders come from a fixed set.
ALLOWED_YOUTUBE_IDS = {"aBcDeFgHiJk", "dQw4w9WgXcQ"}

HANGUL = re.compile(r"[가-힣]+")
VIMEO_ID = re.compile(r"vimeo\.com/(?:video/)?(\d+)")
YOUTUBE_ID = re.compile(r"(?:youtube\.com/(?:embed/|watch\?v=)|youtu\.be/)([A-Za-z0-9_-]{8,})")


def tracked_files() -> list[pathlib.Path]:
    """
    List the files a commit could actually carry.

    :return: Every tracked or untracked-but-not-ignored file, minus the
        directories holding the real site's data
    """
    listed = subprocess.run(
        ["git", "ls-files", "-co", "--exclude-standard"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\n")
    return [
        path
        for name in listed
        if name and not name.startswith(EXCLUDED_PREFIXES) and (path := pathlib.Path(name)).is_file()
    ]


def load_forbidden_terms() -> list[str]:
    """
    Read the site-specific terms, from every site this checkout has.

    Each site names its own, since only it knows what identifies it. A checkout
    with no sites cloned into place contributes none, which is why this check
    never blocks a fresh clone.

    :return: Terms to search for, lowercased; empty when no site supplies any
    """
    terms = []
    for path in [*FORBIDDEN_TERMS_FILES, FORBIDDEN_TERMS_FILE]:
        if not path.is_file():
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        terms += [line.strip().lower() for line in lines if line.strip() and not line.startswith("#")]
    return terms


def check(path: pathlib.Path, forbidden: list[str]) -> list[str]:
    """
    Report everything in one file that should not be committed.

    :param path: File to inspect
    :param forbidden: Site-specific terms to search for
    :return: One message per finding, empty when the file is clean
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []  # binary or unreadable: nothing to read out of it

    findings = []
    for number, line in enumerate(text.splitlines(), 1):
        where = f"{path}:{number}"

        for word in HANGUL.findall(line):
            if word not in ALLOWED_HANGUL:
                findings.append(f"{where}: unrecognized Korean {word!r} — a real title or name?")

        lowered = line.lower()
        for term in forbidden:
            if term in lowered:
                findings.append(f"{where}: names the real site ({term!r})")

        for video_id in VIMEO_ID.findall(line):
            if int(video_id) < MIN_UNASSIGNED_VIMEO_ID:
                findings.append(f"{where}: Vimeo id {video_id} may resolve to a real video")

        for video_id in YOUTUBE_ID.findall(line):
            if video_id not in ALLOWED_YOUTUBE_IDS:
                findings.append(f"{where}: YouTube id {video_id!r} is not a known placeholder")

    return findings


def main() -> int:
    """
    Check every committable file and report what it finds.

    :return: 1 if anything was found, 0 otherwise
    """
    forbidden = load_forbidden_terms()
    findings = [message for path in tracked_files() for message in check(path, forbidden)]

    if not findings:
        note = "" if forbidden else f" ({FORBIDDEN_TERMS_FILE} absent, site-name check skipped)"
        print(f"No real-site data found in committable files{note}.")
        return 0

    print(f"Found {len(findings)} thing(s) that must not be committed — see CLAUDE.md:\n")
    for message in findings:
        print(f"  {message}")
    print("\nUse a placeholder, or add the word to ALLOWED_HANGUL if it is generic vocabulary.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
