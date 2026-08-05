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
    "부",
    "예배",
    "부예배",
    "부설교",
    "영상",
    "주일예배",
    "감사",
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
    # Software and scripture named in prose
    "교회사랑넷",
    "요한복음",
    # Hangul syllable range bounds in models.py
    "가",
    "힣",
}

#: Where the site-specific terms live. Ignored by git, so this file never names
#: the real site; absent on a fresh clone, where the check simply does not run.
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
    Read the site-specific terms, if this checkout has them.

    :return: Terms to search for, lowercased; empty when the file is absent
    """
    if not FORBIDDEN_TERMS_FILE.is_file():
        return []
    lines = FORBIDDEN_TERMS_FILE.read_text(encoding="utf-8").splitlines()
    return [line.strip().lower() for line in lines if line.strip() and not line.startswith("#")]


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
