"""Pytest configuration.

Skips collecting modules whose optional dependencies are not installed, so
``pytest --doctest-modules src/video_migrator/`` works on a plain ``[dev]``
install.
"""

from importlib.util import find_spec

collect_ignore = []

# These drive a real browser and so need the [browser] extra. Nothing else
# imports them, so the rest of the package still collects without it.
if find_spec("playwright") is None:
    collect_ignore.append("src/video_migrator/sources/aninamu_playwright.py")
    collect_ignore.append("src/video_migrator/corrections/churchlove.py")
