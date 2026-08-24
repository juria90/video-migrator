#!/usr/bin/env python3
"""
Tests for the driver that carries one recording through every stage.

The driver is the least tested and most consequential part of this project: a
fault in it costs hours of encoding before it shows. Two have already, and both
appeared only on the *second* recording of a run — which is exactly what a test
exercising a single recording cannot see.

Nothing here touches a network, YouTube or ffmpeg. Measuring is replaced on the
driver itself rather than by substituting the module it came from: a module
substituted in ``sys.modules`` only takes effect if this file is imported first,
which made these tests pass alone and fail beside their neighbours.
"""

import logging
import os
import pathlib
import signal
import subprocess
import sys
import threading
import time
import types

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "tools"))

import migrate  # noqa: E402

from video_migrator.plan import STAGES, read_plan, stage_of  # noqa: E402


@pytest.fixture(autouse=True)
def measurement(monkeypatch):
    """
    Replace measuring, which otherwise decodes a video with ffmpeg.

    :param monkeypatch: Fixture for replacing the driver's own references
    :return: None
    """
    monkeypatch.setattr(migrate, "probe", lambda *_a, **_k: (600.0, 30.0, (1280, 720)))
    monkeypatch.setattr(migrate, "motion_offsets", lambda *_a, **_k: [1.0, 2.0])
    monkeypatch.setattr(migrate, "measure_comb", lambda *_a, **_k: (4.0, 0.5, 2.0))


@pytest.fixture
def work(tmp_path) -> pathlib.Path:
    """
    A working directory holding one master.

    :param tmp_path: Fixture supplying a directory
    :return: The directory
    """
    (tmp_path / "12-345.mp4").write_bytes(b"a master")
    return tmp_path


def planned(**overrides) -> dict[str, str]:
    """
    A plan row for a recording partway through.

    :param overrides: Columns to set
    :return: The row
    """
    row = {"num": "12", "vimeo_id": "345", "title": "설교 제목", "path": "", "repair": "",
           "period": "", "prominence": "", "gib": "", "note": "", "youtube_id": ""}
    row.update({column: "" for _stage, column in STAGES if column not in row})
    row.update(overrides)
    return row


def test_a_repaired_file_keeps_an_extension_ffmpeg_understands() -> None:
    """
    The part-file is where an interrupted encode lands, and it must still say mp4.

    ``with_suffix`` replaces an extension rather than appending, which left
    ffmpeg a filename ending in ".part" and no way to guess a container from it.
    """
    finished, partial = migrate.repaired_paths(pathlib.Path("/w/12-345.mp4"))
    assert finished.name == "12-345.repaired.mp4"
    assert partial.name == "12-345.repaired.mp4.part"
    assert partial.name.endswith(".part")


def test_a_master_on_disk_is_recognised_whatever_the_plan_says(work) -> None:
    """
    A survey leaves gigabytes the plan knows nothing about.

    :param work: Fixture supplying a directory holding one master
    """
    assert migrate.already_fetched(planned(), work)
    assert not migrate.already_fetched(planned(num="13"), work)


def test_nothing_to_repair_means_no_encode_at_all(work) -> None:
    """
    Half these recordings need nothing, and skipping the encode is the point.

    A recording passed through untouched costs no CPU and loses no quality to a
    second generation, so "none" must not become an identity filter.

    :param work: Fixture supplying a directory holding one master
    """
    row = planned(path=str(work / "12-345.mp4"), repair="none")
    migrate.do_repair(row, crf=18)
    assert row["repaired_at"]
    # The path still names the master, so the master is what gets uploaded.
    assert row["path"].endswith("12-345.mp4")
    assert not list(work.glob("*.repaired.*"))


def test_a_failed_encode_leaves_nothing_that_looks_finished(work, monkeypatch) -> None:
    """
    A part-file that survived a failure would be uploaded as though it were whole.

    :param work: Fixture supplying a directory holding one master
    :param monkeypatch: Fixture for replacing the ffmpeg call
    """
    partial = work / "12-345.repaired.mp4.part"

    class FailingProcess:
        """An ffmpeg that writes something and then fails."""

        stdout = iter([])
        stderr = types.SimpleNamespace(read=lambda: "ffmpeg said no")
        returncode = 1

        def wait(self):
            """
            :return: A non-zero exit status
            """
            partial.write_bytes(b"half an encode")
            return 1

    monkeypatch.setattr(migrate.subprocess, "Popen", lambda *_a, **_k: FailingProcess())
    row = planned(path=str(work / "12-345.mp4"), repair="convolution=…")
    with pytest.raises(subprocess.CalledProcessError):
        migrate.do_repair(row, crf=18)
    assert not partial.exists()
    assert not (work / "12-345.repaired.mp4").exists()
    assert not row["repaired_at"]


def test_releasing_gives_back_the_master_and_the_repaired_copy(work) -> None:
    """
    Deleting is the last stage because it is only safe once the upload is known.

    :param work: Fixture supplying a directory holding one master
    """
    (work / "12-345.repaired.mp4").write_bytes(b"repaired")
    (work / "12-345.upload-session").write_text("a session")
    row = planned(path=str(work / "12-345.repaired.mp4"), youtube_id="aBcDeFgHiJk")
    migrate.do_release(row, keep=False)
    assert row["released_at"]
    assert not list(work.glob("12-345*"))


def test_keeping_leaves_the_files_where_they_are(work) -> None:
    """
    A single run may want to look at what it produced.

    :param work: Fixture supplying a directory holding one master
    """
    row = planned(path=str(work / "12-345.mp4"))
    migrate.do_release(row, keep=True)
    assert row["released_at"]
    assert (work / "12-345.mp4").exists()


def test_measuring_records_what_it_found_and_what_follows_from_it(work) -> None:
    """
    The plan carries the measurement, so a decision can be reviewed afterwards.

    :param work: Fixture supplying a directory holding one master
    """
    row = planned(path=str(work / "12-345.mp4"), fetched_at="2026-08-23 09:00")
    migrate.do_measure(row)
    assert row["period"] == "4"
    assert "50%" in row["prominence"]
    assert row["repair"].startswith("convolution=")
    assert stage_of(row) == "repair"


@pytest.fixture
def board(tmp_path) -> pathlib.Path:
    """
    An export naming two recordings by two preachers.

    :param tmp_path: Fixture supplying a directory
    :return: The export
    """
    path = tmp_path / "board.tsv"
    header = ("num\tType\tID\tURL\tEmbed URL\tTitle\tBible Verse\t"
              "Publish Date\tYear\tPreacher\tGenre\tLanguage")
    rows = [
        "12\tvimeo\t345\t\t\t설교 제목 하나\t요한복음 3:16\t2026-08-02\t2026\t홍길동 목사\tSermon\tkor",
        "13\tvimeo\t346\t\t\t설교 제목 둘\t창세기 1:1\t2026-08-09\t2026\t김영희 목사\tSermon\teng",
        "14\tyoutube\t347\t\t\t설교 제목 셋\t\t2026-08-16\t2026\t홍길동 목사\tSermon\tkor",
    ]
    path.write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")
    return path


def test_the_export_becomes_titles_ready_to_publish(board) -> None:
    """
    Each recording carries the title it will go up under, formatted once here.

    :param board: Fixture supplying an export
    """
    wanted = migrate.wanted_from(board, None, "example", "sunday_sermon")
    assert [entry["num"] for entry in wanted] == ["12", "13"]      # the YouTube row is not ours to migrate
    assert wanted[0]["title"].startswith("2026.8.2")
    assert wanted[0]["language"] == "kor"
    assert wanted[1]["verse"] == "창세기 1:1"


def test_a_preacher_pattern_narrows_what_the_driver_will_carry(board) -> None:
    """
    The two halves of a board go to two channels, and one run carries one half.

    :param board: Fixture supplying an export
    """
    assert [e["num"] for e in migrate.wanted_from(board, ["홍길동 목사"], "example", "sunday_sermon")] == ["12"]
    assert [e["num"] for e in migrate.wanted_from(board, ["!홍길동 목사"], "example", "sunday_sermon")] == ["13"]
    assert [e["num"] for e in migrate.wanted_from(board, ["홍길동*"], "example", "sunday_sermon")] == ["12"]


def test_a_run_carrying_two_recordings_completes_both(board, tmp_path, monkeypatch) -> None:
    """
    Two bugs have hidden here, and both appeared only on the *second* recording.

    A parser built by mutating a shared object, and an upload session recorded
    only on success: each worked once and failed thereafter, so a run limited to
    one recording proved nothing. This is that run, twice over.

    :param board: Fixture supplying an export
    :param tmp_path: Fixture supplying a directory
    :param monkeypatch: Fixture for replacing everything outside the process
    """
    work = tmp_path / "work"
    work.mkdir()
    uploaded = []

    def fake_download(_self, video_id, destination):
        destination.write_bytes(b"a master for " + video_id.encode())

    def fake_upload(options):
        uploaded.append(options.file)
        return f"video{len(uploaded)}"

    monkeypatch.setattr(migrate.VimeoAPI, "__init__", lambda self, _token: None)
    monkeypatch.setattr(migrate.VimeoAPI, "download", fake_download)
    monkeypatch.setattr(migrate, "load_token", lambda *_a, **_k: "a token")
    monkeypatch.setattr(migrate, "upload", fake_upload)
    monkeypatch.setattr(migrate, "confirm_upload", lambda *_a, **_k: ("processed", ""))
    monkeypatch.setattr(migrate, "get_authenticated_service", lambda _options: None)
    # Nothing needs repairing, so no encoder is involved.
    monkeypatch.setattr(migrate, "repair_filter", lambda *_a, **_k: None)
    monkeypatch.setattr(sys, "argv", [
        "migrate.py", "--board", str(board), "--profile", "example",
        "--work-dir", str(work), "--limit", "2", "--plan", str(tmp_path / "plan.tsv")])

    assert migrate.main() == 0
    assert len(uploaded) == 2, "the second recording must upload too"

    plan = read_plan(tmp_path / "plan.tsv")
    assert [stage_of(row) for row in plan] == ["done", "done"]
    assert [row["youtube_id"] for row in plan] == ["video1", "video2"]
    assert not [row["note"] for row in plan if row["note"]]
    # Released, so nothing is left behind holding disk.
    assert not list(work.glob("*.mp4"))


def test_a_stage_that_fails_leaves_the_recording_outstanding(board, tmp_path, monkeypatch) -> None:
    """
    A failure must record why and stop, not stamp a success and move on.

    The recording stays where it stopped so the next run resumes there, and the
    note says what happened rather than leaving a silent gap.

    :param board: Fixture supplying an export
    :param tmp_path: Fixture supplying a directory
    :param monkeypatch: Fixture for replacing everything outside the process
    """
    work = tmp_path / "work"
    work.mkdir()

    def refusing_upload(_options):
        raise ValueError("YouTube refused it")

    monkeypatch.setattr(migrate.VimeoAPI, "__init__", lambda self, _token: None)
    monkeypatch.setattr(migrate.VimeoAPI, "download",
                        lambda _self, _id, destination: destination.write_bytes(b"a master"))
    monkeypatch.setattr(migrate, "load_token", lambda *_a, **_k: "a token")
    monkeypatch.setattr(migrate, "upload", refusing_upload)
    monkeypatch.setattr(migrate, "repair_filter", lambda *_a, **_k: None)
    monkeypatch.setattr(sys, "argv", [
        "migrate.py", "--board", str(board), "--profile", "example",
        "--work-dir", str(work), "--limit", "1", "--plan", str(tmp_path / "plan.tsv")])

    assert migrate.main() == 1

    row = read_plan(tmp_path / "plan.tsv")[0]
    assert stage_of(row) == "upload"
    assert "YouTube refused it" in row["note"]
    # The master is still there, so resuming costs no download.
    assert (work / "12-345.mp4").exists()


def test_an_upload_that_cannot_be_confirmed_is_still_an_upload(board, tmp_path, monkeypatch) -> None:
    """
    Failing to *ask* what became of a video is not the video failing.

    The network went down between an upload finishing and the check that
    follows it. The video was on YouTube and its id was written down, but the
    stage was recorded as failed — so the next run would have uploaded a second
    copy of a recording that was already published.

    :param board: Fixture supplying an export
    :param tmp_path: Fixture supplying a directory
    :param monkeypatch: Fixture for replacing everything outside the process
    """
    work = tmp_path / "work"
    work.mkdir()

    def unreachable(*_args, **_kwargs):
        raise OSError(113, "No route to host")

    monkeypatch.setattr(migrate.VimeoAPI, "__init__", lambda self, _token: None)
    monkeypatch.setattr(migrate.VimeoAPI, "download",
                        lambda _self, _id, destination: destination.write_bytes(b"a master"))
    monkeypatch.setattr(migrate, "load_token", lambda *_a, **_k: "a token")
    monkeypatch.setattr(migrate, "upload", lambda _options: "aBcDeFgHiJk")
    monkeypatch.setattr(migrate, "get_authenticated_service", unreachable)
    monkeypatch.setattr(migrate, "repair_filter", lambda *_a, **_k: None)
    monkeypatch.setattr(sys, "argv", [
        "migrate.py", "--board", str(board), "--profile", "example",
        "--work-dir", str(work), "--limit", "1", "--plan", str(tmp_path / "plan.tsv")])

    assert migrate.main() == 0

    row = read_plan(tmp_path / "plan.tsv")[0]
    assert row["youtube_id"] == "aBcDeFgHiJk"
    assert row["uploaded_at"], "the recording is published, whatever the check could not tell us"
    assert stage_of(row) == "done"
    assert "confirming it failed" in row["note"]


def test_repairing_leaves_the_master_addressable(work) -> None:
    """
    The path names the master throughout, so the stage can be run again.

    Pointing it at the repaired file meant that sending a recording back to
    ``repair`` left it addressing output that stage had not produced yet — and
    once that output had been deleted, addressing nothing at all, so the rerun
    failed before it began.

    :param work: Fixture supplying a directory holding one master
    """
    master = work / "12-345.mp4"
    row = planned(path=str(master), repair="none", fetched_at="x", measured_at="x")
    migrate.do_repair(row, crf=18)
    assert row["path"] == str(master)


def test_the_repaired_file_is_what_gets_uploaded_when_there_is_one(work, monkeypatch) -> None:
    """
    Which file is sent follows from what is on disk, not from what the plan says.

    A repair rerun after the plan was written still has to be picked up, and the
    plan cannot know about it.

    :param work: Fixture supplying a directory holding one master
    :param monkeypatch: Fixture for capturing what would be uploaded
    """
    sent = []
    monkeypatch.setattr(migrate, "upload", lambda options: sent.append(options.file) or "aBcDeFgHiJk")
    monkeypatch.setattr(migrate, "confirm_upload", lambda *_a, **_k: ("processed", ""))
    monkeypatch.setattr(migrate, "get_authenticated_service", lambda _options: None)
    options = types.SimpleNamespace(privacy="unlisted", category=29, channel="", language="",
                                    made_for_kids="no", embeddable="yes", no_confirm=False)

    row = planned(path=str(work / "12-345.mp4"))
    migrate.do_upload(row, {}, options)
    assert sent[-1].endswith("12-345.mp4"), "no repaired file, so the master goes"

    (work / "12-345.repaired.mp4").write_bytes(b"repaired")
    migrate.do_upload(row, {}, options)
    assert sent[-1].endswith("12-345.repaired.mp4"), "a repaired file is preferred once it exists"


@pytest.fixture
def busy_board(tmp_path) -> pathlib.Path:
    """
    An export naming four recordings by one preacher, to have several in flight.

    :param tmp_path: Fixture supplying a directory
    :return: The export
    """
    path = tmp_path / "busy.tsv"
    header = ("num\tType\tID\tURL\tEmbed URL\tTitle\tBible Verse\t"
              "Publish Date\tYear\tPreacher\tGenre\tLanguage")
    rows = [f"{10 + n}\tvimeo\t{400 + n}\t\t\t설교 제목 {word}\t요한복음 3:16\t"
            f"2026-08-0{n + 1}\t2026\t홍길동 목사\tSermon\tkor"
            for n, word in enumerate(["하나", "둘", "셋", "넷"])]
    path.write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")
    return path


class Trace:
    """What ran when, so that overlap between stages can be asserted on."""

    def __init__(self) -> None:
        """Start an empty trace."""
        self.spans: list[tuple[str, str, float, float]] = []
        self.lock = threading.Lock()

    def record(self, stage: str, num: str, began: float) -> None:
        """
        Note that a stage has just finished.

        :param stage: Which stage ran
        :param num: The recording it ran for
        :param began: When it started, from :func:`time.monotonic`
        :return: None
        """
        with self.lock:
            self.spans.append((stage, num, began, time.monotonic()))

    def peak(self, stage: str) -> int:
        """
        The most copies of one stage that ever ran at the same moment.

        :param stage: Which stage to count
        :return: The highest number running together
        """
        edges = [(begin, 1) for name, _, begin, _ in self.spans if name == stage]
        edges += [(end, -1) for name, _, _, end in self.spans if name == stage]
        running = most = 0
        for _, step in sorted(edges):
            running += step
            most = max(most, running)
        return most

    def overlapped(self, one: str, other: str) -> bool:
        """
        Did these two stages ever run at the same moment, on different recordings?

        :param one: A stage
        :param other: Another stage
        :return: Whether their spans ever intersected
        """
        return any(a_begin < b_end and b_begin < a_end
                   for a, a_num, a_begin, a_end in self.spans if a == one
                   for b, b_num, b_begin, b_end in self.spans
                   if b == other and a_num != b_num)


@pytest.fixture
def traced(monkeypatch):
    """
    Replace every stage with one that stamps the row, takes time and is noted.

    Repair is made the slow stage, as it is in life, so that a recording held at
    the encoder is what the others have the chance to overlap.

    :param monkeypatch: Fixture for replacing the driver's own references
    :return: The trace the stages write to
    """
    trace = Trace()

    def stamp(_row: dict) -> str:
        """
        Stamp a column with the time, ignoring the row it is stamping.

        :param _row: The row being stamped
        :return: The timestamp
        """
        return migrate.now()

    def stage(name: str, pause: float, **stamps):
        def run(row, *_args, **_kwargs):
            began = time.monotonic()
            time.sleep(pause)
            row.update({column: value(row) if callable(value) else value
                        for column, value in stamps.items()})
            trace.record(name, row["num"], began)
        return run

    monkeypatch.setattr(migrate.VimeoAPI, "__init__", lambda self, _token: None)
    monkeypatch.setattr(migrate, "load_token", lambda *_a, **_k: "a token")
    monkeypatch.setattr(migrate, "do_fetch", lambda _api, row, _work: stage(
        "fetch", 0.02, fetched_at=stamp, path="/nowhere.mp4", gib="1.0")(row))
    monkeypatch.setattr(migrate, "do_measure", stage(
        "measure", 0.02, measured_at=stamp, period="", prominence="", repair="none"))
    monkeypatch.setattr(migrate, "do_repair", stage("repair", 0.20, repaired_at=stamp))
    monkeypatch.setattr(migrate, "do_upload", stage(
        "upload", 0.05, uploaded_at=stamp, youtube_id=lambda row: f"video{row['num']}"))
    monkeypatch.setattr(migrate, "do_release", stage("release", 0.0, released_at=stamp))
    return trace


def run_driver(board: pathlib.Path, tmp_path: pathlib.Path, monkeypatch, *extra: str) -> pathlib.Path:
    """
    Drive the whole board through, and say where the plan was written.

    :param board: An export to migrate
    :param tmp_path: Fixture-supplied directory for the plan and the work dir
    :param monkeypatch: Fixture for setting the command line
    :param extra: Further command line arguments
    :return: The plan the run wrote
    """
    plan_path = tmp_path / "plan.tsv"
    monkeypatch.setattr(sys, "argv", [
        "migrate.py", "--board", str(board), "--profile", "example",
        "--work-dir", str(tmp_path / "work"), "--plan", str(plan_path), *extra])
    migrate.main()
    return plan_path


def test_several_recordings_in_flight_together_all_finish(busy_board, tmp_path, monkeypatch, traced) -> None:
    """
    Overlapping the stages must not lose a recording or a stamp.

    :param busy_board: Fixture supplying an export of four
    :param tmp_path: Fixture supplying a directory
    :param monkeypatch: Fixture for setting the command line
    :param traced: Fixture replacing the stages and recording them
    """
    plan_path = run_driver(busy_board, tmp_path, monkeypatch, "--limit", "4", "--jobs", "3")
    plan = read_plan(plan_path)
    assert [stage_of(row) for row in plan] == ["done"] * 4
    assert sorted(row["youtube_id"] for row in plan) == ["video10", "video11", "video12", "video13"]
    assert not [row["note"] for row in plan if row["note"]]


def test_two_recordings_never_encode_at_the_same_time(busy_board, tmp_path, monkeypatch, traced) -> None:
    """
    Two encodes would only halve each other; the gate is what stops them.

    :param busy_board: Fixture supplying an export of four
    :param tmp_path: Fixture supplying a directory
    :param monkeypatch: Fixture for setting the command line
    :param traced: Fixture replacing the stages and recording them
    """
    run_driver(busy_board, tmp_path, monkeypatch, "--limit", "4", "--jobs", "4")
    assert traced.peak("repair") == 1
    assert traced.peak("fetch") == 1, "nor should two recordings share the link"


def test_an_encode_runs_while_another_recording_is_transferred(busy_board, tmp_path,
                                                               monkeypatch, traced) -> None:
    """
    This overlap is the whole reason for having several in flight.

    Encoding takes the processor and transferring takes the link, so a run that
    does them one after the other leaves one of the two idle throughout. If this
    fails, ``--jobs`` is buying nothing.

    :param busy_board: Fixture supplying an export of four
    :param tmp_path: Fixture supplying a directory
    :param monkeypatch: Fixture for setting the command line
    :param traced: Fixture replacing the stages and recording them
    """
    run_driver(busy_board, tmp_path, monkeypatch, "--limit", "4", "--jobs", "3")
    assert traced.overlapped("repair", "fetch") or traced.overlapped("repair", "upload")


def test_one_recording_in_flight_is_the_old_behaviour(busy_board, tmp_path, monkeypatch, traced) -> None:
    """
    The default must leave a run doing exactly what it did before.

    :param busy_board: Fixture supplying an export of four
    :param tmp_path: Fixture supplying a directory
    :param monkeypatch: Fixture for setting the command line
    :param traced: Fixture replacing the stages and recording them
    """
    run_driver(busy_board, tmp_path, monkeypatch, "--limit", "4")
    assert not traced.overlapped("repair", "fetch")
    assert not traced.overlapped("repair", "upload")


def test_a_recording_that_fails_does_not_stop_the_ones_beside_it(busy_board, tmp_path,
                                                                 monkeypatch, traced) -> None:
    """
    A run of several hundred cannot end on the first recording Vimeo refuses.

    :param busy_board: Fixture supplying an export of four
    :param tmp_path: Fixture supplying a directory
    :param monkeypatch: Fixture for setting the command line
    :param traced: Fixture replacing the stages and recording them
    """
    def refuse_one(_api, row, _work):
        if row["num"] == "11":
            raise OSError("no route to host")
        row.update(fetched_at=migrate.now(), path="/nowhere.mp4", gib="1.0")

    monkeypatch.setattr(migrate, "do_fetch", refuse_one)
    plan_path = run_driver(busy_board, tmp_path, monkeypatch, "--limit", "4", "--jobs", "3")

    plan = {row["num"]: row for row in read_plan(plan_path)}
    assert [stage_of(plan[num]) for num in ("10", "12", "13")] == ["done"] * 3
    assert stage_of(plan["11"]) == "fetch"
    assert "no route to host" in plan["11"]["note"]


def test_interrupting_a_batch_drops_what_it_had_not_begun(busy_board, tmp_path, monkeypatch, traced) -> None:
    """
    ^C on a run of thirty must not mean waiting out the rest of the day.

    Every recording is handed to the pool up front, and shutting a pool down
    waits for its queue unless the queue is cancelled — so this is the
    difference between stopping a batch and merely asking it to stop.

    :param busy_board: Fixture supplying an export of four
    :param tmp_path: Fixture supplying a directory
    :param monkeypatch: Fixture for setting the command line
    :param traced: Fixture replacing the stages and recording them
    """
    begun = []

    def interrupt_the_first(_api, row, _work):
        begun.append(row["num"])
        if row["num"] == "10":
            raise KeyboardInterrupt
        time.sleep(0.05)
        row.update(fetched_at=migrate.now(), path="/nowhere.mp4", gib="1.0")

    monkeypatch.setattr(migrate, "do_fetch", interrupt_the_first)
    plan_path = run_driver(busy_board, tmp_path, monkeypatch, "--limit", "4")

    assert "13" not in begun, "the last recording should never have been started"
    plan = {row["num"]: row for row in read_plan(plan_path)}
    assert stage_of(plan["13"]) == "fetch", "and so it is still waiting, not failed"


def test_what_finished_before_an_interruption_is_kept(busy_board, tmp_path, monkeypatch, traced) -> None:
    """
    Stopping a run costs the stage in progress, never a stage that finished.

    :param busy_board: Fixture supplying an export of four
    :param tmp_path: Fixture supplying a directory
    :param monkeypatch: Fixture for setting the command line
    :param traced: Fixture replacing the stages and recording them
    """
    def interrupt_the_second_encode(row, *_args, **_kwargs):
        if row["num"] == "11":
            raise KeyboardInterrupt
        row["repaired_at"] = migrate.now()

    monkeypatch.setattr(migrate, "do_repair", interrupt_the_second_encode)
    plan_path = run_driver(busy_board, tmp_path, monkeypatch, "--limit", "4")

    plan = {row["num"]: row for row in read_plan(plan_path)}
    # The first got the whole way; the interrupted one keeps the stages it had.
    assert stage_of(plan["10"]) == "done"
    assert plan["11"]["fetched_at"] and plan["11"]["measured_at"]
    assert stage_of(plan["11"]) == "repair"


def test_a_recording_told_to_stop_begins_no_further_stage(work, traced) -> None:
    """
    Stopping is checked between stages, so nothing is abandoned half done.

    A stage that finishes stamps the plan and need never be repeated; a stage
    killed part way through — an encode above all, which cannot be resumed —
    costs everything it had done. So an interrupted run finishes what it is
    doing and starts nothing more.

    :param work: Fixture supplying a working directory
    :param traced: Fixture replacing the stages and recording them
    """
    row = planned(num="10", vimeo_id="400")
    stopping = threading.Event()
    stopping.set()
    args = types.SimpleNamespace(stop_before=None, crf=18, preset="fast", no_repair=False, keep=True)

    advanced = migrate.advance(row, {}, args, None, [row], work / "plan.tsv",
                               {}, threading.Lock(), stopping)

    assert advanced, "stopping is not a failure"
    assert stage_of(row) == "fetch", "and the recording is left exactly where it was"
    assert not traced.spans, "no stage should have run at all"


def test_a_termination_signal_stops_the_run_without_killing_a_stage(busy_board, tmp_path,
                                                                    monkeypatch, traced) -> None:
    """
    ``kill <pid>`` asks for a tidy stop, where ^C asks for an immediate one.

    An encode cannot be resumed, so the tidy stop is worth having: the stage
    running when the signal arrives finishes and stamps the plan, and only then
    does the run end.

    :param busy_board: Fixture supplying an export of four
    :param tmp_path: Fixture supplying a directory
    :param monkeypatch: Fixture for setting the command line
    :param traced: Fixture replacing the stages and recording them
    """
    def ask_to_stop_during_the_first_encode(row, *_args, **_kwargs):
        if row["num"] == "10":
            os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(0.05)
        row["repaired_at"] = migrate.now()

    monkeypatch.setattr(migrate, "do_repair", ask_to_stop_during_the_first_encode)
    plan_path = run_driver(busy_board, tmp_path, monkeypatch, "--limit", "4", "--jobs", "2")

    plan = {row["num"]: row for row in read_plan(plan_path)}
    # The encode the signal landed in was not thrown away — it finished and
    # stamped the plan, which is the whole point of stopping this way.
    assert plan["10"]["repaired_at"], "the stage in progress should have finished"
    # It stops after that stage rather than carrying the recording to the end:
    # every stage boundary is resumable, so there is nothing to gain by going on.
    assert stage_of(plan["10"]) == "upload"
    # And the recordings behind it were not worked through.
    assert [stage_of(plan[num]) for num in ("12", "13")] != ["done", "done"]


def test_stopping_a_run_ends_the_encode_it_was_running() -> None:
    """
    ^C must reach ffmpeg itself, not rely on the shell having reached it.

    A run started under ``nohup`` or in a detached ``tmux`` is not in the
    terminal's process group, so nothing else ends the encode — and waiting for
    one to finish is twenty minutes of a run that has been told to stop.
    """
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        with migrate.ENCODING_LOCK:
            migrate.ENCODING.add(process)

        assert migrate.stop_encoding() == 1
        assert process.wait(timeout=10) != 0, "the encode should have been ended, not left running"
    finally:
        with migrate.ENCODING_LOCK:
            migrate.ENCODING.discard(process)
        if process.poll() is None:
            process.kill()


def test_an_encode_that_finishes_is_no_longer_stoppable(work, monkeypatch) -> None:
    """
    The register must empty as encodes finish, or it grows for the whole run.

    :param work: Fixture supplying a directory holding one master
    :param monkeypatch: Fixture for replacing the ffmpeg call
    """
    class QuietProcess:
        """An ffmpeg that says nothing and succeeds."""

        stdout = iter([])
        stderr = types.SimpleNamespace(read=lambda: "")
        returncode = 0

        def wait(self):
            """
            :return: A successful exit status
            """
            (work / "12-345.repaired.mp4.part").write_bytes(b"an encode")
            return 0

    monkeypatch.setattr(migrate.subprocess, "Popen", lambda *_a, **_k: QuietProcess())
    row = planned(path=str(work / "12-345.mp4"), repair="convolution=…")
    migrate.do_repair(row, crf=18)

    assert row["repaired_at"]
    assert not migrate.ENCODING, "a finished encode must not stay in the register"


def test_an_encode_that_fails_is_no_longer_stoppable(work, monkeypatch) -> None:
    """
    Nor may a failure leave one behind, which a plain remove-at-the-end would.

    :param work: Fixture supplying a directory holding one master
    :param monkeypatch: Fixture for replacing the ffmpeg call
    """
    class FailingProcess:
        """An ffmpeg that fails."""

        stdout = iter([])
        stderr = types.SimpleNamespace(read=lambda: "ffmpeg said no")
        returncode = 1

        def wait(self):
            """
            :return: A non-zero exit status
            """
            return 1

    monkeypatch.setattr(migrate.subprocess, "Popen", lambda *_a, **_k: FailingProcess())
    row = planned(path=str(work / "12-345.mp4"), repair="convolution=…")
    with pytest.raises(subprocess.CalledProcessError):
        migrate.do_repair(row, crf=18)
    assert not migrate.ENCODING


def test_a_stage_ended_by_stopping_is_not_recorded_as_a_fault(work, traced, monkeypatch) -> None:
    """
    We killed the encode, so the recording is not the thing that went wrong.

    A note saying ``repair: CalledProcessError`` would be read later as a
    recording that cannot be encoded, and somebody would go looking for a fault
    in it. The plan already shows the stage unfinished, which is the whole truth.

    :param work: Fixture supplying a working directory
    :param traced: Fixture replacing the stages
    :param monkeypatch: Fixture for replacing the repair
    """
    stopping = threading.Event()

    def killed_on_the_way_out(row, *_args, **_kwargs):
        stopping.set()
        raise subprocess.CalledProcessError(-15, "ffmpeg")

    monkeypatch.setattr(migrate, "do_repair", killed_on_the_way_out)
    row = planned(num="10", fetched_at="2026-08-23 15:00", measured_at="2026-08-23 15:01",
                  path=str(work / "10-400.mp4"), repair="convolution=…")
    args = types.SimpleNamespace(stop_before=None, crf=18, preset="fast", no_repair=False, keep=True)

    advanced = migrate.advance(row, {}, args, None, [row], work / "plan.tsv",
                               {}, threading.Lock(), stopping)

    assert advanced, "being stopped is not a failure"
    assert not row["note"], f"nothing should be blamed on the recording, got {row['note']!r}"
    assert stage_of(row) == "repair", "and it is simply still waiting to be repaired"


def test_a_stage_that_genuinely_fails_still_says_so(work, traced, monkeypatch) -> None:
    """
    The quiet path must not swallow a real fault, which is what it sits beside.

    :param work: Fixture supplying a working directory
    :param traced: Fixture replacing the stages
    :param monkeypatch: Fixture for replacing the repair
    """
    def broken(row, *_args, **_kwargs):
        raise subprocess.CalledProcessError(1, "ffmpeg")

    monkeypatch.setattr(migrate, "do_repair", broken)
    row = planned(num="10", fetched_at="2026-08-23 15:00", measured_at="2026-08-23 15:01",
                  path=str(work / "10-400.mp4"), repair="convolution=…")
    args = types.SimpleNamespace(stop_before=None, crf=18, preset="fast", no_repair=False, keep=True)

    advanced = migrate.advance(row, {}, args, None, [row], work / "plan.tsv",
                               {}, threading.Lock(), threading.Event())

    assert not advanced
    assert "CalledProcessError" in row["note"]


def test_a_recording_queued_behind_another_says_so(caplog) -> None:
    """
    Otherwise a log of three recordings starting reads as three running.

    :param caplog: Fixture capturing what the run reported
    """
    gate = threading.Semaphore(1)
    gate.acquire()                                        # somebody else is encoding

    released = threading.Event()
    threading.Timer(0.05, lambda: (gate.release(), released.set())).start()

    with caplog.at_level(logging.INFO, logger="migrate"), migrate.held(gate, "844", "repair"):
        pass

    assert released.is_set(), "it should have waited for the gate, not skipped it"
    assert "num=844 waiting to repair" in caplog.text


def test_a_recording_that_waits_for_nothing_stays_quiet(caplog) -> None:
    """
    The message is for queueing; an unheld gate is the ordinary case.

    :param caplog: Fixture capturing what the run reported
    """
    with caplog.at_level(logging.INFO, logger="migrate"):
        with migrate.held(threading.Semaphore(1), "844", "repair"):
            pass
        with migrate.held(None, "844", "release"):
            pass

    assert "waiting" not in caplog.text


def test_a_gate_is_given_back_even_when_a_stage_fails() -> None:
    """
    A gate held by a failed stage would stop the whole run at that resource.

    :return: None
    """
    gate = threading.Semaphore(1)
    with pytest.raises(ValueError), migrate.held(gate, "844", "repair"):
        raise ValueError("the encode failed")
    assert gate.acquire(blocking=False), "the gate must have been released"


def test_a_recording_queued_for_the_encoder_starts_nothing_once_stopped(work, traced) -> None:
    """
    The interruption almost always lands while something is queued.

    Waiting for the encoder takes as long as an encode, so that wait is where a
    run spends most of its time — and a recording that asked whether to stop
    *before* queueing has long since had its answer. This is the case that got
    through: ^C printed that it had stopped, the stage in front finished and
    released the gate, and the recording behind it began regardless.

    :param work: Fixture supplying a working directory
    :param traced: Fixture replacing the stages and recording them
    """
    gate = threading.Semaphore(1)
    gate.acquire()                                        # the recording in front holds it
    stopping = threading.Event()

    # The interruption arrives while this recording is queued, and only then
    # does the recording in front finish and hand the gate over.
    threading.Timer(0.05, lambda: (stopping.set(), gate.release())).start()

    row = planned(num="844", fetched_at="2026-08-23 15:00", path=str(work / "844-400.mp4"))
    args = types.SimpleNamespace(stop_before=None, crf=18, preset="fast", no_repair=False, keep=True)

    advanced = migrate.advance(row, {}, args, None, [row], work / "plan.tsv",
                               {"cpu": gate}, threading.Lock(), stopping)

    assert advanced, "being stopped is not a failure"
    assert not traced.spans, f"no stage should have run, but {[s[0] for s in traced.spans]} did"
    assert stage_of(row) == "measure", "and it is left exactly where it was queued"


def test_the_daily_upload_limit_stops_the_whole_run(busy_board, tmp_path, monkeypatch, traced) -> None:
    """
    One refusal means every recording behind it would be refused too.

    Left to carry on, the run fetches and encodes each of the six hundred
    remaining recordings — twenty minutes apiece — to reach the same answer.
    This is the difference between losing a minute and losing a night.

    :param busy_board: Fixture supplying an export of four
    :param tmp_path: Fixture supplying a directory
    :param monkeypatch: Fixture for setting the command line
    :param traced: Fixture replacing the stages and recording them
    """
    def refuse_every_upload(row, *_args, **_kwargs):
        raise RuntimeError('HttpError 400 ... "reason": "uploadLimitExceeded"')

    monkeypatch.setattr(migrate, "do_upload", refuse_every_upload)
    plan_path = run_driver(busy_board, tmp_path, monkeypatch, "--limit", "4", "--jobs", "1")

    plan = {row["num"]: row for row in read_plan(plan_path)}
    refused = [num for num, row in plan.items() if row["note"]]
    assert len(refused) == 1, f"only the first should have tried, but {refused} did"
    assert "no more videos today" in plan[refused[0]]["note"]
    # And the rest were not fetched or encoded on the way to the same refusal.
    assert traced.peak("repair") <= 1
    assert len([span for span in traced.spans if span[0] == "repair"]) == 1


def test_work_already_done_survives_the_daily_limit(busy_board, tmp_path, monkeypatch, traced) -> None:
    """
    The recording that was refused keeps its fetch, its measurement and its encode.

    Tomorrow's run has to upload it, not produce it again — which is twenty
    minutes of encoding per recording that must not be thrown away.

    :param busy_board: Fixture supplying an export of four
    :param tmp_path: Fixture supplying a directory
    :param monkeypatch: Fixture for setting the command line
    :param traced: Fixture replacing the stages and recording them
    """
    def refuse_every_upload(row, *_args, **_kwargs):
        raise RuntimeError('HttpError 400 ... "reason": "uploadLimitExceeded"')

    monkeypatch.setattr(migrate, "do_upload", refuse_every_upload)
    plan_path = run_driver(busy_board, tmp_path, monkeypatch, "--limit", "4", "--jobs", "1")

    refused = next(row for row in read_plan(plan_path) if row["note"])
    assert refused["fetched_at"] and refused["measured_at"] and refused["repaired_at"]
    assert stage_of(refused) == "upload", "it waits at upload, having done everything before it"


@pytest.mark.parametrize(("message", "expected"), [
    ('HttpError 400 ... "reason": "uploadLimitExceeded"', True),
    ("[Errno 113] No route to host", False),
    ("HttpError 403 ... quotaExceeded", False),
])
def test_only_the_upload_limit_ends_a_run(message, expected) -> None:
    """
    Every other failure is one recording's problem and the run carries on.

    :param message: What the upload raised
    :param expected: Whether it should end the run
    """
    assert migrate.is_daily_limit(RuntimeError(message)) is expected
