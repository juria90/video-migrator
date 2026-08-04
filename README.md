# Video Migrator

Move videos and their metadata from one hosting platform to another.

The pipeline has four stages, and each one is pluggable:

1. **Scrape** a website to discover which videos exist and what their metadata is
2. **Download** each video from its source platform (Vimeo, Aninamu, ...)
3. **Normalize and write** metadata into the media file with ffmpeg
4. **Upload** to the destination platform (YouTube, ...)

The shipped profile is a generic example; point it at a real site by adding a
profile under `config/`. Sources, sinks and scrapers are pluggable — new
sources, sinks and scrapers are added by dropping a module into the matching
package and registering it — see [Extending](#extending).

## Table of Contents

- [Features](#features)
- [Development Environment Setup](#development-environment-setup)
- [Pipeline Stages](#pipeline-stages)
- [Usage](#usage)
- [Extending](#extending)
- [Testing](#testing)
- [Project Structure](#project-structure)
- [Configuration](#configuration)

## Features

- Scrape video metadata from GnuBoard gallery boards (any site running it)
- Support for multiple video platforms (Vimeo, YouTube, SoundCloud)
- HTML page caching for faster subsequent runs
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

## Usage

### 1. List videos on the source site

Installed as `video-migrator-list`, or run as `uv run python -m video_migrator.cli`.

```bash
# Scrape early morning prayer videos (default board)
video-migrator-list

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
| `-b, --board` | Board name (see choices below) | `early_morning_prayer` |
| `-l, --language` | ISO 639-2/B language code (e.g., 'kor', 'eng') | Auto-detect |
| `-p, --pages` | Number of pages to scrape | Auto-detect |
| `-o, --output` | Output file path | stdout |
| `-f, --format` | Output format (json, csv, text) | `text` |
| `-v, --verbose` | Show detailed information | False |
| `--cache-dir` | Cache directory | `.cache` |
| `--cache-duration` | Cache duration in seconds | `3600` |
| `--no-cache` | Disable caching | False |

**Board Choices**:
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
    id="123456789",
    url="https://vimeo.com/123456789",
    embed_url="https://player.vimeo.com/video/123456789",
    title="[사도행전 28장 강해] 바울의 로마 여정",
    bible_verse="사도행전 28:11-31",
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
any [GnuBoard](https://github.com/gnuboard/gnuboard5) gallery board. Add a
profile and register it against the existing class:

```python
# src/video_migrator/scrapers/__init__.py
SCRAPERS = {
    "example": GnuBoardScraper,
    "another_church": GnuBoardScraper,  # + profiles/another_church.yaml
}
```

**A site running different software**: add
`src/video_migrator/scrapers/<software>.py` with a scraper class that returns
`Video` objects, then register it in `SCRAPERS` keyed by profile name.

Look them up with `get_downloader()`, `get_uploader()` and `get_scraper()`.

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
├── config/                        # Configuration files
│   └── client_secrets.json        # OAuth credentials (gitignored)
│
├── src/                           # Source code directory
│   └── video_migrator/            # Main package
│       ├── __init__.py
│       ├── models.py              # Video record shared by every stage
│       ├── config.py              # Site profile loading (see Configuration)
│       ├── cli.py                 # Listing CLI (video-migrator-list)
│       ├── profiles/              # Shipped site profiles
│       │   └── example.yaml       # Shipped default; real ones live in config/
│       ├── scrapers/              # Stage 1: discover videos on a website
│       │   ├── __init__.py        # SCRAPERS registry
│       │   └── gnuboard.py        # GnuBoard gallery scraper (any GnuBoard site)
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
    ├── test_multi_regex_replace.py    # Regex replacement tests
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
| 2 | `./config/<name>.yaml` | **Your real site profile — gitignored** |
| 3 | `src/video_migrator/profiles/example.yaml` | The shipped, publishable default |

The default lives inside the package so an installed CLI works without a
checkout; it is included in the wheel via `[tool.setuptools.package-data]`.

**A real profile names real people** — preacher aliases are personal data — so
`config/*.yaml` is gitignored and only the generic `example.yaml` is committed.
To describe an actual site:

```bash
cp src/video_migrator/profiles/example.yaml config/mysite.yaml
$EDITOR config/mysite.yaml
VIDEO_MIGRATOR_CONFIG=config/mysite.yaml video-migrator-list
```

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

### Normalization rules

```yaml
normalize:
  title_replacements:      # regex -> replacement, applied in order
    '^\]\s+': ''           # single-quote both sides, see below
    '\]제': '] 제'
  valid_preacher_titles: [" 목사", " 선교사", " 장로"]
  title_spacing_words: ["강해"]
  preacher_names:          # scraped name -> canonical name
    홍길동목사: 홍길동 목사
    김영희: 김영희 목사
```

`title_replacements` runs first, in declaration order, using Python `re.sub`
syntax (`\1` for a capture group); `title_spacing_words` runs afterwards.
**Single-quote both the pattern and the replacement** — YAML processes
backslash escapes inside double quotes, so `"\]"` would not survive.

Profiles are loaded with a strict YAML loader that **rejects duplicate keys**.
Plain YAML keeps the last one, so forgetting the `- ` on a new board would
silently merge it into its predecessor; you get an error pointing at the line
instead.

### What stays in code

Not everything site-shaped belongs in config. `PLATFORM_LABELS` in
`scrapers/gnuboard.py` maps to parsers the module actually implements —
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
