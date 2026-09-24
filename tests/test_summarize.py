#!/usr/bin/env python3
"""
Tests for transcribing a recording and summarizing what it said.

Two things here are worth more than the rest. The transcript cache decides
whether six GPU-minutes are spent or a file is read, and it runs against
recordings whose media is deleted a stage later — so a cache that misses is
expensive and a cache that *wrongly hits* is a silently truncated sermon. And
the stub marker is the whole of the publishing rule: a summary file still
carrying it holds a transcript, and pushing that to YouTube would put tens of
thousands of characters where three sentences belong.
"""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from video_migrator.metadata.summarize import (  # noqa: E402
    STUB_MARKER,
    is_cuda_failure,
    is_stub,
    read_summary,
    summarize,
    summary_path,
    transcribe_cached,
    transcript_path,
    write_text,
)

#: A placeholder transcript. Korean prose over more than one line, which is the
#: shape that matters here; the vocabulary is the allowlist's, per CLAUDE.md.
SERMON = "설교 본문 요한복음 하나 둘\n설교 본문 요한복음 셋 넷"


@pytest.fixture
def recording(tmp_path) -> pathlib.Path:
    """
    A file standing in for a master, and somewhere to put its transcript.

    :param tmp_path: Fixture supplying a directory
    :return: The media file
    """
    media = tmp_path / "12-999888777666.mp4"
    media.write_bytes(b"a master")
    return media


def transcribing(counter: list[int], text: str = SERMON):
    """
    A stand-in for Whisper that counts how often it was asked to run.

    :param counter: Appended to on every call
    :param text: What to return as the transcript
    :return: A function with :func:`transcribe`'s signature
    """
    def fake(*_args, **_kwargs) -> str:
        counter.append(1)
        return text
    return fake


def test_a_transcript_already_on_disk_is_never_produced_again(recording, tmp_path, monkeypatch) -> None:
    """
    The cache is the file, and it is what makes --redo-from summarize cheap.

    Re-deriving text that is already sitting in a file costs six GPU-minutes and
    buys nothing, and after ``release`` has deleted the master it is not merely
    expensive but impossible.

    :param recording: Fixture supplying a master
    :param tmp_path: Fixture supplying a directory
    :param monkeypatch: Fixture for replacing the model
    """
    import video_migrator.metadata.summarize as summarize_module

    ran: list[int] = []
    monkeypatch.setattr(summarize_module, "transcribe", transcribing(ran))
    destination = transcript_path(tmp_path, "12", "999888777666")

    first, produced = transcribe_cached(recording, destination)
    assert produced and first == SERMON and len(ran) == 1

    second, produced_again = transcribe_cached(recording, destination)
    assert second == SERMON
    assert not produced_again, "the second call must read the file"
    assert len(ran) == 1, "no model may be loaded when a transcript is already there"


def test_retranscribing_is_the_one_way_past_the_cache(recording, tmp_path, monkeypatch) -> None:
    """
    A transcript known to be wrong has to be replaceable without deleting it by hand.

    :param recording: Fixture supplying a master
    :param tmp_path: Fixture supplying a directory
    :param monkeypatch: Fixture for replacing the model
    """
    import video_migrator.metadata.summarize as summarize_module

    ran: list[int] = []
    monkeypatch.setattr(summarize_module, "transcribe", transcribing(ran))
    destination = transcript_path(tmp_path, "12", "999888777666")

    transcribe_cached(recording, destination)
    monkeypatch.setattr(summarize_module, "transcribe", transcribing(ran, "고친 설교 본문"))
    again, produced = transcribe_cached(recording, destination, force=True)

    assert produced and again == "고친 설교 본문"
    assert destination.read_text(encoding="utf-8") == "고친 설교 본문"
    assert len(ran) == 2


def test_an_empty_transcript_is_a_failure_rather_than_a_cached_answer(recording, tmp_path,
                                                                     monkeypatch) -> None:
    """
    Whisper answers a silent or corrupt file with nothing at all.

    Cached, that emptiness becomes permanent: the row is stamped, the master is
    released, and the sermon is gone with no record that anything went wrong.

    :param recording: Fixture supplying a master
    :param tmp_path: Fixture supplying a directory
    :param monkeypatch: Fixture for replacing the model
    """
    import video_migrator.metadata.summarize as summarize_module

    monkeypatch.setattr(summarize_module, "transcribe", transcribing([], "   \n  "))
    destination = transcript_path(tmp_path, "12", "999888777666")

    with pytest.raises(ValueError, match="produced nothing"):
        transcribe_cached(recording, destination)
    assert not destination.exists(), "nothing may be left behind for a later run to trust"


def test_an_empty_file_left_by_an_older_run_is_not_trusted(recording, tmp_path, monkeypatch) -> None:
    """
    A zero-length transcript is a truncated write, not a recording with no words.

    :param recording: Fixture supplying a master
    :param tmp_path: Fixture supplying a directory
    :param monkeypatch: Fixture for replacing the model
    """
    import video_migrator.metadata.summarize as summarize_module

    ran: list[int] = []
    monkeypatch.setattr(summarize_module, "transcribe", transcribing(ran))
    destination = transcript_path(tmp_path, "12", "999888777666")
    write_text(destination, "")

    transcript, produced = transcribe_cached(recording, destination)
    assert produced and transcript == SERMON and len(ran) == 1


def test_a_transcript_is_written_whole_or_not_at_all(tmp_path) -> None:
    """
    Transcription runs for minutes and the run is expected to be interrupted.

    :param tmp_path: Fixture supplying a directory
    """
    destination = transcript_path(tmp_path, "12", "999888777666")
    write_text(destination, SERMON)

    assert destination.read_text(encoding="utf-8") == SERMON
    assert not list(destination.parent.glob("*.writing")), "no part-file may survive"
    # Written with LF on every platform, for the reason the plan and the ledger
    # are: this file is diffed and edited by hand.
    assert b"\r\n" not in destination.read_bytes()


def test_a_stub_carries_the_transcript_under_a_marker() -> None:
    """The summary file is where the work happens, so it starts with the sermon in it."""
    stub = summarize(SERMON, "stub")

    assert is_stub(stub)
    assert stub.splitlines()[0] == STUB_MARKER
    assert SERMON in stub, "the transcript is what somebody reads to write the summary"


def test_a_summary_still_carrying_its_marker_is_not_publishable(tmp_path) -> None:
    """
    Missing and still-a-stub are the same answer to the only question asked.

    Publishing a stub would put a whole transcript in the description, which
    YouTube refuses past 5,000 characters and which is wrong long before that.

    :param tmp_path: Fixture supplying a directory
    """
    path = summary_path(tmp_path, "12")
    assert read_summary(path) == "", "a summary that was never written is not one"

    write_text(path, summarize(SERMON, "stub"))
    assert read_summary(path) == "", "a stub is not a summary either"


def test_deleting_the_marker_by_hand_is_what_promotes_a_summary(tmp_path) -> None:
    """
    That is the whole editing workflow: read the sermon, write three sentences,
    delete the line.

    :param tmp_path: Fixture supplying a directory
    """
    path = summary_path(tmp_path, "12")
    write_text(path, summarize(SERMON, "stub"))

    finished = "고친 설교 본문 요한복음 다섯"
    write_text(path, finished + "\n")

    assert read_summary(path) == finished


def test_the_backends_that_are_not_written_yet_say_so() -> None:
    """
    A backend named in the plan but not yet built must not fail as though it ran.

    Silently producing an empty summary would stamp the row and publish the
    metadata-only description, and nothing would say the model never ran.
    """
    for backend in ("api", "local"):
        with pytest.raises(NotImplementedError, match=backend):
            summarize(SERMON, backend)

    with pytest.raises(ValueError, match="unknown summarize backend"):
        summarize(SERMON, "gpt")


def test_a_transcript_is_named_for_the_recording_it_came_from(tmp_path) -> None:
    """
    Both ids, because a summary belongs to the sermon and a transcript to the copy.

    :param tmp_path: Fixture supplying a directory
    """
    assert transcript_path(tmp_path, "12", "999888777666").name == "12-999888777666.txt"
    assert summary_path(tmp_path, "12").name == "12.md"
    # Outside data/, which is the site's regenerable scrape output. A transcript
    # is not regenerable once the master is released.
    assert transcript_path(tmp_path, "12", "999888777666").parent.name == "transcript"


def test_cuda_failing_is_told_apart_from_a_bad_recording() -> None:
    """
    One is a fallback, the other is a fault, and they arrive as the same type.

    CTranslate2 reports a missing CUDA runtime as a bare RuntimeError, so the
    message is the only thing separating "this machine cannot do CUDA" from
    "this recording is unreadable". Treating the second as the first would move
    a whole run onto the CPU because one file was broken.
    """
    assert is_cuda_failure(RuntimeError("Library libcublas.so.12 is not found or cannot be loaded"))
    assert is_cuda_failure(RuntimeError("Library libcudnn_ops.so.9 is not found"))
    assert not is_cuda_failure(RuntimeError("Invalid audio data"))
    assert not is_cuda_failure(FileNotFoundError("no such file"))


def test_preloading_the_cuda_runtime_is_harmless_where_there_is_none() -> None:
    """
    A machine with no NVIDIA GPU has no such wheels, and the CPU path must not
    care. The function reports how many it opened and raises nothing either way.
    """
    import video_migrator.metadata.summarize as summarize_module

    summarize_module._PRELOADED = False
    opened = summarize_module._preload_cuda_libraries()
    assert isinstance(opened, int) and opened >= 0
    # Once per process: the second call is a no-op rather than a second dlopen
    # of a gigabyte of shared objects.
    assert summarize_module._preload_cuda_libraries() == 0
