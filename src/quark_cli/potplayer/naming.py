"""Name-based helpers: natural ordering, media classification, and MIME type."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

_DIGITS = re.compile(r"(\d+)")

# Only these extensions join a same-directory playlist. Playback itself is not
# restricted to them; a playlist entry the player cannot decode would silently
# break "next episode" ordering, so ambiguous extensions stay out.
VIDEO_EXTENSIONS = frozenset({
    ".3gp", ".asf", ".avi", ".dat", ".divx", ".f4v", ".flv", ".iso", ".m2t",
    ".m2ts", ".m2v", ".m4v", ".mkv", ".mov", ".mp4", ".mpg", ".mpeg", ".mts",
    ".ogm", ".ogv", ".rm", ".rmvb", ".ts", ".vob", ".webm", ".wmv",
})

_CONTENT_TYPES = {
    ".3gp": "video/3gpp",
    ".asf": "video/x-ms-asf",
    ".avi": "video/x-msvideo",
    ".flv": "video/x-flv",
    ".m2ts": "video/mp2t",
    ".m4v": "video/mp4",
    ".mkv": "video/x-matroska",
    ".mov": "video/quicktime",
    ".mp4": "video/mp4",
    ".mpeg": "video/mpeg",
    ".mpg": "video/mpeg",
    ".mts": "video/mp2t",
    ".ogv": "video/ogg",
    ".rm": "application/vnd.rn-realmedia",
    ".rmvb": "application/vnd.rn-realmedia-vbr",
    ".ts": "video/mp2t",
    ".vob": "video/dvd",
    ".webm": "video/webm",
    ".wmv": "video/x-ms-wmv",
}


def extension(name: str) -> str:
    """Return the lower-cased extension including the dot, or an empty string."""
    dot = name.rfind(".")
    if dot <= 0 or dot == len(name) - 1:
        return ""
    return name[dot:].casefold()


def natural_key(name: str) -> tuple[tuple[int, int, str], ...]:
    """Order "第9集" before "第10集" instead of comparing digit characters."""
    parts: list[tuple[int, int, str]] = []
    for part in _DIGITS.split(name):
        if not part:
            continue
        if part.isdigit():
            parts.append((1, int(part), ""))
        else:
            parts.append((0, 0, part.casefold()))
    return tuple(parts)


def order_key(item: Mapping[str, Any]) -> tuple[Any, ...]:
    name = item["name"]
    return (natural_key(name), name)


def is_video(name: str) -> bool:
    return extension(name) in VIDEO_EXTENSIONS


def content_type(name: str) -> str:
    return _CONTENT_TYPES.get(extension(name), "application/octet-stream")
