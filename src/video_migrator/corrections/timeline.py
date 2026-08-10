#!/usr/bin/env python3
"""
Judge a record's date against the order its video was uploaded in, and judge
whether a scrape is recent enough to draw conclusions from at all.

A hosting platform hands out ids in order, so within one board an id predicts a
publish date closely. A record far off that curve is either mis-dated or was
uploaded long after the event, and telling those apart is the whole difficulty.
"""

import datetime
import pathlib
import statistics

#: How far a date may sit from what its id implies before it is worth reporting.
#: An id predicts a date to within a couple of days in practice, so a month is
#: generous.
MAX_ID_DRIFT_DAYS = 30

#: How many records either side form the flanking blocks an estimate is taken
#: from, and the share of adjacent pairs in that span whose dates must climb with
#: their ids before an outlier inside it is believed.
ID_DRIFT_WINDOW = 8
MIN_ID_DRIFT_ORDER = 0.9


def find_misdated(
    dated: list[tuple[int, datetime.date, str]],
    max_drift: int = MAX_ID_DRIFT_DAYS,
    window: int = ID_DRIFT_WINDOW,
    min_order: float = MIN_ID_DRIFT_ORDER,
) -> dict[str, datetime.date]:
    """
    Find records whose video id says they were posted at a different time.

    Telling a typo from a back-filled archive takes two guards, because the
    naive form of each is wrong.

    The estimate cannot come from the immediately neighbouring records. A wrong
    date is a bracket endpoint for the records either side of it, so one typo
    drags its own neighbours off the curve with it. Each side is reduced to a
    median first, which a single bad date barely moves.

    The cluster test cannot ask whether those neighbours look wrong either, for
    the same reason — it would suppress exactly the isolated typo it exists to
    find. What separates the two cases is the *order* of the neighbourhood: an
    archive back-filled later has ids climbing while dates fall, so nearly every
    adjacent pair disagrees, whereas one typo breaks only the two pairs it
    touches. A record is reported only from a neighbourhood otherwise in order.

    :param dated: (video id, the date recorded, the record's key), in any order
    :param max_drift: How many days a date may sit from its estimate
    :param window: How many records either side form the flanking blocks
    :param min_order: Share of nearby pairs that must run in id order
    :return: Record key -> the date its video id implies, for isolated outliers only
    """
    ordered = sorted(dated)
    ids = [entry[0] for entry in ordered]
    days = [entry[1].toordinal() for entry in ordered]

    def implied(index: int) -> datetime.date | None:
        """Estimate one record's date from the median of the records flanking it."""
        left = range(max(0, index - window), index)
        right = range(index + 1, min(len(ordered), index + 1 + window))
        if len(left) < 2 or len(right) < 2:
            return None
        low_id, low_day = statistics.median(ids[j] for j in left), statistics.median(days[j] for j in left)
        high_id, high_day = statistics.median(ids[j] for j in right), statistics.median(days[j] for j in right)
        if high_id == low_id:
            return None
        share = (ids[index] - low_id) / (high_id - low_id)
        return datetime.date.fromordinal(round(low_day + (high_day - low_day) * share))

    def well_ordered(index: int) -> bool:
        """Do the dates around this record climb with the ids, ignoring the record itself?"""
        pairs = [(j, j + 1) for j in range(max(0, index - window), min(len(ordered) - 1, index + window))
                 if index not in (j, j + 1)]
        if not pairs:
            return False
        return sum(days[b] >= days[a] for a, b in pairs) / len(pairs) >= min_order

    out = {}
    for index, (_, when, key) in enumerate(ordered):
        guess = implied(index)
        if guess is None or abs((guess - when).days) <= max_drift:
            continue
        if not well_ordered(index):
            continue
        out[key] = guess
    return out


def cache_age_hours(cache_dir: pathlib.Path) -> float | None:
    """
    How long ago a scrape cache was last written.

    Worth asking before merging anything, because a merge reads the cache as the
    site's present state: it records a correction as landed because the cache
    holds its new value, and retracts a finding because the cache no longer shows
    the problem. Against an old scrape both go wrong together, and work already
    done comes back as outstanding.

    :param cache_dir: The directory the scrape cached into
    :return: Age of the newest file in hours, or None when the cache is empty
    """
    newest = max((f.stat().st_mtime for f in pathlib.Path(cache_dir).glob("*")), default=None)
    if newest is None:
        return None
    return (datetime.datetime.now().timestamp() - newest) / 3600
