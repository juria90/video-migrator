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

import pathlib
import subprocess
import sys
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
