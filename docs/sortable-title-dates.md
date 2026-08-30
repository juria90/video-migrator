# Sortable title dates

The date in an upload's title becomes `YYMMDD` (`251219`) instead of `MMDDYY`
(`121925`), so that sorting a channel by title sorts it by date. New uploads use
the new format; the 135 videos already published are retitled in place.

**Status: done (2026-08-30).** All 135 retitled, 0 failed, plan and live titles
verified identical. Two lessons from doing it are folded into §5 below; both
matter to any later pass that edits published videos.

Companion plan: [A summary of the sermon in the description](sermon-summaries.md).
That one depends on the tool and the OAuth scope introduced here, but nothing
here depends on it — this plan stands on its own and can ship first.

Everything below uses placeholders (`예시교회`, `홍길동 목사`, `example.org`) per
`CLAUDE.md`; the real values stay in the site profile, which is not in this
repository.

---

## 1. What changes

| File | Change |
| --- | --- |
| `sites/<site>/<site>.yaml` | `upload.date_format: "{month:02d}{day:02d}{short_year:02d}"` → `"{short_year:02d}{month:02d}{day:02d}"` |
| `tests/test_upload_title.py` | the `bracketed` fixture's `upload_date_format`, its docstring ("an MMDDYY date"), and the four expected titles: `080226` → `260802` |
| `src/video_migrator/metadata/upload_title.py` | nothing — `format_date` already takes the format from the profile. Its doctests cover both orderings already |

`{short_year:02d}` is needed rather than `{short_year}`: 2005 renders as `5`
without the pad, and a five-digit date sorts wrong.

No change to `format_upload_title`, the ffmpeg metadata, or `recordingDate` —
those already carry the real date in ISO form.

## 2. Retitling the videos already published

There are 135 rows in the plan with a `youtube_id`, all of them one board. A
new tool, `tools/republish.py`, brings a published video's snippet back into
line with the plan. It is written here for titles and reused by the companion
plan for descriptions.

**Inputs**, all from the site directory, which is not in this repository:

| File | Supplies |
| --- | --- |
| `sites/<site>/data/migration-plan.tsv` | `num`, `youtube_id`, `published`, `title` — the only record of which recording became which video |
| `sites/<site>/<site>.yaml` | `upload.date_format`, before and after, and `title_template` |
| `sites/<site>/data/<site>-<board>.tsv` | the optional cross-check only (`--board`) |

Retitling therefore needs no export: the date tokens come from `published` and
the profile, and the text they are swapped into comes from YouTube. `--board`
is optional, and without it the recompute cross-check is simply skipped.

**Mechanism — swap the date token, do not rebuild the title.** Rebuilding from
the export would silently revert any correction applied at the source since the
upload, and would fail on a row whose export entry has since changed. So, per
row:

```
old = format_date(row["published"], OLD_FORMAT)      # "121925"
new = format_date(row["published"], NEW_FORMAT)      # "251219"
live = the title read back from YouTube
```

Replace `old` with `new` in `live`, requiring **exactly one** match, not
adjacent to another digit (`(?<!\d)…(?!\d)`). Zero or several matches means the
tool cannot be sure which six digits are the date: it reports the row and
changes nothing. It also recomputes `format_upload_title` from the export and
reports where that disagrees with the live title — as information, not as an
edit, since the live title is what gets patched.

`old == new` on a date like 2011-11-11; that row is simply skipped.

**Resumability.** No new plan column. The tool reads the live title first (it
has to — see the quota note), so a row whose live title already carries the new
token is skipped. Interrupt it anywhere and running it again continues.

**The plan's `title` column** is updated with the same substitution and the
plan rewritten with `write_plan`, so the file keeps matching what is published.

**CLI**, following `admin_browser.py`'s convention — prints what it would send
and sends nothing unless told to:

```
uv run python tools/republish.py --plan sites/<site>/data/migration-plan.tsv \
    --profile <site> --title [--board <export>.tsv] [--limit N] [--apply]
```

## 3. The OAuth scope — the one blocking prerequisite

`videos.update` is not permitted by either scope the project currently asks
for (`youtube.upload`, `youtube.readonly`), so `YOUTUBE_SCOPES` in
`src/video_migrator/sinks/youtube.py` gains
`https://www.googleapis.com/auth/youtube`.

`oauth2client` does not notice that the scopes it is asked for have changed: a
token granted under the old pair loads with `invalid` False and fails at the
first update with a 403. **This is handled in code rather than by hand.**
`get_authenticated_service` compares the scopes a cached token carries against
the ones the run needs and consents again when any is missing — and separately,
refreshes a stale token *before* the first request, so a refresh token that has
been revoked asks for consent instead of raising from inside the transport
several stages into a run.

Deleting the token by hand is therefore not a step, and should not be made one:
`existing_credentials` settles for the single token it can find when no channel
is named, so removing the expected file does not produce a fresh consent — it
produces whatever other channel's token is lying beside it.

Re-consenting is still the risky step in this plan: **which channel the token
belongs to is decided at the consent screen**, and consenting as the wrong
Brand Account would break the rest of the migration. So:

1. Note the current channel id: `uv run python -m video_migrator.sinks.youtube --verify-only`
2. Run it again once the new scope is in place; it says which scope is missing,
   consents, and writes the token back where it was
3. Confirm the same channel id comes back
4. Only then run the retitle with `--apply`

## 4. Quota

`videos.update` costs 50 units against a default 10,000/day; `videos.list`
costs 1 and takes up to 50 ids per call. So the whole retitle is
`3 + 135×50 = 6,753` units — one day's work, but it leaves room for only about
two uploads (`videos.insert` is 1,600 each). **Run the retitle on a day with no
uploads**, or split it with `--limit`. If a run does hit the quota, it stops on
the 403 and the next run resumes where it left off.

## Order of work

| # | Step | Verified by |
| --- | --- | --- |
| 1 | Profile date format + tests | `make check` |
| 2 | `youtube` scope added; token moved aside and re-consented | `--verify-only` reports the same channel id as before |
| 3 | `tools/republish.py --title`, run without `--apply` | the printed before/after for all 135 read correctly |
| 4 | Same with `--apply`, on a day with no uploads | spot-check on YouTube; plan `title` column matches |

## 5. What doing it taught

- **`videos.update` clears every writable field you omit — including
  `defaultAudioLanguage`.** The API reference does not list that field among the
  writable ones for `videos.update`, but it behaves as writable: the first
  retitle wiped it and YouTube re-derived the sermon's audio as `en-US`, which
  changes captions, auto-translation and recommendations. It is now in
  `WRITABLE_SNIPPET_FIELDS`. Caught by editing one video and comparing every
  field against one left untouched — the edit itself reported success.
- **`videos.list` is eventually consistent.** It served pre-edit values from
  seconds to ~3 minutes after an update, and batched reads (50 ids) stayed stale
  longer than single-id reads. Two wrong conclusions came out of reading too
  soon. Verification needs a 60-90s wait, and a single row disagreeing inside a
  large batch should be re-read alone before it is believed.
- **Never move the cached token aside to force a re-consent.**
  `existing_credentials` settles for the one token it can find when no channel
  is named, so removing the expected file picks up another channel's token
  instead of consenting afresh. Scope drift is handled in code now (§3).

## Risks

- **Re-consenting to the wrong channel** (§3) is the only irreversible step
  here. The `--verify-only` check before and after is what catches it.
- **`videos.update` replaces the whole snippet.** The tool must send back every
  writable field it read — `description`, `tags`, `categoryId`,
  `defaultLanguage` and `defaultAudioLanguage` — or they are cleared. Reading
  first is not just for idempotence.
- **A title where the six digits appear twice** is skipped and reported rather
  than guessed at.
- **Bulk edits at speed** can look like abuse; the tool paces itself with a
  short sleep between updates.
- **The plan file is the only `num` → `youtube_id` record.** Reading titles back
  off YouTube would not rebuild it. Copy it before step 4 runs with `--apply`.

## Open questions

- Should the retitle also make the 135 public, or do they stay unlisted?
  Nothing here changes privacy.
