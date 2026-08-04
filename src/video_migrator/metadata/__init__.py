"""Metadata normalization and writing."""

from .ffmpeg import update_video_metadata
from .normalize import fix_video_metadata, validate_videos

__all__ = ["fix_video_metadata", "update_video_metadata", "validate_videos"]
