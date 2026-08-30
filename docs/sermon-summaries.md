# A summary of the sermon in the description

An upload's description gains a summary of the sermon, produced by transcribing
the recording and summarizing the transcript. New uploads carry it as soon as
the stage exists; the 135 videos already published are backfilled afterwards.

Companion plan: [Sortable title dates](sortable-title-dates.md). **This plan
depends on it** for two things — `tools/republish.py`, and the
`https://www.googleapis.com/auth/youtube` OAuth scope that lets
`videos.update` run at all. Ship that one first.

Everything below uses placeholders (`예시교회`, `홍길동 목사`, `example.org`) per
`CLAUDE.md`; the real values stay in the site profile, which is not in this
repository.

---

## 1. Where a summary comes from

Nothing in the pipeline holds sermon text: the export carries title, verse,
preacher, date, genre and language, and the CMS's `vodInfo` has no body. So the
summary is produced from the recording itself, in two steps — transcribe
locally, summarize with the Claude API — as a new pipeline stage.

## 2. A new `summarize` stage

Inserted between `repair` and `upload`, because the description is needed *at*
upload and the media is on disk only until `release`.

| Module | Change |
| --- | --- |
| `src/video_migrator/plan.py` | `COLUMNS` gains `summarized_at` (after `repaired_at`); `STAGES` gains `("summarize", "summarized_at")` between repair and upload; `STAGE_COLUMNS` gains the matching entry so `--redo-from summarize` works |
| `tools/migrate.py` | `do_summarize(row, source, args)`; a `summarize` branch in `advance`; `CONTENDS_FOR["summarize"] = "asr"` with its own semaphore (size 1 — the GPU holds one model); `--stop-before` and `--redo-from` gain the choice; `--whisper-model`, `--whisper-device`, `--no-summarize` flags |
| `src/video_migrator/metadata/summarize.py` (new) | `transcribe(path, ...) -> str` over faster-whisper, and `summarize(transcript, video, ...) -> str` over the Claude API |

An existing plan file gains the new column the first time `write_plan` runs;
`read_plan`/`merge` fill missing keys, so the 135 finished rows read as
`summarize`-outstanding rather than `done`. That is correct — they *are*
outstanding — but it means `tally_stages` will show them there, and
`outstanding()` will hand them to `advance`, which would try to re-fetch them.
**So the stage lands with a guard**: a row that already has `released_at` is
stamped `summarized_at = "n/a"` by a one-off pass (`tools/republish.py
--mark-summarized`) before the new stage goes live, and is picked up instead by
the backfill in §6.

## 3. Transcribing

`faster-whisper` (CTranslate2), added as a `summarize` optional extra beside the
existing `browser` one, along with `anthropic`. The machine has a Quadro T1000
(4 GiB), so `large-v3-turbo` at `int8_float16` on CUDA is the right size — the
full `large-v3` does not fit. `--whisper-device auto` falls back to CPU, where
the stage should instead share the `cpu` gate with x264 rather than run beside
it.

Rough cost: ~6 GPU-minutes per hour-long sermon. The migration is capped at
about 30 uploads a day, so transcription runs inside time the pipeline is
already spending and adds no wall-clock.

Input is the same file the upload sends (repaired if present, else master) —
`do_upload`'s inline choice gets pulled out into a `file_to_send(row)` helper
and shared. faster-whisper decodes the container itself; no ffmpeg step.

Language comes from the row (`kor`/`eng`), not from Whisper's detector, which
is unreliable on the first seconds of a service.

## 4. Summarizing

One Claude API call per transcript. `claude-opus-5`, adaptive thinking,
`output_config={"effort": "low"}` — the task is summarization, not reasoning —
`max_tokens=2000`. A one-hour Korean sermon is roughly 20K input tokens, so at
Opus 5's $5/$25 per MTok that is about **$0.11 per sermon, ~$77 for all 686**.
Two levers if that is too much: the Batch API halves it, and `claude-sonnet-5`
takes it to about $31.

The prompt asks for the summary in the sermon's own language, three to five
sentences, drawn only from the transcript — no invented scripture, no invented
names. The verse and preacher in the description come from the export, never
from the transcript, since Whisper mangles proper nouns and references.

`ANTHROPIC_API_KEY` is not in `.env` yet and will need adding; it is read the
same way `VIMEO_ACCESS_TOKEN` is, via the `load_token` pattern.

**Where the artifacts live.** Transcripts and summaries are the most
identifying data this project produces, so both go under the site's gitignored
data directory, never in the repository:

```
sites/<site>/data/transcripts/<num>-<vimeo_id>.txt.gz
sites/<site>/data/summaries/<num>.md
```

The transcript is kept because it cannot be regenerated once the master is
released, and it is what a re-summarization would run against. Gzipped, all 686
come to roughly 10 MB. The summary is a plain file so it can be read and edited
by hand before it is published — the tool re-reads it at push time.

## 5. Publishing the description

`upload.description_template` in the profile, mirroring `title_template`, over
the slots `{summary}`, `{verse}`, `{artist}`, `{date}`, `{service}`,
`{church}`. `format_upload_description` lives in a new
`metadata/upload_description.py` and reuses `_render` from `upload_title.py`
(promoted to a shared helper), so an empty slot still takes its separator with
it. Proposed default:

```
{summary}

본문: {verse}
설교: {artist}
예배: {date} {service}
{church}
```

The summary leads because the title already carries date, preacher and verse,
and YouTube's collapsed view shows only the first couple of lines. The result
is checked against YouTube's 5,000-character description limit.

`do_upload` reads the summary file if one exists and falls back to today's
metadata-only description when it does not, so the stage can be switched on
without blocking uploads.

## 6. Backfilling the videos already published

The 135 are backfilled separately from the retitle, to keep the two changes
independently reviewable. Their masters were deleted at `release`, so the audio
has to come from somewhere:

- **Preferred:** pull audio-only from the channel's own copy with yt-dlp
  (`-f bestaudio`, `https://youtu.be/<youtube_id>`). No Vimeo quota, a few tens
  of MB each instead of ~1 GiB.
- **Fallback:** `--redo-from fetch` on those rows, which re-downloads the
  masters from Vimeo.

Then `tools/republish.py --description --apply` pushes them, at 50 quota units
per video — the same arithmetic as the retitle, so the same "not on an upload
day" rule applies.

## Order of work

| # | Step | Verified by |
| --- | --- | --- |
| 1 | `summarize` stage + `--mark-summarized` on the released rows | `tally_stages` unchanged for finished rows |
| 2 | Transcribe + summarize one recording end to end | read the summary file; check cost and GPU time |
| 3 | Description template + `do_upload` wiring | one new upload, checked on YouTube |
| 4 | Backfill the 135 descriptions via yt-dlp audio | reviewed in batches before `--apply` |

## Risks

- **`summarized_at` shifts stage accounting** for every existing row (§2). The
  `--mark-summarized` pass has to run before the stage does, or finished rows
  get sent back to `fetch`.
- **Whisper hallucinates on silence and mangles proper nouns.** Summaries are
  drafts for review, which is why they land as editable files rather than going
  straight to YouTube.
- **Transcripts are the most identifying data here.** They must stay under the
  gitignored site data directory; `make no-real-data` does not read outside
  committable files, so nothing will catch a copy left elsewhere.
- **`videos.update` replaces the whole snippet** — same trap as the retitle, and
  it drew blood there: send back the title, `categoryId`, `tags`,
  `defaultLanguage` *and* `defaultAudioLanguage` that were read. Use
  `writable_snippet()` rather than assembling the body by hand, and verify a
  single video against an untouched one before any bulk pass.

## Open questions

- Is `claude-opus-5` at ~$77 the right call, or should the run use the Batch
  API (~$39) given none of it is latency-sensitive?
- Should a summary be required before an upload proceeds, or is
  `--no-summarize` enough of an escape hatch when transcription fails?
