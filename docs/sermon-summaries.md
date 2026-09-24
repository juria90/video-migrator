# A summary of the sermon in the description

An upload's description gains a summary of the sermon, produced by transcribing
the recording and summarizing the transcript. New uploads carry it as soon as
the stage exists; the 135 videos already published are backfilled afterwards.

**The two halves ship separately.** Transcription lands now, as part of the
pipeline; summarization is stubbed, because *which* summarizer to use is not yet
decided and cannot be decided before a transcript has been read (§4). So what
this plan delivers today is a transcript per recording, cached, and a
description that is published *only where a real summary exists*.

Companion plan: [Sortable title dates](sortable-title-dates.md). **This plan
depends on it** for two things — `tools/republish.py`, and the
`https://www.googleapis.com/auth/youtube` OAuth scope that lets
`videos.update` run at all. Ship that one first.

Everything below uses placeholders (`예시교회`, `홍길동 목사`, `example.org`) per
`CLAUDE.md`; the real values stay in the site profile, which is not in this
repository. `<site>` stands for the site directory throughout — naming the real
one here is exactly the leak `make no-real-data` exists to catch.

---

## 1. Where a summary comes from

Nothing in the pipeline holds sermon text: the export carries title, verse,
preacher, date, genre and language, and the CMS's `vodInfo` has no body. So the
summary is produced from the recording itself, in two steps — transcribe
locally, summarize with the Claude API — as a new pipeline stage.

Only the first step runs for now. The second is a stub that costs nothing and
writes a file a human can finish by hand.

## 2. A new `summarize` stage

Inserted between `repair` and `upload`, because the description is needed *at*
upload and the media is on disk only until `release`.

The stage is named for what it will eventually do rather than for what it does
today. Calling it `transcribe` now would mean renaming the stage and its column
when the real summarizer lands — and a stage column is written into every row of
a plan file that is merged, never rebuilt, so renaming one is not free.

| Module | Change |
| --- | --- |
| `src/video_migrator/plan.py` | `COLUMNS` gains `summarized_at` (after `repaired_at`); `STAGES` gains `("summarize", "summarized_at")` between repair and upload; `STAGE_COLUMNS` gains the matching entry so `--redo-from summarize` works |
| `tools/migrate.py` | `do_summarize(row, source, args)`; a `summarize` branch in `advance`; `CONTENDS_FOR["summarize"] = "asr"` with its own semaphore (size 1 — the GPU holds one model); `--stop-before` and `--redo-from` gain the choice; `--whisper-model`, `--whisper-device`, `--retranscribe`, `--summarize-backend` flags |
| `src/video_migrator/metadata/summarize.py` (new) | `transcript_path` / `summary_path`, `write_text` (atomic), `transcribe` over faster-whisper, `transcribe_cached` holding the cache rule, `read_summary` / `is_stub` holding the publishing rule, and `summarize(transcript, backend)` dispatching over §4 |
| `src/video_migrator/metadata/render.py` (new) | `_render`, promoted out of `upload_title.py` and taught to keep a label while still dropping a separator — see §5 |
| `src/video_migrator/metadata/upload_description.py` (new) | `format_upload_description`, and the 5,000-character check |
| `src/video_migrator/metadata/language.py` (new) | `LANGUAGE_TAGS`, moved out of `sinks/youtube.py` so that `metadata` need not import `sinks` to map `kor` to `ko` |
| `tools/republish.py` | `--mark-summarized`; `--description` with `--board`, `--site-dir` and `--board-name`; `videos_from` and `redescribe` |
| `pyproject.toml` | `faster-whisper`, `anthropic`, and the `nvidia-*-cu12` CUDA runtime, all as ordinary dependencies |
| `src/video_migrator/logs.py` | `CHATTY`, turning the huggingface client's per-request logging down to WARNING |

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

`faster-whisper` (CTranslate2), an ordinary dependency rather than an extra
beside `browser`, along with `anthropic`. It is a heavy install — CTranslate2
and onnxruntime come with it — but the stage is part of the pipeline rather
than beside it, and the cost of *not* having it is not an error message: a run
that reaches `summarize` without it stops the recording there, and if it is
waved past with `--summarize-backend none` the master is released and the
sermon's words are gone. `faster_whisper` is imported inside the function that
uses it, so the weight falls on disk rather than on the start-up of every run.

The machine has a Quadro T1000
(4 GiB), so `large-v3-turbo` at `int8_float16` on CUDA is the right size — the
full `large-v3` does not fit. `--whisper-device auto` falls back to CPU, where
the stage should instead share the `cpu` gate with x264 rather than run beside
it.

**The CUDA runtime has to be installed and preloaded**, which cost a run to
find out. `faster-whisper` does not pull `libcublas`/`libcudnn` in, and
CTranslate2 opens them by bare name from a directory the dynamic linker does not
search — so it fails at *inference*, after the model has loaded and the GPU has
shown activity, with `Library libcublas.so.12 is not found`. The wheels are
ordinary dependencies now, and `summarize._preload_cuda_libraries` opens them
with `RTLD_GLOBAL` at first use, because the usual `LD_LIBRARY_PATH` answer
cannot be applied from inside a process that is already running and would have
to be remembered on every future invocation. Where CUDA is unusable anyway,
`auto` degrades to the CPU once per run rather than failing each recording.

Rough cost: ~6 GPU-minutes per hour-long sermon. The migration is capped at
about 30 uploads a day, so transcription runs inside time the pipeline is
already spending and adds no wall-clock.

**Transcription reports its progress**, on the same `Ticker` the encoder uses
and for the same reason: an hour-long sermon otherwise says nothing for
twenty-five minutes, and a stalled transcription looks exactly like a slow one.
The huggingface client's per-request logging is turned down to WARNING in
`logs.configure` for the same reason — the first real run filled the screen with
resolve-cache URLs and was taken for hung while it was transcribing.

Input is the same file the upload sends (repaired if present, else master) —
`do_upload`'s inline choice gets pulled out into a `file_to_send(row)` helper
and shared. faster-whisper decodes the container itself; no ffmpeg step.

Language comes from the row (`kor`/`eng`), not from Whisper's detector, which
is unreliable on the first seconds of a service.

### Caching

**A transcript is written once and never produced twice.** `do_summarize` looks
for the transcript file first and reads it if it is there; it loads the Whisper
model only when the file is missing. `--retranscribe` is the one way to force
the work again, and it names the recordings it applies to rather than being a
blanket switch.

The cache is the file on disk, not the `summarized_at` stamp, and the two must
not be conflated. `--redo-from summarize` is for re-running *summarization* —
after the stub is replaced, or after a prompt changes — and it has to be cheap,
which it only is if it reuses the transcript. Six GPU-minutes to re-derive text
that is already sitting in a file is the exact cost this split avoids.

It also means the cache outlives the media. `release` deletes the master, so
after it has run the recording cannot be transcribed again at all — from Vimeo
only by re-fetching a gigabyte, and after the Vimeo account closes not at all.
The transcript file is the only copy of the sermon's words that survives, which
is why it is kept rather than treated as a scratch artifact.

Two failure modes the presence check has to get right:

- **A partial write is not a cache hit.** Transcription is minutes long and the
  run is expected to be interrupted; a half-written file that is read back as
  complete is a silently truncated sermon. Written to a neighbouring
  `.writing` file and moved into place, the same way `write_plan` does it.
- **An empty transcript is a failure, not a result.** Whisper returns an empty
  string on a silent or corrupt file. That is stamped as a note and left
  outstanding, not cached — otherwise the emptiness becomes permanent.

### Where the transcripts live

Transcripts are the most identifying data this project produces — a sermon's
full text, naming the people in the room. They go in the site's own directory,
which is a separate private repository cloned into place:

```
sites/<site>/transcript/<num>-<vimeo_id>.txt
```

Plain text rather than gzipped. All 686 come to roughly 30 MB, which is nothing
beside the masters, and the file's whole job now is to be opened and read — it
is what a stub summary is written from and what a human edits against. A
`.txt.gz` that has to be piped through `zcat` to be glanced at buys 20 MB and
costs that.

**This path sits outside `data/`, and that is a deliberate departure from where
the rest of the site's artifacts live** — it is checked into the site's own
repository, because a transcript is not regenerable scrape output. It has a
consequence, in Risks below: the outer repository's `/sites/*/data/*` ignore
rule does not cover it.

## 4. Summarizing — a backend seam, stubbed by default

Summarization is one function behind one flag, `--summarize-backend`, over four
values. Whichever wins, the stage, the cache, the summary file and the
publishing rule in §5 are unchanged — only this function differs. That seam is
the point: it is what makes the choice reversible and the comparison below cheap
to run, and it should exist even once the choice is settled.

| Backend | What it does | Status |
| --- | --- | --- |
| `stub` | Writes the transcript into the summary file under a marker. No model, no key, no network | The default, today |
| `api` | One Claude API call per transcript | Written, not yet chosen |
| `local` | An open-weights model on this machine | To be evaluated (see the bake-off below) |
| `none` | Stamps `summarized_at` and moves on. No transcription, no summary file | The escape hatch |

`none` is where a `--no-summarize` boolean would otherwise have gone, and it is
deliberately not one — `--no-repair` is a boolean because repair has one
alternative, whereas skipping is simply the fourth thing this stage can do.
Keeping it on the same flag means what the stage did is answerable from one
value rather than from a flag and a boolean read together, and it makes the
illegal combination (`--no-summarize --summarize-backend api`) unspellable
rather than something `advance` has to resolve.

**`none` is the one value that loses something irreversibly.** The other three
all end with a transcript on disk; this one skips the recording entirely, and
`release` then deletes the master. Used on a row that goes on to finish, it
costs the sermon's words permanently (§3). It is for a Whisper failure that
would otherwise stop an upload run — not for saving six GPU-minutes.

Whatever runs, the prompt asks for the summary in the sermon's own language,
three to five sentences, drawn only from the transcript — no invented scripture,
no invented names. The verse and preacher in the description come from the
export, never from the transcript, since Whisper mangles proper nouns and
references.

### The stub, which is what runs today

`summarize()` makes no API call, needs no `ANTHROPIC_API_KEY`, and writes the
transcript into the summary file under a marker:

```markdown
<!-- stub: transcript only, not summarized -->

<the transcript>
```

The point of seeding the file with the transcript rather than leaving it empty
is that the summary file becomes the one place to work: open it, read the
sermon, write three sentences at the top, delete the marker line. A file that is
still carrying its marker is not a summary and is never published (§5).

### `api` — one Claude call per transcript

`claude-opus-5`, adaptive thinking, `output_config={"effort": "low"}` — the task
is summarization, not reasoning — `max_tokens=2000`. A one-hour Korean sermon is
roughly 20K input tokens against ~1K out once thinking is counted, which prices
the back catalogue at:

| Model | Per sermon | All 686 | Batched (−50%) |
| --- | --- | --- | --- |
| `claude-opus-5` ($5/$25 per MTok) | ~$0.13 | ~$86 | ~$43 |
| `claude-sonnet-5` ($2/$10) | ~$0.05 | ~$34 | ~$17 |
| `claude-haiku-4-5` ($1/$5) | ~$0.025 | ~$17 | ~$8 |

None of this is latency-sensitive — the pipeline is upload-bound at about 30 a
day — so the Batch API's half price costs nothing but patience. **`sonnet-5`
batched is roughly $17 for all 686**, which is less than the electricity a local
run would burn (below). Cost is therefore *not* the reason to prefer a local
model, and this plan should stop implying it is.

`ANTHROPIC_API_KEY` will need adding to `.env` if this backend is chosen; it is
read the same way `VIMEO_ACCESS_TOKEN` is, via the `load_token` pattern. Nothing
reads it before then.

### `local` — an open-weights model on this machine

The real argument for running the summarizer locally is not money. It is that
**the transcripts never leave the machine.** These are full sermon texts naming
a real congregation; "it never left the laptop" is a categorically stronger
claim than any vendor's retention promise, and it is the claim most consistent
with how the rest of this project treats the site's data. That is the case to
weigh, and the price table above is beside it.

What the hardware allows, measured rather than assumed:

- **GPU: Quadro T1000 Max-Q, 4096 MiB.** That caps a resident model at roughly
  4B parameters at Q4 (~2.5 GB of weights) plus KV cache for a 20K context. It
  is also the same 4 GiB the Whisper model wants, and the stage is already gated
  at size 1 on the `asr` semaphore — so the two would load and unload around
  each other, per recording.
- **CPU: i7-10875H, 8 cores / 16 threads @ 2.3 GHz. RAM: 15 GB, ~11 free**
  (WSL2). An 8B at Q4 (~5 GB) fits; a 14B at Q4 (~9 GB) fits but leaves little
  for anything else. Prefilling 20K tokens on eight Comet Lake cores runs
  10–15 minutes per sermon at 8B — **about 7 days of continuous CPU for all
  686**, roughly double at 14B.

The wall clock is there: 686 recordings at 30 uploads a day is ~23 days, and the
migration is upload-bound. But it is the *same* CPU the x264 repair stage
contends for, on a thermally limited laptop, so this is not free the way the
`--no-repair` arithmetic is free.

Quality is the part that actually decides it, and three things stack against a
small model here:

1. **The output has to be Korean.** Small multilingual models fall off harder in
   Korean than their English benchmarks suggest. The families worth testing are
   Qwen3-4B and the Korean-native ones — LG's EXAONE, Kakao's Kanana, Naver's
   HyperCLOVA X SEED — but which is best should be checked at bake-off time
   rather than settled here.
2. **20K tokens in one shot is where small models drift.** They over-weight the
   start and end of the input, which in a sermon is the greeting and the
   benediction — the two least informative stretches. The mitigation that works
   is chunk-and-reduce (summarize ~4K windows, then combine), which sidesteps
   the long-context weakness at the cost of several passes per recording, so the
   day estimates above go up.
3. **The input is already lossy.** Whisper mangles proper nouns; a weak
   summarizer on top compounds it. Two lossy steps is one more than this is
   worth.

Nothing is installed for this yet — no ollama, no llama.cpp, no torch — so
`local` is a real piece of work, not a flag flip.

### The bake-off that decides it

Once transcription has run, take five transcripts and summarize each with a
local 4B, with `claude-sonnet-5`, and with `claude-opus-5`. **That costs about
$0.60 of API spend.** Judge it by reading the Korean — whether the summary says
what the sermon said, and whether it invented a name or a reference — not by
benchmark scores.

The expectation going in is that the local model loses on Korean at this VRAM
budget and that `sonnet-5` batched at ~$17 wins on the merits. The privacy
argument is strong enough that it deserves the $0.60 rather than the assumption.

Summaries live beside the transcripts, for the same reason:

```
sites/<site>/summaries/<num>.md
```

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
and YouTube's collapsed view shows only the first couple of lines.

**The summary is published only where one exists.** `do_upload` reads
`sites/<site>/summaries/<num>.md` and fills `{summary}` from it; where the file
is missing, or still carries the stub marker, the slot renders empty and the
description is the metadata-only one being published today. So the stage can be
switched on without blocking uploads, and — while summarization is stubbed —
every upload keeps exactly the description it has now, gaining a transcript on
disk and nothing else.

Publishing a stub would be actively wrong, not merely unhelpful: an hour-long
transcript is 20–40K characters against YouTube's 5,000-character description
limit, so it would be refused or truncated mid-sentence into the description of
a real sermon. The marker check is what prevents that, and the rendered result
is length-checked against the 5,000 limit regardless — a hand-written summary
can run long too.

## 6. Backfilling the videos already published

The 135 are backfilled separately from the retitle, to keep the two changes
independently reviewable. Their masters were deleted at `release`, so the audio
has to come from somewhere:

- **Preferred:** pull audio-only from the channel's own copy with yt-dlp
  (`-f bestaudio`, `https://youtu.be/<youtube_id>`). No Vimeo quota, a few tens
  of MB each instead of ~1 GiB.
- **Fallback:** `--redo-from fetch` on those rows, which re-downloads the
  masters from Vimeo.

Transcribing them is worth doing now even though summarizing them is not: it is
free, it is the same six GPU-minutes each, and it is the step that stops being
possible if the channel copy ever goes. The transcripts land in the same
directory under the same cache rule, so a later summarization pass reads them
rather than re-deriving them.

Pushing the descriptions is `tools/republish.py --description --board <export>
--apply`, at 50 quota units per video — the same arithmetic as the retitle, so
the same "not on an upload day" rule applies.

**It needs the export, and that is not incidental.** A description carries the
verse and the preacher; the plan holds neither, because it records what a
recording *became* rather than what it said. Rebuilding a description from the
plan alone would have sent back a description with those two lines missing —
and `videos.update` replaces the field whole, so it would have deleted them
from 135 live videos. The export is where they come from, for the backfill
exactly as for a fresh upload.

Three things are refused rather than guessed at, on the principle the retitle
pass already runs on:

- a summary that is missing or still a stub — skipped and counted, which today
  is all 135 of them, so the flag exists and does nothing until summaries are
  written;
- a description that is not the one this migration wrote, checked exactly
  against what the profile renders for that recording with no summary. That is
  what protects a hand-edited description, and it is also what stops a second
  pass stacking a changed summary on top of the one already published;
- a recording the export does not cover, whose verse and preacher are unknown.

## Order of work

| # | Step | Verified by |
| --- | --- | --- |
| 1 | `summarize` stage + `--mark-summarized` on the released rows | `tally_stages` unchanged for finished rows |
| 2 | Transcribe one recording end to end; run the stage again | a transcript file appears; the second run loads no model and takes seconds |
| 3 | Description template + `do_upload` wiring, summary absent | one new upload, description byte-identical to today's |
| 4 | Write one summary by hand, delete its marker, publish it | that video on YouTube; an untouched neighbour unchanged |
| 5 | Backfill the 135 transcripts via yt-dlp audio | transcript count; no API spend |
| 6 | The bake-off: five transcripts through `local`, `sonnet-5` and `opus-5` | reading the Korean side by side; ~$0.60 spent |
| 7 | Switch `--summarize-backend` to whichever won, and run it | cost or GPU-hours per sermon against §4's estimates |

## Risks

- **`summarized_at` shifts stage accounting** for every existing row (§2). The
  `--mark-summarized` pass has to run before the stage does, or finished rows
  get sent back to `fetch`.
- **`sites/<site>/transcript/` is not covered by any ignore rule in this
  repository.** The root `.gitignore` ignores `/sites/*/data/*`, and this path
  is not under `data/`. What keeps it out is only that the site directory is a
  nested git repository, which git does not descend into — and for the same
  reason `make no-real-data` never inspects it either: `git ls-files -co`
  yields the nested repository as a single directory entry, which
  `tracked_files()` drops because it is not a file. So there is no check
  standing behind this, and a transcript copied anywhere else in the tree — a
  scratch file, a test fixture, a paste into a commit message — is a leak that
  nothing will catch.
- **A `local` backend contends for the CPU the repair stage uses**, and for the
  4 GiB the Whisper model wants (§4). It is the one option in this plan whose
  cost is wall-clock and heat on a laptop rather than dollars, and that cost
  does not show up in any of the tables.
- **Whisper hallucinates on silence and mangles proper nouns.** Summaries are
  drafts for review, which is why they land as editable files rather than going
  straight to YouTube, and why the stub marker has to be deleted by hand.
- **A stub published as a description would be a 30,000-character transcript**
  where a summary belongs (§5). Both writers — `do_upload` and
  `tools/republish.py --description` — have to test the marker, not merely test
  that the file exists.
- **`videos.update` replaces the whole snippet** — same trap as the retitle, and
  it drew blood there: send back the title, `categoryId`, `tags`,
  `defaultLanguage` *and* `defaultAudioLanguage` that were read. Use
  `writable_snippet()` rather than assembling the body by hand, and verify a
  single video against an untouched one before any bulk pass.

## Open questions

- Does keeping the transcripts on this machine outweigh a local model's worse
  Korean? That is the whole `local`-versus-`api` question, and the bake-off is
  designed to answer the second half of it — the first half is a judgement about
  the site's data that no measurement settles.
- Should a transcript be required before an upload proceeds, or is
  `--summarize-backend none` enough of an escape hatch when transcription
  fails? The stage is cheap and the media is gone after `release`, which argues
  for requiring it — but a Whisper crash would then stop the upload run, and
  the escape hatch is exactly the value that forfeits the transcript.
