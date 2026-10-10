"""Same-directory subtitle matching and bounded in-memory subtitle reads.

Nothing is written to disk: the bridge only lists sibling SRT/ASS names, and a
chosen subtitle is fetched through the same trusted, cookie-isolated transfer
path as playback, then decoded in memory.
"""

from __future__ import annotations

import codecs
import re
from collections.abc import Mapping
from contextlib import ExitStack
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..client import QuarkClient
from ..errors import RemoteError, UsageError
from ..files import Files
from ..paths import is_directory, valid_fid
from ..transfer import download_address, successor_url, trusted_url
from .library import require_fid
from .naming import extension, is_video, order_key
from .stream import Sessions

SUBTITLE_EXTENSIONS = frozenset({".srt", ".ass"})
SUBTITLE_FORMATS = {".srt": "srt", ".ass": "ass"}
LIST_PAGE_SIZE = 100
MAX_SUBTITLE_SIZE = 4 * 1024 * 1024
REDIRECT_LIMIT = 6
CHUNK_SIZE = 64 * 1024
_REDIRECT_STATUSES = (301, 302, 303, 307, 308)
_AUTH_STATUSES = (401, 403)


class UnknownSession(Exception):
    """A play-session selector that no longer names a live session."""


# "S01E07", tolerating separators like "S01.E07"; the lookbehind rejects text
# that happens to contain the letters inside a word such as "class12e34".
_SEASON_EPISODE = re.compile(r"(?<![0-9A-Za-z])[sS](\d{1,2})[\s._-]*[eE](\d{1,3})")
_CN_EPISODE = re.compile(r"第\s*(\d{1,4})\s*[集话]")
# A single trailing language tag on a subtitle name; anything else stays put so
# "剧.S01E01.1080p" is never mistaken for an exact name match.
_LANGUAGE_SUFFIX = re.compile(
    r"[._\-\s]+(?P<lang>"
    r"zh[-_]?(?:hans|hant|cn|tw|hk|sg)|"
    r"zh|zho|chi|chs|cht|cn|sc|tc|"
    r"en|eng|english|"
    r"简体中文|繁体中文|简体|繁体|中文|中字|简中|繁中|英文"
    r")$",
    re.IGNORECASE,
)
_LANGUAGE_CODES = {
    **{token: "zh" for token in (
        "zh", "zho", "chi", "chs", "cht", "cn", "sc", "tc",
        "zh-hans", "zh_hans", "zh-hant", "zh_hant", "zh-cn", "zh_cn",
        "zh-tw", "zh_tw", "zh-hk", "zh_hk", "zh-sg", "zh_sg",
        "中文", "简体", "繁体", "中字", "简中", "繁中", "简体中文", "繁体中文",
    )},
    **{token: "en" for token in ("en", "eng", "english", "英文")},
}
_LANGUAGE_RANK = {"zh": 0, "en": 1, "": 2}


def match_subtitles(
    files: Files,
    sessions: Sessions,
    fid: str | None,
    session: str | None,
) -> dict[str, Any]:
    """List same-directory SRT/ASS candidates for exactly one video selector."""
    video_fid = _video_fid(sessions, fid, session)
    info = files.info(video_fid)
    name = info.get("file_name")
    if is_directory(info) or not isinstance(name, str) or not name:
        raise UsageError("fid 不是网盘视频文件")
    if not is_video(name):
        raise UsageError("fid 不是支持的视频文件")
    parent = require_fid(info.get("pdir_fid"), label="pdir_fid", allow_root=True)
    snapshot = files.list_all(parent, 1, LIST_PAGE_SIZE)
    return {"video_fid": video_fid, "items": _candidates(snapshot["data"]["list"], name)}


def read_subtitle(files: Files, client: QuarkClient, fid: str) -> str:
    """Read one SRT/ASS file into memory, bounded and trusted, never to disk."""
    info = files.info(fid)
    name = info.get("file_name")
    if is_directory(info) or not isinstance(name, str) or not name:
        raise UsageError("fid 不是网盘字幕文件")
    suffix = extension(name)
    if suffix not in SUBTITLE_EXTENSIONS:
        raise UsageError("fid 不是支持的字幕文件")
    _reject_oversized(info.get("size"))
    address = download_address(client, fid)
    _reject_oversized(address.size)
    return _decode(_fetch(client, address.url, fid))


def _video_fid(sessions: Sessions, fid: str | None, session: str | None) -> str:
    if (fid is None) == (session is None):
        raise UsageError("必须且只能指定 fid 或 session 之一")
    if session is None:
        return require_fid(fid, label="fid")
    entry = sessions.get(session)
    if entry is None:
        raise UnknownSession()
    return require_fid(entry.fid, label="fid")


def _reject_oversized(size: Any) -> None:
    if isinstance(size, int) and not isinstance(size, bool) and size > MAX_SUBTITLE_SIZE:
        raise UsageError("字幕文件超过 4 MiB 上限")


def _candidates(entries: list[dict[str, Any]], video_name: str) -> list[dict[str, Any]]:
    video_stem = _stem(video_name)
    items: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, Mapping) or is_directory(entry):
            continue
        name = entry.get("file_name")
        if not isinstance(name, str) or not name:
            continue
        suffix = extension(name)
        if suffix not in SUBTITLE_EXTENSIONS:
            continue
        matched = _match(video_stem, _stem(name))
        if matched is None:
            continue
        match, lang = matched
        items.append({
            "fid": _fid(entry),
            "name": name,
            "format": SUBTITLE_FORMATS[suffix],
            "lang": lang,
            "match": match,
        })
    items.sort(key=lambda item: (_LANGUAGE_RANK[item["lang"]], order_key(item)))
    return items


def _fid(entry: Mapping[str, Any]) -> str:
    try:
        return valid_fid(entry.get("fid"))
    except RemoteError:
        raise RemoteError("同目录字幕记录缺少有效 FID") from None


def _stem(name: str) -> str:
    suffix = extension(name)
    return name[: len(name) - len(suffix)] if suffix else name


def _match(video_stem: str, subtitle_stem: str) -> tuple[str, str] | None:
    """Return ``(kind, lang)`` for one candidate name, or ``None`` to skip it."""
    base, lang = _language(subtitle_stem)
    if base.casefold() == video_stem.casefold():
        return "name", lang
    video_marker = _episode(video_stem)
    subtitle_marker = _episode(base)
    if video_marker is None or subtitle_marker is None:
        return None
    video_season, video_number, video_start = video_marker
    season, number, start = subtitle_marker
    # A season-less "第1集" never confirms a numbered season, and vice versa.
    if season != video_season or number != video_number:
        return None
    if _series(video_stem, video_start) != _series(base, start):
        return None
    return "episode", lang


def _episode(stem: str) -> tuple[int | None, int, int] | None:
    match = _SEASON_EPISODE.search(stem)
    if match is not None:
        return int(match.group(1)), int(match.group(2)), match.start()
    match = _CN_EPISODE.search(stem)
    if match is not None:
        return None, int(match.group(1)), match.start()
    return None


def _series(stem: str, start: int) -> str:
    return re.sub(r"[._\-\s]+", "", stem[:start]).casefold()


def _language(stem: str) -> tuple[str, str]:
    """Split ``stem`` into its base name and a recognised language code."""
    match = _LANGUAGE_SUFFIX.search(stem)
    if match is None:
        return stem, ""
    base = stem[: match.start()]
    if not base:
        return stem, ""
    return base, _LANGUAGE_CODES.get(match.group("lang").casefold(), "")


def _fetch(client: QuarkClient, url: str, fid: str) -> bytes:
    """Read the signed URL through trusted hops, retrying once after an auth failure."""
    for attempt in (0, 1):
        with ExitStack() as stack:
            storage = client.new_transfer_client()
            stack.callback(client.release_transfer_clients, [storage])
            current = url
            response = None
            for _ in range(REDIRECT_LIMIT):
                current = trusted_url(current)
                headers = {"Accept-Encoding": "identity"}
                cookie = client.transfer_cookie_header(urlsplit(current).hostname or "")
                if cookie is not None:
                    headers["Cookie"] = cookie
                response = stack.enter_context(storage.stream("GET", current, headers=headers))
                if response.status_code in _REDIRECT_STATUSES:
                    location = response.headers.get("location")
                    if not location:
                        raise RemoteError("下载地址重定向无效")
                    current = successor_url(current, location)
                    continue
                break
            else:
                raise RemoteError("下载地址重定向次数过多")
            if response.status_code in _AUTH_STATUSES:
                if attempt == 0:
                    url = download_address(client, fid).url
                    continue
                raise RemoteError("字幕取链失效且重新获取后仍无法下载")
            if response.status_code != 200:
                raise RemoteError(f"下载源返回了异常状态 {response.status_code}")
            encoding = response.headers.get("content-encoding")
            if encoding and encoding.casefold() != "identity":
                raise RemoteError("下载源启用了压缩编码，已中止")
            length = response.headers.get("content-length")
            if length is not None and length.isdigit() and int(length) > MAX_SUBTITLE_SIZE:
                raise RemoteError("字幕内容超过 4 MiB 上限")
            return _bounded(response)
    raise AssertionError("unreachable subtitle retry state")


def _bounded(response: httpx.Response) -> bytes:
    """Stop the moment the real body crosses the limit, whatever the header said."""
    body = bytearray()
    for chunk in response.iter_raw(CHUNK_SIZE):
        if not chunk:
            continue
        if len(body) + len(chunk) > MAX_SUBTITLE_SIZE:
            raise RemoteError("字幕内容超过 4 MiB 上限")
        body += chunk
    return bytes(body)


def _decode(raw: bytes) -> str:
    """Decode subtitle text by BOM, then UTF-8, then GB18030; never lossy."""
    if raw.startswith(codecs.BOM_UTF8) or raw.startswith(codecs.BOM_UTF16_LE) or raw.startswith(codecs.BOM_UTF16_BE):
        encoding = "utf-8-sig" if raw.startswith(codecs.BOM_UTF8) else "utf-16"
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            raise RemoteError("字幕编码无法识别") from None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return raw.decode("gb18030")
    except UnicodeDecodeError:
        raise RemoteError("字幕编码无法识别") from None
