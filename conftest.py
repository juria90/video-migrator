"""Pytest configuration.

Skips collecting modules whose optional dependencies are not installed, so
``pytest --doctest-modules src/video_migrator/`` works on a plain ``[dev]``
install.
"""

from importlib.util import find_spec

collect_ignore = []

# video_migrator.sources.aninamu_playwright needs the [browser] extra.
if find_spec("playwright") is None:
    collect_ignore.append("src/video_migrator/sources/aninamu_playwright.py")
