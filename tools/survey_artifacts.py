#!/usr/bin/env python3
"""
Measure many recordings, to see how a fault is distributed rather than whether one has it.

A single recording says almost nothing about where to set a threshold. The comb
this archive's 720p years carry measures anywhere from 2.6 to 6.0 times above
the surrounding spectrum depending on how much the sampled frames happened to
move, and a cutoff placed in the middle of that range decides some recordings
one way and their neighbours the other.

So this walks a slice of the board, fetches each recording, measures it, and
throws it away again — keeping only the numbers. Disk stays flat regardless of
how many are surveyed, which is what makes surveying a hundred practical.

Interrupted runs resume: a recording already in the output file is not fetched
again.

    uv run --with numpy python tools/survey_artifacts.py --board sites/<site>/data/<export>.tsv \
        --year-from 2015 --year-to 2021 --count 20 --keep
"""

import argparse
import csv
import logging
import pathlib
import random
import sys
import traceback

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from measure_artifacts import measure_comb, measure_hum, motion_offsets, probe  # noqa: E402

from video_migrator.logs import configure  # noqa: E402
from video_migrator.media.artifacts import repair_filter  # noqa: E402
from video_migrator.sources.vimeo_api import VimeoAPI, load_token  # noqa: E402

COLUMNS = ["num", "date", "vimeo_id", "width", "height", "fps", "seconds", "gib",
           "period", "comb_agreement", "hum_velocity", "hum_amplitude", "hum_prominence", "repair"]


logger = logging.getLogger(__name__)


def read_done(path: pathlib.Path) -> set[str]:
    """
    Which recordings a previous run already measured.

    :param path: The output file
    :return: Their record ids
    """
    if not path.exists():
        return set()
    with path.open(encoding="utf-8") as handle:
        return {row["num"] for row in csv.DictReader(handle, delimiter="\t")}


def choose(board: pathlib.Path, preacher: str, years: range, count: int, seed: int) -> list[dict]:
    """
    Pick a spread of recordings to measure.

    Sampled per year rather than at random over the whole slice, so a year that
    happens to hold more recordings does not crowd out its neighbours — the
    boundaries between formats are what a survey is looking for.

    :param board: The scraped export to choose from
    :param preacher: Keep only this preacher, empty for all
    :param years: Years to cover
    :param count: How many recordings in total
    :param seed: Seed, so a repeated run picks the same recordings
    :return: The chosen rows
    """
    with board.open(encoding="utf-8") as handle:
        rows = [row for row in csv.DictReader(handle, delimiter="\t")
                if row["Type"] == "vimeo"
                and (not preacher or row["Preacher"] == preacher)
                and row["Year"].isdigit() and int(row["Year"]) in years]

    by_year: dict[str, list[dict]] = {}
    for row in rows:
        by_year.setdefault(row["Year"], []).append(row)

    random.seed(seed)
    per_year = max(1, count // max(1, len(by_year)))
    chosen: list[dict] = []
    for year in sorted(by_year):
        chosen += random.sample(by_year[year], min(per_year, len(by_year[year])))
    return chosen[:count]


def main() -> int:
    """
    Survey a slice of the board and write one row of measurements per recording.

    :return: 0
    """
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--board", type=pathlib.Path, required=True,
                        help="A scraped export to choose recordings from")
    parser.add_argument("--preacher", default="", help="Keep only this preacher; omit for every preacher")
    parser.add_argument("--year-from", type=int, default=2015)
    parser.add_argument("--year-to", type=int, default=2021)
    parser.add_argument("--count", type=int, default=20)
    parser.add_argument("--samples", type=int, default=16, help="Frames measured per recording")
    parser.add_argument("--seed", type=int, default=11)
    # Shared with whatever downloads next, so a recording fetched to be measured
    # does not have to be fetched again to be repaired and uploaded.
    parser.add_argument("--work-dir", type=pathlib.Path, default=pathlib.Path.home() / "vimeo-work")
    # Beside the export it read, which is to say under a site's data/ — gitignored,
    # because every row names a record and a Vimeo id, and an id resolves to a
    # real recording.
    parser.add_argument("--out", type=pathlib.Path,
                        help="Where to write the measurements (default: beside --board)")
    parser.add_argument("--keep", action="store_true",
                        help="Keep each recording in --work-dir instead of deleting it once measured, "
                             "so the download can be reused to repair and upload")
    args = parser.parse_args()
    configure()
    if args.out is None:
        args.out = args.board.parent / "artifact-survey.tsv"

    chosen = choose(args.board, args.preacher, range(args.year_from, args.year_to + 1), args.count, args.seed)
    done = read_done(args.out)
    args.work_dir.mkdir(parents=True, exist_ok=True)
    api = VimeoAPI(load_token())

    fresh = not args.out.exists()
    with args.out.open("a", encoding="utf-8", newline="\n") as handle:
        out = csv.writer(handle, delimiter="\t", lineterminator="\n")
        if fresh:
            out.writerow(COLUMNS)
            handle.flush()
        logger.info(f"{len(chosen)} chosen, {len(done)} already measured")

        for index, row in enumerate(chosen, 1):
            if row["num"] in done:
                continue
            path = args.work_dir / f"{row['num']}-{row['ID']}.mp4"
            # Said before each slow step rather than after it. A recording takes
            # minutes to fetch and minutes more to decode looking for movement,
            # and a run that says nothing for that long is indistinguishable
            # from one that has stopped.
            logger.info(f"  [{index}/{len(chosen)}] num={row['num']} {row['Publish Date']} {'cached' if path.exists() else 'fetching'}...")
            try:
                if not path.exists():
                    api.download(row["ID"], path)
                duration, fps, size = probe(path)
                logger.info(f"        {size[0]}x{size[1]} {duration / 60:.0f}min {path.stat().st_size / (1 << 30):.1f}GiB, finding movement...")
                period, agreement, strength = measure_comb(
                    path, motion_offsets(path, args.samples, duration), size)
                logger.info(f"        period {period:.0f} in {agreement:.0%} of frames (median {strength:.1f}x), measuring hum...")
                velocity, amplitude, hum = measure_hum(path, max(0.0, duration / 2 - 60), 120.0, fps)
                repair = repair_filter(period, agreement)
                gib = path.stat().st_size / (1 << 30)
                out.writerow([row["num"], row["Publish Date"], row["ID"], size[0], size[1], f"{fps:.3f}",
                              f"{duration:.0f}", f"{gib:.2f}", f"{period:.0f}", f"{agreement:.2f}",
                              f"{velocity:.4f}", f"{amplitude:.2f}", f"{hum:.1f}",
                              "deinterlace" if repair and repair.startswith("bwdif")
                              else "notch" if repair else "none"])
                handle.flush()
                logger.info(f"        hum {amplitude:.2f}pp at {hum:.1f}x  ->  "
                      f"{'deinterlace' if repair and repair.startswith('bwdif') else 'notch' if repair else 'none'}")
            except Exception as exc:
                logger.info(f"  [{index}/{len(chosen)}] num={row['num']} FAILED {type(exc).__name__}: {exc}")
                traceback.print_exc(limit=1)
            finally:
                if path.exists() and not args.keep:
                    path.unlink()
    logger.info(f"written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
