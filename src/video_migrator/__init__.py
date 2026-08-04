"""Video Migrator.

Tools for moving videos and their metadata from one hosting platform to
another: scrape a listing, download from the source platform, normalize and
write metadata, then upload to the destination platform.

The package is organized around the stages of that pipeline:

- :mod:`video_migrator.scrapers` - discover videos on a website
- :mod:`video_migrator.sources` - download from a source platform (Vimeo, ...)
- :mod:`video_migrator.metadata` - normalize and write file metadata
- :mod:`video_migrator.sinks` - upload to a destination platform (YouTube, ...)
"""

__version__ = "0.1.0"

from .metadata.ffmpeg import update_video_metadata
from .models import Video

__all__ = ["Video", "update_video_metadata"]
