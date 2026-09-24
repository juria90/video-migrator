#!/usr/bin/env python3
"""
Turn a recording into a transcript, and a transcript into a summary.

Nothing in the pipeline holds sermon text: the export carries title, verse,
preacher, date, genre and language, and the CMS has no body. So the summary that
goes in an upload's description is produced from the recording itself, in two
steps — transcribe locally, then summarize.

**The transcript is written once and never produced twice.** It is not a scratch
artifact: ``release`` deletes the master, and after that the recording cannot be
transcribed again at all. The file on disk is the only copy of the sermon's
words that survives, and it is what a re-summarization runs against, so
:func:`transcribe_cached` reads it in preference to loading a model.

Summarization is one function, :func:`summarize`, over four backends — see
:data:`BACKENDS`. Which one to use is not settled; the default writes the
transcript into the summary file under :data:`STUB_MARKER` so a person can
finish it by hand, and a file still carrying that marker is never published.
"""

import ctypes
import importlib
import logging
import pathlib
import threading

from ..logs import Ticker
from .language import LANGUAGE_TAGS

logger = logging.getLogger(__name__)

#: What ``--summarize-backend`` accepts.
#:
#: ``stub``
#:     Writes the transcript into the summary file under :data:`STUB_MARKER`.
#:     No model, no key, no network. The default.
#: ``api``
#:     One Claude API call per transcript.
#: ``local``
#:     An open-weights model on this machine. Not implemented yet.
#: ``none``
#:     Skips the stage entirely — no transcription, no summary file. The escape
#:     hatch, for a transcription failure that would otherwise stop an upload
#:     run. It is the one value that loses something irreversibly: every other
#:     backend leaves a transcript on disk, and ``release`` then deletes the
#:     master, so a row summarized as ``none`` loses the sermon's words for good.
BACKENDS = ("stub", "api", "local", "none")

#: Marks a summary file as holding a transcript rather than a summary. A file
#: carrying it is not published; deleting the line by hand is what promotes it.
STUB_MARKER = "<!-- stub: transcript only, not summarized -->"

#: The Whisper model that fits the 4 GiB card this project runs on. The full
#: ``large-v3`` does not.
DEFAULT_WHISPER_MODEL = "large-v3-turbo"

#: How that model is quantized on CUDA. On CPU faster-whisper wants ``int8``.
CUDA_COMPUTE_TYPE = "int8_float16"
CPU_COMPUTE_TYPE = "int8"

#: The model, kept between recordings. Loading it costs seconds and a gigabyte
#: of VRAM, and a back-catalogue run does this several hundred times — so it is
#: loaded once and reused. Keyed by what was asked for, so that changing
#: ``--whisper-model`` mid-run still loads the model asked for rather than
#: quietly going on with the last one.
_MODELS: dict[tuple[str, str], object] = {}
_MODELS_LOCK = threading.Lock()

#: What ``auto`` has turned out to mean on this machine. It starts as ``cuda``
#: and becomes ``cpu`` the first time CUDA proves unusable, so that a run pays
#: for that discovery once rather than on all 686 recordings.
_AUTO_DEVICE = "cuda"

#: What CTranslate2 says when the CUDA runtime is missing. It is a bare
#: ``RuntimeError``, so the message is the only thing distinguishing "this
#: machine cannot do CUDA" from "this recording is broken" — and the two must
#: not be treated alike: one is a fallback, the other is a fault.
CUDA_FAILURES = ("libcublas", "libcudnn", "libcuda", "cuda", "cudnn", "no kernel image", "gpu")

#: The CUDA runtime libraries CTranslate2 opens by name, in load order — cuDNN
#: needs cuBLAS already present. Supplied by the ``nvidia-*-cu12`` wheels.
CUDA_LIBRARY_PACKAGES = ("nvidia.cublas.lib", "nvidia.cudnn.lib")

#: Whether :func:`_preload_cuda_libraries` has run. Once is enough per process.
_PRELOADED = False


def _preload_cuda_libraries() -> int:
    """
    Put the CUDA runtime where CTranslate2 can find it.

    The ``nvidia-*-cu12`` wheels install their shared objects under
    ``site-packages/nvidia/*/lib``, which is not on the dynamic linker's path.
    CTranslate2 opens them by bare name — ``libcublas.so.12`` — and fails with
    ``Library libcublas.so.12 is not found or cannot be loaded`` when it cannot,
    *at inference rather than at load*, so the model appears to come up fine and
    the GPU even shows activity before it dies.

    The usual answer is to export ``LD_LIBRARY_PATH`` before starting Python,
    which cannot be done from inside a process that is already running and which
    every future invocation would have to remember. Opening them here with
    ``RTLD_GLOBAL`` instead puts them in this process's global symbol namespace,
    where CTranslate2's own ``dlopen`` then finds them — so ``uv run python
    tools/migrate.py`` works with no wrapper and no environment to set up.

    Absence is not an error: a machine with no NVIDIA GPU has no such wheels,
    and it is the CPU path's business to be unaffected by that.

    :return: How many shared objects were opened
    """
    global _PRELOADED
    if _PRELOADED:
        return 0
    _PRELOADED = True

    opened = 0
    for package in CUDA_LIBRARY_PACKAGES:
        try:
            module = importlib.import_module(package)
        except ImportError:
            continue
        for directory in map(pathlib.Path, getattr(module, "__path__", ())):
            for library in sorted(directory.glob("*.so*")):
                try:
                    ctypes.CDLL(str(library), mode=ctypes.RTLD_GLOBAL)
                except OSError:
                    # One that will not open on its own is not necessarily one
                    # CTranslate2 wants; it asks for what it needs by name.
                    continue
                opened += 1
    if opened:
        logger.debug("preloaded %d CUDA shared object(s)", opened)
    return opened


def is_cuda_failure(exc: BaseException) -> bool:
    """
    Is this the CUDA runtime being unusable, rather than the recording being bad?

    :param exc: Whatever transcribing raised
    :return: Whether the CPU is worth trying instead

    >>> is_cuda_failure(RuntimeError("Library libcublas.so.12 is not found or cannot be loaded"))
    True
    >>> is_cuda_failure(RuntimeError("Invalid audio data"))
    False
    """
    message = str(exc).lower()
    return any(marker in message for marker in CUDA_FAILURES)


def _model(model_size: str, device: str):
    """
    The Whisper model, loaded once and kept.

    :param model_size: Which model to load
    :param device: ``cuda``, ``cpu`` or ``auto``
    :return: The loaded model
    :raises ImportError: If faster-whisper is not installed
    """
    # Imported here rather than at module scope. faster-whisper pulls CTranslate2
    # and onnxruntime in behind it, and this module is imported by every
    # ``tools/migrate.py`` run — including the ones that never reach this stage,
    # and every test collection. Deferring it keeps that cost on the runs that
    # actually transcribe.
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:  # pragma: no cover - a broken install, not a missing extra
        raise ImportError("transcribing needs faster-whisper: uv sync") from exc

    resolved = _AUTO_DEVICE if device == "auto" else device
    if resolved != "cpu":
        _preload_cuda_libraries()
    with _MODELS_LOCK:
        if (model_size, resolved) not in _MODELS:
            logger.info("loading Whisper %s on %s", model_size, resolved)
            compute_type = CPU_COMPUTE_TYPE if resolved == "cpu" else CUDA_COMPUTE_TYPE
            _MODELS[(model_size, resolved)] = WhisperModel(model_size, device=resolved,
                                                           compute_type=compute_type)
        return _MODELS[(model_size, resolved)]


def transcript_path(site_dir: pathlib.Path, num: str, vimeo_id: str) -> pathlib.Path:
    """
    Where one recording's transcript is kept.

    Under the site's own directory rather than beside the media, because the
    transcript outlives the media: the master is deleted at ``release`` and this
    file is what is left. Plain text rather than gzipped — it exists to be
    opened and read.

    :param site_dir: The site directory, ``sites/<site>``
    :param num: The board's record id
    :param vimeo_id: The source id, so the name says which recording it came from
    :return: The path, which may not exist

    >>> transcript_path(pathlib.Path("sites/example"), "12", "999888777666").name
    '12-999888777666.txt'
    """
    return site_dir / "transcript" / f"{num}-{vimeo_id}.txt"


def summary_path(site_dir: pathlib.Path, num: str) -> pathlib.Path:
    """
    Where one recording's summary is kept.

    Keyed by ``num`` alone: a summary belongs to the sermon, not to the copy of
    it that happened to be on Vimeo.

    :param site_dir: The site directory, ``sites/<site>``
    :param num: The board's record id
    :return: The path, which may not exist

    >>> summary_path(pathlib.Path("sites/example"), "12").name
    '12.md'
    """
    return site_dir / "summaries" / f"{num}.md"


def write_text(path: pathlib.Path, text: str) -> None:
    """
    Write a file whole, or not at all.

    Transcription runs for minutes and the run is expected to be interrupted. A
    half-written transcript that a later run reads back as complete is a
    silently truncated sermon, and nothing downstream could tell — so the file
    is built beside itself and moved into place, the move being atomic.

    :param path: Where the finished file goes
    :param text: Its contents
    :return: None
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".writing")
    partial.write_text(text, encoding="utf-8", newline="\n")
    partial.replace(path)


def whisper_language(language: str) -> str | None:
    """
    Which language to tell Whisper the recording is in.

    Taken from the export rather than from Whisper's own detector, which is
    unreliable on the first seconds of a service — an organ voluntary and a
    greeting are thin evidence, and a misdetected sermon transcribes as
    plausible nonsense in the wrong language.

    :param language: The language the scrape recorded, as ISO 639-2 (``kor``)
    :return: The ISO 639-1 tag Whisper wants, or None to let it detect

    >>> whisper_language("kor"), whisper_language("eng")
    ('ko', 'en')
    >>> whisper_language("") is None
    True
    """
    return LANGUAGE_TAGS.get(language.strip().lower())


def transcribe(media: pathlib.Path, language: str = "",
               model_size: str = DEFAULT_WHISPER_MODEL, device: str = "auto") -> str:
    """
    Read the speech out of a recording.

    faster-whisper decodes the container itself, so there is no ffmpeg step and
    the file passed here is the same one the upload sends.

    :param media: The recording to transcribe
    :param language: The language the scrape recorded, ``kor`` or ``eng``
    :param model_size: Which Whisper model to load; see
        :data:`DEFAULT_WHISPER_MODEL`
    :param device: ``cuda``, ``cpu``, or ``auto`` to let faster-whisper choose
    :return: The transcript, one segment per line
    :raises ImportError: If faster-whisper is not installed
    """
    try:
        return _read_out(_model(model_size, device), media, language)
    except RuntimeError as exc:
        # An explicit --whisper-device cuda is an instruction, not a preference:
        # falling back would hide the very thing that was asked about. Only
        # ``auto`` degrades, and only for a failure that is about the machine.
        if device != "auto" or _AUTO_DEVICE == "cpu" or not is_cuda_failure(exc):
            raise
        logger.warning("CUDA is unusable (%s) — transcribing on the CPU for the rest of "
                       "this run, which is a great deal slower", exc)
        _fall_back_to_cpu()
        return _read_out(_model(model_size, device), media, language)


def _fall_back_to_cpu() -> None:
    """
    Make ``auto`` mean the CPU from here on, and forget the model that failed.

    :return: None
    """
    global _AUTO_DEVICE
    with _MODELS_LOCK:
        _AUTO_DEVICE = "cpu"
        for key in [key for key in _MODELS if key[1] == "cuda"]:
            del _MODELS[key]


def _read_out(model, media: pathlib.Path, language: str) -> str:
    """
    Consume the segment generator, reporting as it goes.

    :param model: The loaded Whisper model
    :param media: The recording being transcribed, for the log
    :param language: The language the scrape recorded
    :return: The transcript, one segment per line
    """
    segments, info = model.transcribe(str(media), language=whisper_language(language))

    # ``segments`` is a generator and the work happens while it is consumed, so
    # this loop is the transcription rather than the call above it — which is
    # also why progress can be reported at all.
    #
    # Reported for the same reason an encode is: an hour-long sermon otherwise
    # says nothing for twenty-five minutes, and a stalled transcription looks
    # exactly like a slow one. It was mistaken for a hung run the first time it
    # ran for real.
    duration = getattr(info, "duration", 0.0) or 0.0
    ticker = Ticker()
    lines = []
    for segment in segments:
        if text := segment.text.strip():
            lines.append(text)
        if ticker.due():
            share = f" ({segment.end / duration:.0%})" if duration else ""
            logger.info("  transcribed %d:%02d of %d:%02d%s",
                        int(segment.end) // 60, int(segment.end) % 60,
                        int(duration) // 60, int(duration) % 60, share)
    return "\n".join(lines)


def transcribe_cached(media: pathlib.Path, destination: pathlib.Path, language: str = "",
                      model_size: str = DEFAULT_WHISPER_MODEL, device: str = "auto",
                      force: bool = False) -> tuple[str, bool]:
    """
    Transcribe a recording, unless it has been transcribed already.

    The cache is the file on disk, deliberately not the plan's ``summarized_at``
    stamp. Sending a row back with ``--redo-from summarize`` is for producing a
    better *summary*, which has to be cheap; six GPU-minutes to re-derive text
    that is already sitting in a file is the cost that split avoids.

    An empty result is a failure, not an answer — Whisper returns one for a
    silent or corrupt file — so it raises rather than caching the emptiness and
    making it permanent.

    :param media: The recording to transcribe
    :param destination: Where the transcript is kept; see :func:`transcript_path`
    :param language: The language the scrape recorded
    :param model_size: Which Whisper model to load
    :param device: ``cuda``, ``cpu`` or ``auto``
    :param force: Transcribe again even though a transcript exists
    :return: The transcript, and whether this call produced it
    :raises ValueError: If transcription produces nothing
    """
    if destination.exists() and not force:
        cached = destination.read_text(encoding="utf-8")
        if cached.strip():
            return cached, False
        # An empty file is a bug from before this check existed, or a truncated
        # write from before ``write_text`` did. Either way it is not a result.
        logger.info("%s is empty; transcribing again", destination)

    transcript = transcribe(media, language, model_size, device)
    if not transcript.strip():
        raise ValueError(f"transcribing {media.name} produced nothing — silent or unreadable audio?")
    write_text(destination, transcript)
    return transcript, True


def is_stub(summary: str) -> bool:
    """
    Is this summary file still holding a transcript rather than a summary?

    :param summary: The file's contents
    :return: Whether it carries :data:`STUB_MARKER`

    >>> is_stub(f"{STUB_MARKER}\\n\\n설교 제목")
    True
    >>> is_stub("설교 본문 요한복음")
    False
    >>> is_stub("")
    False
    """
    return STUB_MARKER in summary


def read_summary(path: pathlib.Path) -> str:
    """
    Read a summary that is fit to publish, or nothing.

    Missing and still-a-stub are the same answer to the only question the
    publishing side asks — is there a summary to put in the description? — and
    both mean the ``{summary}`` slot renders empty. Publishing a stub would put
    a 30,000-character transcript where three sentences belong.

    :param path: Where the summary is kept; see :func:`summary_path`
    :return: The summary, or empty where there is none to publish
    """
    if not path.exists():
        return ""
    summary = path.read_text(encoding="utf-8").strip()
    return "" if is_stub(summary) else summary


def summarize_stub(transcript: str) -> str:
    """
    Seed a summary file with the transcript, for somebody to finish by hand.

    Seeded rather than left empty so that the summary file is the one place to
    work: open it, read the sermon, write three sentences at the top, delete the
    marker line.

    :param transcript: The transcript to carry
    :return: The file's contents, marked as a stub

    >>> summarize_stub("설교 본문").splitlines()[0] == STUB_MARKER
    True
    """
    return f"{STUB_MARKER}\n\n{transcript.strip()}\n"


def summarize(transcript: str, backend: str = "stub") -> str:
    """
    Turn a transcript into what the summary file should hold.

    :param transcript: The transcript to summarize
    :param backend: Which backend to use; see :data:`BACKENDS`
    :return: The summary file's contents
    :raises ValueError: If the backend is not one of :data:`BACKENDS`
    :raises NotImplementedError: For a backend that is planned but not written

    >>> summarize("설교 본문", "stub").splitlines()[0] == STUB_MARKER
    True
    """
    if backend == "stub":
        return summarize_stub(transcript)
    if backend in ("api", "local"):
        raise NotImplementedError(
            f"the {backend!r} summarize backend is not written yet; "
            f"see docs/sermon-summaries.md — use --summarize-backend stub"
        )
    raise ValueError(f"unknown summarize backend {backend!r}, expected one of {', '.join(BACKENDS)}")
