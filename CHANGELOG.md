# Changelog

## Unreleased — migrate to the new source site

The source site moved to a different CMS. The boards, their contents and their
record ids all survived; the software underneath did not. Everything below
follows from that.

### Added

- **`scrapers/churchlove.py`** — scraper for the VOD module of the 교회사랑넷
  (church-love.net) church CMS. The listing is server-rendered HTML, so no
  browser is needed, but the video URL lives behind an XML endpoint that answers
  POST only. One listing fetch enumerates a page; one POST resolves each
  recording.
- **`scrapers/platforms.py`** — `identify()` turns any embed or watch URL into
  `(platform, id, canonical_url)`. `PLATFORM_LABELS` and `summarize_types()`
  moved here out of `gnuboard.py`, which now shares them.
- **`profiles/churchlove.yaml`** — publishable example profile for that CMS.
- **`tools/check_no_real_data.py`** — run by `make lint`. Checks committable
  files for Korean outside an allowlist, for site-specific terms read from a
  gitignored file, and for platform ids that could resolve to a real video.
- **`--profile/-P`** on the listing CLI, so a board is scraped against a chosen
  profile rather than the built-in default.

### Changed

- **Profiles describe more of a site.** `site.scraper` lets a profile under
  `config/` name the software it runs, so adding a site needs no code. Boards
  gained `page_code` (how the CMS addresses a board) and `weekday` (the day the
  service is held). Normalization gained `bible_verse_replacements`,
  `service_parts`, `preacher_service_pattern`, `valid_preacher_prefixes` and
  `max_publish_date_drift`.
- **Publish dates are corrected against the board's weekday.** The site recorded
  the day a recording was *posted* rather than the day it was preached for a
  five-year stretch; 322 of 806 Sunday sermons were stamped Monday. Corrections
  run backwards only and never onto a date already holding the same service.
- **`fix_video_metadata()` returns its changes** instead of applying them
  silently. The CLI prints a count, and each rewrite under `-v`.
- **Preacher validation accepts titles in either position.** Korean titles
  follow a name and English ones precede it, so a single "ends with" rule
  rejected every English-speaking guest.
- **A service part recorded against the preacher moves into the title**, where
  the rest of the board keeps it — and where the date logic can use it to tell
  two services on one day apart.
- **`HTTPCache` supports POST**, and sniffs the charset when a response declares
  none. The listing pages send `text/html` with no charset, so every Korean
  field was decoding as ISO-8859-1.
- **`config/` and `data/` are gitignored wholesale** rather than by extension.
  The old rule covered `*.csv` and `config/*.yaml`; a `.tsv` of preacher names
  would have been committed.

### Fixed

- Duplicate records are reported — keyed on the **video URL**, not on title and
  date, because a two-service Sunday files one sermon twice as two recordings.
- Titles missing an opening bracket are repaired, matching the existing rule for
  `]`.
- `year` moves with `publish_date` when a date is corrected. Both are written
  into the media file, and a Monday 1 January belongs to the year before.

### Documentation

- **CLAUDE.md gained a Real-World Data section.** Nothing committed may carry
  data identifying the people the site is about. The rule existed in prose and
  in `.gitignore`; it was broken four times during this work, every time by
  reaching for a real example while writing docs or a test.

### Migration notes

The site's own data carries defects the pipeline cannot invent answers for.
`config/build_todo.py` regenerates the outstanding list from a scrape, so it
shrinks as records are corrected and never needs reconciling by hand:

```bash
python config/build_todo.py .cache 9 sunday_sermon
```

As of the last run, `sunday_sermon` has 137 outstanding items — 53 records whose
video URL is empty, 34 bible verses missing or malformed, 26 titles off the
agreed style, 20 preachers missing or untitled, and 2 duplicate pairs.

Sixteen preacher names were recovered from the church's own weekly bulletins,
which are readable without login once the image path is known. Two publish dates
were settled the same way, and one by sermon-series order where no bulletin
reached back far enough.
