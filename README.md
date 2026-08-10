# Video Migrator

Move videos and their metadata from one hosting platform to another.

The pipeline has four stages, and each one is pluggable:

1. **Scrape** a website to discover which videos exist and what their metadata is
2. **Download** each video from its source platform (Vimeo, Aninamu, ...)
3. **Normalize and write** metadata into the media file with ffmpeg
4. **Upload** to the destination platform (YouTube, ...)

The shipped profiles are generic examples; point one at a real site by adding a
profile under `sites/`. Sources, sinks and scrapers are pluggable — new
sources, sinks and scrapers are added by dropping a module into the matching
package and registering it — see [Extending](#extending).

## Table of Contents

- [Features](#features)
- [Development Environment Setup](#development-environment-setup)
- [Pipeline Stages](#pipeline-stages)
- [Correcting the source site](#correcting-the-source-site)
- [Usage](#usage)
- [Extending](#extending)
- [Testing](#testing)
- [Project Structure](#project-structure)
- [Configuration](#configuration)

## Features

- Scrape video metadata from GnuBoard gallery boards (any site running it)
- Scrape the VOD module of the 교회사랑넷 (church-love.net) church CMS
- Support for multiple video platforms (Vimeo, YouTube, SoundCloud)
- HTTP response caching for faster subsequent runs
- Video metadata management using ffmpeg
- Automatic language detection (Korean/English)
- Metadata normalization and validation
- Multiple output formats (JSON, CSV, text)
- Resumable YouTube uploads with exponential backoff

## Development Environment Setup

### Prerequisites

- [uv](https://docs.astral.sh/uv/) (manages the Python toolchain and dependencies)
- ffmpeg (for video metadata manipulation)
- Git

The interpreter is pinned to **Python 3.13** in `.python-version`, matching the
`requires-python = ">=3.13,<3.14"` floor in `pyproject.toml` and ruff's
`target-version`. Change all three together. uv installs that interpreter itself
if the system does not provide one, so a separate Python install is not
required. On Ubuntu 24.04 this also avoids the `externally-managed-environment`
error that `pip install` raises against the system interpreter.

### Installation

1. **Install uv** (if not already installed):
   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh   # macOS / Linux
   powershell -c "irm https://astral.sh/uv/install.ps1 | iex"   # Windows
   ```

2. **Clone the repository** and change into it.

3. **Install dependencies**:

   For regular usage — creates `.venv/` and installs from `uv.lock`:
   ```bash
   uv sync
   ```

   `uv sync` includes the `dev` dependency group (pytest, ruff) by default.
   To install only the runtime dependencies:
   ```bash
   uv sync --no-default-groups
   ```

   To add the browser-driven download strategy (Playwright):
   ```bash
   uv sync --extra browser
   uv run playwright install chromium
   ```
   Or `make install-browser`, which runs both. Note that a plain `uv sync`
   removes the extra again — see [Doctests](#doctests) for how the test suite
   copes with `playwright` being absent.

   There is no virtual environment to activate — prefix commands with
   `uv run` and uv resolves the project environment automatically. If you
   prefer an activated shell, `source .venv/bin/activate` still works.

4. **Install ffmpeg** (if not already installed):
   - **macOS**: `brew install ffmpeg`
   - **Ubuntu/Debian**: `sudo apt-get install ffmpeg`
   - **Windows**: Download from [ffmpeg.org](https://ffmpeg.org/download.html)

### Managing dependencies

```bash
uv add <package>                 # add a runtime dependency
uv add --group dev <package>     # add a dev-only dependency
uv remove <package>              # drop a dependency
uv lock --upgrade                # refresh uv.lock to newer versions
```

`uv.lock` is committed and pins the exact resolution — do not edit it by hand.

### Common tasks

A `Makefile` wraps every routine command; `make` on its own prints the list.
Each recipe goes through `uv run`, so no target needs an activated virtualenv.

| Target | Does |
|--------|------|
| `make install` | `uv sync` — venv with the dev group |
| `make install-browser` | Sync with the `browser` extra, then fetch Chromium |
| `make install-prod` | Runtime dependencies only |
| `make lock` | Refresh `uv.lock` |
| `make test` | Unit tests |
| `make doctest` | Doctests in `src/` |
| `make test-all` | Both (the default `pytest` invocation) |
| `make coverage` | Tests plus an HTML + terminal coverage report |
| `make lint` / `make lint-fix` | ruff check, optionally autofixing |
| `make format` / `make format-check` | ruff format, optionally read-only |
| `make check` | `lint` + `format-check` + `test-all` — the CI gate |
| `make build` | Build sdist and wheel into `dist/` |
| `make clean` | Drop caches, coverage output, build artifacts (never `.venv/`) |

Override the tool with e.g. `make test UV=uvx`.

## Pipeline Stages

| Stage | Package | What it does |
|-------|---------|--------------|
| Scrape | `video_migrator.scrapers` | Walk a website and produce `Video` records |
| Download | `video_migrator.sources` | Pull the media file off the source platform |
| Metadata | `video_migrator.metadata` | Normalize fields, then write them with ffmpeg |
| Upload | `video_migrator.sinks` | Push the file to the destination platform |

All four exchange a single record type, `video_migrator.models.Video`.

## Correcting the source site

A migration is only as good as what it reads. Where the source site's own
records are wrong — a mis-typed date, a preacher never entered, a video whose
link is gone — normalization repairs what it can on the way past, and the rest
has to be fixed at the source.

That is a second loop alongside the pipeline, and it turns on one artifact:

```
scrape ──► .cache/       ──► build ledger ──► ledger ◄── review, research, judgement
                                                │
                                                ▼
                                      apply ──► the site
                                                │
                                                └── stamp, then scrape again
```

### The ledger

One row per field per record. A row says what the field holds, what it should
hold, why, and where the new value came from:

```
num  date  field  old  new  reason  source  updated_at
```

`updated_at` does the work of two files. Blank means outstanding; a timestamp
means applied — so the same file is the queue and the history of what was done.
Nothing has to be reconciled by hand, and a change is never recorded twice.

Two kinds of writer share it. A **scan** fills what rules can see: a book name
that is not a book, a title off the house style, a field left empty. A **person**
fills what rules cannot: a name read out of a printed bulletin, a wording
decision, a verdict of "checked, correct as it stands". Hence the one invariant
that matters — **a scan may revise its own rows and never anyone else's.** A
rule that can only say "missing" must not overwrite an answer someone spent an
afternoon establishing.

A value in parentheses — `(look it up in last Sunday's bulletin)` — is a note to
a person, and the applier refuses it for want of anything to write.

### Two forms of crawled data

The scrape leaves two artifacts, and they are not interchangeable:

| | holds | read by |
|---|---|---|
| `.cache/` | records exactly as the site stores them | the ledger |
| `sites/<site>/data/<site>-<board>.csv` | records after normalization | the migration |

The ledger **must** read the cache. The CSV holds what normalization *made* of a
record — dates corrected, fields moved, titles rewritten — so a row built from
it would describe a value the site has never held, and the applier would refuse
it as changed-since-the-scan.

### Applying

Writes are deliberate by construction:

- nothing is sent without `--apply`, and the batch size defaults to one;
- a record whose field already holds the target is skipped, so a stale ledger
  costs nothing and an interrupted run is safe to repeat;
- a record holding *neither* the scanned value nor the proposed one has been
  edited by hand since — that edit wins, and the row is left alone;
- a row is stamped only after the new value has been **read back from the public
  side of the site**, so a timestamp means the change was seen from outside, not
  merely submitted.

Fields are grouped by record: one page load and one save per record, however
many of its fields are being corrected.

### What is generic, and what is not

| | where |
|---|---|
| Ledger schema, merge rules, apply/skip/diverge decision, verification | `video_migrator` |
| The admin driver for one CMS | `video_migrator.admin`, named after the software |
| Field vocabulary — valid titles, house style, name lists | the site profile |
| Credentials, board codes, admin URLs, the ledger itself | `sites/<site>/` — a private repo |

Scrapers are named after site *software* rather than a site, and the admin
drivers follow the same rule: one driver serves every site running that CMS,
and a second site needs a profile rather than code.

## Usage

### 1. List videos on the source site

Installed as `video-migrator-list`, or run as `uv run python -m video_migrator.cli`.

```bash
# Scrape early morning prayer videos (default board of the default profile)
video-migrator-list

# Scrape a board on your own site profile under sites/
video-migrator-list -P mysite -b sunday_sermon

# Scrape specific board with custom options
video-migrator-list -b sunday_sermon -p 5 -o output.json -f json

# Disable caching for fresh data
video-migrator-list -b friday_prayer --no-cache

# Scrape with specific language code
video-migrator-list -b charisma_praise -l kor
```

**Command-line Options**:

| Option | Description | Default |
|--------|-------------|---------|
| `-P, --profile` | Site profile to scrape (see [Configuration](#configuration)) | `example` |
| `-b, --board` | Board name (see choices below) | `early_morning_prayer` |
| `-l, --language` | ISO 639-2/B language code (e.g., 'kor', 'eng') | Auto-detect |
| `-p, --pages` | Number of pages to scrape | Auto-detect |
| `-o, --output` | Output file path | stdout |
| `-f, --format` | Output format (json, csv, text) | `text` |
| `-v, --verbose` | Show detailed information | False |
| `--cache-dir` | Cache directory | `.cache` |
| `--cache-duration` | Cache duration in seconds | `3600` |
| `--no-cache` | Disable caching | False |

**Board Choices** (whatever the chosen profile declares; the shipped examples
declare these):
- `early_morning_prayer` - Early morning prayer sermons
- `sunday_sermon` - Sunday worship sermons
- `wednesday_prayer` - Wednesday prayer meetings
- `friday_prayer` - Friday prayer meetings
- `special_sermon` - Special sermon events
- `charisma_praise` - Charisma praise songs
- `votive_song` - Votive songs
- `choir_praise` - Choir praise performances

**Output Formats**:

- **Text**: Human-readable format with video details
- **JSON**: Machine-readable JSON format
- **CSV**: Spreadsheet-compatible format with headers

### 2. Download from the source platform

Installed as `video-migrator-download` (Vimeo), or run a source module directly.

```bash
# Vimeo
video-migrator-download <vimeo_url>
uv run python -m video_migrator.sources.vimeo <vimeo_url> -o output.mp4 -q 1080

# Aninamu (direct download)
uv run python -m video_migrator.sources.aninamu <aninamu_url> video.mp4

# Aninamu fallbacks when the media is streamed
uv run python -m video_migrator.sources.aninamu_ytdlp <aninamu_url> video.mp4
uv run python -m video_migrator.sources.aninamu_playwright <aninamu_url>   # needs [browser] extra
```

### 3. Write metadata

`update_video_metadata()` updates a video file's metadata using ffmpeg.

**Features**:
- Updates standard metadata fields (title, artist, genre, date, year)
- Sets creation_time based on board type
- Adds bible verse as comment for sermon videos
- Sets language for all streams

**Board-Specific Creation Times** (`video_migrator.metadata.ffmpeg.BOARD_CREATION_TIMES`):
- `early_morning_prayer`: 05:30:00
- `sunday_sermon`: 10:30:00
- `friday_prayer`: 19:30:00
- Other boards: 00:00:00

**Usage**:

```python
from pathlib import Path

from video_migrator import Video, update_video_metadata
from video_migrator.config import load_profile

video = Video(
    type="vimeo",
    id="900000000001",
    url="https://vimeo.com/900000000001",
    embed_url="https://player.vimeo.com/video/900000000001",
    title="[설교 시리즈 강해] 설교 제목",
    bible_verse="요한복음 21:15-23",
    publish_date="2012-06-25",
    artist="홍길동 목사",
    genre="Sermon",
    language="kor",
)

success = update_video_metadata(
    video_file=Path("input.mp4"),
    video=video,
    output_file=Path("output.mp4"),
    creation_time=load_profile().board("early_morning_prayer").creation_time,
)
```

`update_video_metadata()` takes the resolved time of day rather than a board
name, so `video_migrator.metadata` stays independent of any particular source
site. Look the time up from the site profile — see [Configuration](#configuration).

### 4. Upload to the destination platform

Installed as `video-migrator-upload`, or run as `uv run python -m video_migrator.sinks.youtube`.
Uploads to YouTube using the YouTube Data API v3.

**Client Secrets Configuration**:

The script looks for `client_secrets.json` in the following locations (in order):
1. Environment variable: `VIDEO_MIGRATOR_CLIENT_SECRETS`
   (`VIDEO_SCRAPER_CLIENT_SECRETS` still works, for backwards compatibility)
2. Current directory: `./client_secrets.json`
3. Config subdirectory: `./config/client_secrets.json`
4. User config directory: `~/.config/video-migrator/client_secrets.json`

To set up OAuth credentials:
1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a project and enable the YouTube Data API v3
3. Create OAuth 2.0 credentials and download the `client_secrets.json` file
4. Place it in one of the locations above or set the environment variable

**Usage**:

```bash
# Basic upload
video-migrator-upload --file video.mp4 --title "My Video"

# With full metadata
video-migrator-upload \
  --file video.mp4 \
  --title "Sermon Title" \
  --description "Sermon description" \
  --category 29 \
  --keywords "sermon,church,faith" \
  --privacyStatus private \
  --publishAt 2024-12-31T10:00:00Z
```

## Extending

Each stage keeps a registry so new platforms plug in without touching the rest
of the pipeline.

**A new source platform**: add `src/video_migrator/sources/<platform>.py` with a
downloader class, then register it:

```python
# src/video_migrator/sources/__init__.py
SOURCES = {
    "vimeo": "video_migrator.sources.vimeo:VimeoDownloader",
    "myplatform": "video_migrator.sources.myplatform:MyPlatformDownloader",
}
```

Entries are `"module:attribute"` strings resolved lazily, so a source with heavy
or optional dependencies costs nothing until it is actually used.

**A new destination platform**: same pattern in `video_migrator/sinks/__init__.py`
via `SINKS`, pointing at an upload callable.

**Another site running software you already parse**: no code at all. Scraper
modules are named after the *site software*, not the site — `gnuboard.py` parses
any [GnuBoard](https://github.com/gnuboard/gnuboard5) gallery board, and
`churchlove.py` parses any site on the 교회사랑넷 CMS. A real profile lives under
`sites/` and so cannot be registered in the package; it names its software
itself instead:

```yaml
# sites/another_church/another_church.yaml
site:
  board_url: https://www.another-church.org/main/sub.html
  scraper: churchlove        # the profile whose scraper parses this site
```

A profile shipped *inside* the package registers against the existing class
instead:

```python
# src/video_migrator/scrapers/__init__.py
SCRAPERS = {
    "example": GnuBoardScraper,
    "churchlove": ChurchLoveScraper,
    "another_church": GnuBoardScraper,  # + profiles/another_church.yaml
}
```

**A site running different software**: add
`src/video_migrator/scrapers/<software>.py` with a scraper class that returns
`Video` objects and exposes a `board_url(board, profile_name)` static method,
then register it in `SCRAPERS` keyed by profile name.

Look them up with `get_downloader()`, `get_uploader()` and `get_scraper()` —
or, given a loaded profile, `scraper_for(profile)`, which honours `site.scraper`.

## Testing

### Running Tests

Settings live in `[tool.pytest.ini_options]` in `pyproject.toml`, so bare
`pytest` already does the right thing — `testpaths` covers both trees and
`addopts` supplies `--doctest-modules`. No flags to remember:

```bash
make test-all          # everything: unit tests + doctests
uv run pytest          # the same thing
```

Narrow it by path:
```bash
make test              # unit tests only
make doctest           # doctests only
uv run pytest tests/test_board_creation_time.py        # one file
uv run pytest tests/test_board_creation_time.py -vv    # verbose
uv run pytest tests/test_board_creation_time.py -vv -s # show print output
```

Coverage — writes `htmlcov/index.html`:
```bash
make coverage
```

### Doctests

Docstring examples in `src/video_migrator/` are collected as real tests, which
is why `testpaths` lists the package alongside `tests/`. One module currently
carries them (`utils/multi_regex_replace.py`); run just that file with:

```bash
uv run pytest src/video_migrator/utils/multi_regex_replace.py -v
```

Because the package itself is collected, `conftest.py` sits in the **project
root** rather than in `tests/` — a conftest only governs its own directory
subtree, and `tests/` is a sibling of `src/`, not a parent. The root is the
nearest shared ancestor of both. It skips
`sources/aninamu_playwright.py` when the optional `playwright` dependency is
absent, so doctests still run on a plain `uv sync`:

```python
if find_spec("playwright") is None:
    collect_ignore.append("src/video_migrator/sources/aninamu_playwright.py")
```

Install the extra (`make install-browser`) and the module is collected normally.

### Test Files

Located in the `tests/` directory:

- `tests/test_board_creation_time.py` - Tests for board-specific creation time metadata
- `tests/test_multi_regex_replace.py` - Tests for regex replacement utility
- `tests/test_registries.py` - Tests for the source/sink/scraper registries

All test files follow the `test_*.py` naming convention and use pytest fixtures where appropriate.

## Project Structure

```
.
├── README.md                      # Project documentation
├── Makefile                       # Developer task runner (make = help)
├── pyproject.toml                 # Dependencies, pytest and ruff configuration
├── uv.lock                        # Pinned resolution (committed, never hand-edited)
├── conftest.py                    # Collection config; must be above src/ (see Doctests)
├── .python-version                # Interpreter pin (3.13)
├── .gitignore                     # Git ignore rules
├── CLAUDE.md                      # Coding standards, read by Claude Code and Copilot
├── LICENSE                        # Project license
│
├── .github/                       # GitHub specific files
│   └── instructions/
│       └── mermaid.instructions.md  # Diagram conventions (Copilot path-scoped)
│
├── sites/                         # One directory per source site (see sites/README.md)
│   └── <yoursite>/                # A private repo cloned into place — gitignored
│
├── config/                        # Credentials not tied to any one site
│   └── client_secrets.json        # OAuth credentials (gitignored)
│
├── src/                           # Source code directory
│   └── video_migrator/            # Main package
│       ├── __init__.py
│       ├── models.py              # Video record shared by every stage
│       ├── config.py              # Site profile loading (see Configuration)
│       ├── cli.py                 # Listing CLI (video-migrator-list)
│       ├── profiles/              # Shipped site profiles
│       │   ├── example.yaml       # Shipped default; real ones live in sites/
│       │   └── churchlove.yaml    # Example profile for the 교회사랑넷 CMS
│       ├── scrapers/              # Stage 1: discover videos on a website
│       │   ├── __init__.py        # SCRAPERS registry
│       │   ├── platforms.py       # Which platform a scraped video URL points at
│       │   ├── gnuboard.py        # GnuBoard gallery scraper (any GnuBoard site)
│       │   └── churchlove.py      # 교회사랑넷 (church-love.net) VOD scraper
│       ├── sources/               # Stage 2: download from a source platform
│       │   ├── __init__.py        # SOURCES registry
│       │   ├── vimeo.py           # Vimeo downloader (yt-dlp)
│       │   ├── aninamu.py         # Aninamu direct downloader
│       │   ├── aninamu_ytdlp.py   # Aninamu fallback via yt-dlp
│       │   └── aninamu_playwright.py  # Aninamu browser-driven strategy
│       ├── metadata/              # Stage 3: normalize and write metadata
│       │   ├── __init__.py
│       │   ├── ffmpeg.py          # update_video_metadata()
│       │   └── normalize.py       # Title/preacher normalization, validation
│       ├── sinks/                 # Stage 4: upload to a destination platform
│       │   ├── __init__.py        # SINKS registry
│       │   └── youtube.py         # YouTube Data API v3 uploader
│       └── utils/                 # Shared utilities
│           ├── __init__.py
│           ├── http_cache.py      # HTTP caching utility
│           └── multi_regex_replace.py # Regex replacement utility
│
└── tests/                         # Test directory
    ├── __init__.py
    ├── test_board_creation_time.py    # Creation time tests
    ├── test_churchlove.py             # 교회사랑넷 scraper and platform recognizer
    ├── test_http_cache.py             # Cache decoding and POST support
    ├── test_multi_regex_replace.py    # Regex replacement tests
    ├── test_normalize.py              # Title rewriting and preacher validation
    └── test_registries.py             # Registry lookup tests
```

## Configuration

Everything site-specific lives in a **site profile** — a YAML file, not Python.
Adding a board or a preacher alias is a config edit.

### Site profiles

`load_profile("example")` takes the first of these that exists:

| Precedence | Location | Use for |
|---|---|---|
| 1 | `$VIDEO_MIGRATOR_CONFIG` | Point at any file; overrides everything |
| 2 | `./sites/<name>/<name>.yaml` | **Your real site profile — a private repo** |
| 3 | `src/video_migrator/profiles/<name>.yaml` | The shipped, publishable examples |

The examples live inside the package so an installed CLI works without a
checkout; they are included in the wheel via `[tool.setuptools.package-data]`.
Two ship today: `example.yaml` for a GnuBoard site and `churchlove.yaml` for one
on the 교회사랑넷 CMS.

**A real profile names real people** — preacher aliases are personal data — so
everything about an actual site lives under **`sites/<name>/`, ignored
wholesale** rather than by extension: its profile, its correction ledger, its
scrape output and any working notes taken from it. Each such directory is meant
to be a separate private repository cloned into place, which keeps the boundary
where the sensitivity is; see `sites/README.md`. Only the generic examples under
`src/video_migrator/profiles/` are committed. To describe an actual site, copy
the example that matches its software:

```bash
mkdir -p sites/mysite
cp src/video_migrator/profiles/churchlove.yaml sites/mysite/mysite.yaml
$EDITOR sites/mysite/mysite.yaml
video-migrator-list --profile mysite --board sunday_sermon
```

`--profile` selects a profile by name, resolved through the table above;
`VIDEO_MIGRATOR_CONFIG=<path> video-migrator-list` still works for pointing at a
file directly.

### Boards

A board is described **once**. Order sets the CLI `--board` choices and the
first entry is the default. `creation_time` is the time of day stamped into the
media file, defaulting to midnight when omitted.

```yaml
boards:
  - name: early_morning_prayer
    genre: Sermon
    creation_time: "05:30:00"
  - name: charisma_praise
    genre: Praise
```

Keep `creation_time` quoted — bare `10:30:00` is an integer in YAML.

Some CMSes address a board by an opaque code rather than by a name you choose.
Give those boards a `page_code` alongside the name you want the CLI to use —
find it in the URL the site's own menu links to. `weekday` is the day the
service is held:

```yaml
boards:
  - name: sunday_sermon    # 주일예배
    page_code: 9
    weekday: sunday
    genre: Sermon
    creation_time: "10:30:00"
```

### Publish dates that run late

A site often stamps a recording when it was *posted*, not when it was recorded —
a Sunday sermon uploaded that evening can land on the Monday. Declaring a board's
`weekday` lets normalization pull such a date back onto the day of the service,
and `year` moves with it (a Monday 1 January belongs to the year before).

Corrections run **backwards only** — a recording is filed when it is posted, so
its date lands on or after the service, never before. How far back to reach is a
policy, since it trades a stricter invariant against the risk of inventing a
date:

```yaml
normalize:
  max_publish_date_drift: 6   # default 1
```

`1` corrects only the routine posted-the-next-day case and leaves anything
further for `validate_videos` to report by name. `6` treats the weekday as an
invariant and always corrects to the preceding occurrence. Either way, a move of
more than a day is printed, so the unusual corrections stay visible rather than
silently rewriting history.

A correction of more than a day is a guess, and a guess is **refused where
something contradicts it**: another row already holding that date *for the same
service*, or a second guess competing for it. Those rows keep their scraped date
and are reported. Without this a mis-typed year quietly becomes a
plausible-looking duplicate, which is far harder to notice later than a date that
still looks wrong.

A single day yields several recordings, so a date alone does not identify a
service — `service_parts` names the markers that tell them apart:

```yaml
normalize:
  service_parts: ["1부", "2부", "3부", "영상"]
```

A title's markers are collected as a set, not matched in order, because a title
can carry two: `1부 영상` is the video of the first service and is neither the
first service nor another day's video. A second service therefore lands beside
its first rather than being refused, while a row naming no service still
conflicts with another such row. One-day corrections are never refused at all —
a day's services drift together and are meant to land on the same date.

Omit `weekday` for a board with no fixed day — a daily prayer meeting, or an
occasional series — and its dates are never touched, whatever the drift setting.

### Normalization rules

```yaml
normalize:
  title_replacements:      # regex -> replacement, applied in order
    '^\]\s+': ''           # single-quote both sides, see below
    '\]제': '] 제'
  valid_preacher_titles: [" 목사", " 선교사", " 장로"]   # titles that follow a name
  valid_preacher_prefixes: ["Rev.", "Pastor", "Dr."]     # titles that precede one
  title_spacing_words: ["강해"]
  preacher_names:          # scraped name -> canonical name
    홍길동목사: 홍길동 목사
    김영희: 김영희 목사
    ".": ""                # the site's placeholder for "nobody recorded"
```

A service part recorded against the *preacher* describes the service, not the
person. `preacher_service_pattern` moves it into the title, where the rest of the
board keeps it:

```yaml
normalize:
  preacher_service_pattern: '[_\s]*([123]부|영상)(?:설교|예배)?\s*$'
```

Its one capture group is the part. `홍길동 선교사_2부설교` on *설교 제목 하나*
becomes preacher `홍길동 선교사` and title `설교 제목 하나 (2부)` — the topic leads
and the part qualifies it, so search terms are not buried behind a number. A title already
naming the part keeps the one it has. This runs before `preacher_names`, so
aliases only ever see a cleaned name — and it is what lets the date logic tell a
1부 from a 2부 (see [Publish dates that run late](#publish-dates-that-run-late)).

A preacher name passes validation if it carries a title from **either** list —
Korean titles follow the name, English ones precede it. A trailing
`(home church)` is ignored when looking, so `홍길동 목사 (예시교회 담임)` passes on
its ` 목사`. Names the site records with the service part attached
(`홍길동 목사 2부`) or with no space (`김영희목사`) are best fixed as
`preacher_names` aliases.

Every rewrite is reported. `fix_video_metadata()` **returns** its changes rather
than printing them, so the caller chooses how loudly to say so; the CLI prints a
count by default and each rewrite under `-v`:

```
Normalized 351 fields: 322 date, 18 preacher, 11 title
```

The counts are the point: a rule that quietly rewrote 800 titles is worth
noticing even when every rewrite was correct, and a misfiring rule rewrites just
as silently as a working one. Change indexes match the numbering
`validate_videos` uses, so a warning and a rewrite can be lined up against the
same row.

`title_replacements` runs first, in declaration order, using Python `re.sub`
syntax (`\1` for a capture group); `title_spacing_words` runs afterwards.
**Single-quote both the pattern and the replacement** — YAML processes
backslash escapes inside double quotes, so `"\]"` would not survive.

### Duplicate records

`validate_videos` also flags a video filed under more than one record:

```
Warning: Found 2 videos filed under more than one record:
  https://vimeo.com/999999999999
    456. (1부예배) 설교 제목
    457. (1부예배) 설교 제목
```

It keys on the **video URL**, not on title and date. A two-service Sunday
preaches one sermon twice and files two recordings under one title, so matching
on title and date would flag those as duplicates when they are nothing of the
kind. Two records pointing at the same recording is the only unambiguous signal.

Profiles are loaded with a strict YAML loader that **rejects duplicate keys**.
Plain YAML keeps the last one, so forgetting the `- ` on a new board would
silently merge it into its predecessor; you get an error pointing at the line
instead.

### What stays in code

Not everything site-shaped belongs in config. `PLATFORM_LABELS` in
`scrapers/platforms.py` maps to platforms the package can actually resolve —
adding an entry would produce a label, not the ability to scrape that platform.
Same for the retry and category tables in `sinks/youtube.py`.

## Coding Standards

This project follows the Python coding standards in [CLAUDE.md](CLAUDE.md), which
both Claude Code and GitHub Copilot code review read:

- **Import Organization**: Alphabetically sorted, grouped by standard/third-party/local
- **Documentation**: Sphinx-style docstrings for all classes and functions
- **Formatting**: Use `ruff` for formatting and linting
- **Line Length**: Maximum 120 characters
- **Naming**: `snake_case` for variables/functions, `PascalCase` for classes, `ALL_CAPS` for constants

### Running Code Formatters

```bash
make format        # rewrite files with ruff format
make format-check  # verify formatting, write nothing
make lint          # ruff check
make lint-fix      # ruff check with safe autofixes
make check         # lint + format-check + test-all, the CI gate
```

`make check` is what a change should pass before it lands.

## License

MIT — see [LICENSE](LICENSE).

## Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

## Support

For issues and questions, please open an issue on the project repository.
