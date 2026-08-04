"""Source platforms - where videos are downloaded from.

Each module here knows how to pull a video off one hosting platform. Add a new
source by writing a module with a downloader and registering it in
:data:`SOURCES`.

Downloaders are imported lazily so that a missing optional dependency (for
example Playwright, used only by the browser-driven Aninamu strategy) does not
break importing this package.
"""

from importlib import import_module

# Source name -> "module:attribute" of its downloader entry point.
SOURCES = {
    "vimeo": "video_migrator.sources.vimeo:VimeoDownloader",
    "aninamu": "video_migrator.sources.aninamu:AninamuDownloader",
}


def get_downloader(source: str):
    """
    Look up a downloader class by source platform name.

    :param source: Source key registered in :data:`SOURCES` (e.g. 'vimeo')
    :return: The downloader class for that platform
    :raises KeyError: If no downloader is registered for the source
    :raises ImportError: If the downloader's optional dependencies are missing
    """
    if source not in SOURCES:
        raise KeyError(f"No downloader registered for source '{source}'. Known sources: {', '.join(sorted(SOURCES))}")

    module_path, _, attribute = SOURCES[source].partition(":")
    return getattr(import_module(module_path), attribute)


__all__ = ["SOURCES", "get_downloader"]
