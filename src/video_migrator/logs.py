#!/usr/bin/env python3
"""
One logging format, so that a long run says when each thing happened.

A migration runs for hours and is read afterwards as much as during: the useful
questions are how long a fetch took, whether an encode stalled, how far apart
two retries were. Bare prints answer none of those, and a progress line that
overwrites itself answers them least of all — it shows only the present moment,
and keeps no record of the ones before it.

Timestamps are ISO 8601 to the millisecond, which sorts, greps and subtracts.
"""

import logging
import time
from collections.abc import Callable

#: How each line is written. The date format cannot express fractions, so the
#: milliseconds are appended from the record itself.
FORMAT = "%(asctime)s.%(msecs)03d %(levelname)-7s %(message)s"

#: ISO 8601, without the fraction that :data:`FORMAT` adds back.
DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"


def configure(level: int = logging.INFO, stream=None) -> None:
    """
    Set up logging for a command-line run.

    Only a program's entry point should call this. A library module asks for its
    own logger and writes to it, leaving the decision about where that goes to
    whoever is running it.

    :param level: The lowest level to report
    :param stream: Where to write, defaulting to standard error
    :return: None
    """
    logging.basicConfig(format=FORMAT, datefmt=DATE_FORMAT, level=level, stream=stream, force=True)


#: How often a long stage first says how far it has got.
PROGRESS_SECONDS = 30.0

#: How rarely it says so once it has been running a while. A fetch takes a
#: minute and a half and an encode takes twenty, and no single interval suits
#: both: half a minute is right for the first, and would fill a screen for the
#: second.
PROGRESS_LONGEST = 180.0

#: How the interval stretches. At a tenth of the elapsed time, a stage reports
#: about ten times however long it runs for — every 30 seconds through the first
#: five minutes, and every three minutes once past half an hour.
PROGRESS_STRETCH = 10.0


class Ticker:
    """Says when a long-running stage is due to report its progress again."""

    def __init__(self, seconds: float = PROGRESS_SECONDS, clock: Callable[[], float] = time.monotonic,
                 longest: float = PROGRESS_LONGEST, stretch: float = PROGRESS_STRETCH) -> None:
        """
        Start a ticker, due immediately so that a stage reports as it begins.

        :param seconds: How long between the first reports
        :param clock: What to read the time from, for tests
        :param longest: How long between reports at most, however long it runs
        :param stretch: The fraction of elapsed time to wait, as a divisor
        """
        self.seconds = seconds
        self.longest = longest
        self.stretch = stretch
        self.clock = clock
        self.started = clock()
        self.last = self.started - seconds

    def interval(self) -> float:
        """
        How long to wait before reporting again, given how long this has run.

        :return: Seconds, between :data:`PROGRESS_SECONDS` and
            :data:`PROGRESS_LONGEST`

        >>> now = [0.0]
        >>> ticker = Ticker(clock=lambda: now[0])
        >>> ticker.interval()
        30.0
        >>> now[0] = 600
        >>> ticker.interval()
        60.0
        >>> now[0] = 100000
        >>> ticker.interval()
        180.0
        """
        elapsed = self.clock() - self.started
        return min(self.longest, max(self.seconds, elapsed / self.stretch))

    def due(self) -> bool:
        """
        Is it time to report again?

        Asked once per chunk or per frame, so it must be cheap and must not
        report every time it is asked.

        :return: True when enough time has passed, and not again until it has
            passed once more

        >>> now = [100.0]
        >>> ticker = Ticker(30.0, lambda: now[0])
        >>> ticker.due(), ticker.due()
        (True, False)
        >>> now[0] += 29
        >>> ticker.due()
        False
        >>> now[0] += 2
        >>> ticker.due()
        True
        """
        if self.clock() - self.last < self.interval():
            return False
        self.last = self.clock()
        return True
