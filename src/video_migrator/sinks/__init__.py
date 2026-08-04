"""Destination platforms - where videos are uploaded to.

Add a new destination by writing a module with an uploader and registering it
in :data:`SINKS`. Uploaders are imported lazily so their API clients are only
loaded when actually used.
"""

from importlib import import_module

# Sink name -> "module:attribute" of its uploader entry point.
SINKS = {
    "youtube": "video_migrator.sinks.youtube:upload",
}


def get_uploader(sink: str):
    """
    Look up an upload callable by destination platform name.

    :param sink: Sink key registered in :data:`SINKS` (e.g. 'youtube')
    :return: The upload callable for that platform
    :raises KeyError: If no uploader is registered for the sink
    :raises ImportError: If the uploader's dependencies are missing
    """
    if sink not in SINKS:
        raise KeyError(f"No uploader registered for sink '{sink}'. Known sinks: {', '.join(sorted(SINKS))}")

    module_path, _, attribute = SINKS[sink].partition(":")
    return getattr(import_module(module_path), attribute)


__all__ = ["SINKS", "get_uploader"]
