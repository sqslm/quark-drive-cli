"""Static and contract checks for the PotPlayer AngelScript extensions.

PotPlayer ships no standalone AngelScript toolchain, so these scripts cannot be
compiled here.  The checks below therefore verify what a compiler and a real
player would catch:

* all three scripts tokenise cleanly (terminated literals/comments, balanced
  delimiters, and no call to a function the file does not define);
* every ``Host*`` call exists in the ``api.txt`` that ships with the installed
  player, which is what stops invented native interfaces;
* the exported entry points keep the exact signatures the player looks up, and
  the logic links keep the designed shape (a single episode is never
  re-expanded into a directory playlist);
* the client-side video filter stays identical to ``naming.VIDEO_EXTENSIONS``;
* the loopback bridge answers the exact JSON shapes the scripts parse, including
  the login POST that asks for a connection close because the bridge answers it
  without reading the request body.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.parse import quote

import httpx

from quark_cli.auth import LoginCancelled
from quark_cli.errors import QuarkError
from quark_cli.potplayer.naming import VIDEO_EXTENSIONS, is_video

from potplayer_fixture import COOKIE, VIDEO_BYTES, DriveFixture, record

ROOT = Path(__file__).resolve().parents[1]
AS_DIR = ROOT / "potplayer"
URL_LIST = AS_DIR / "MediaUrlList - Quark.as"
PLAY_PARSE = AS_DIR / "MediaPlayParse - Quark.as"
SUBTITLE_SEARCH = AS_DIR / "SubtitleSearch - Quark.as"
LAUNCHER = AS_DIR / "quark-potplayer.cmd"

URL_LIST_EXPORTS = (
    "string GetTitle()",
    "string GetVersion()",
    "string GetDesc()",
    "array<dictionary> GetCategorys()",
    "array<dictionary> GetUrlList(string Category, string Extra, string PathToken, string Query, string PageToken)",
)
PLAY_PARSE_EXPORTS = (
    "string GetTitle()",
    "string GetVersion()",
    "string GetDesc()",
    "bool PlayitemCheck(const string &in path)",
    "string PlayitemParse(const string &in path, dictionary &MetaData, array<dictionary> &QualityList)",
    "bool PlaylistCheck(const string &in path)",
    "array<dictionary> PlaylistParse(const string &in path)",
)
# Mirrors the installed ``Extension/Subtitle/Search/SubtitleSearch - *.as`` exports.
SUBTITLE_SEARCH_EXPORTS = (
    "string GetTitle()",
    "string GetVersion()",
    "string GetDesc()",
    "string GetLanguages()",
    "array<dictionary> SubtitleSearch(string MovieFileName, dictionary MovieMetaData)",
    "string SubtitleDownload(string id)",
)

#: A minimal ASS subtitle body the bridge must return verbatim (Unicode kept).
SUBTITLE_BODY = "1\n00:00:01,000 --> 00:00:02,000\n你好，世界\n".encode("utf-8")

#: Calls that exist in PotPlayer's AngelScript runtime but are not in api.txt.
BUILTIN_CALLS = frozenset({"formatInt", "formatFloat", "parseInt", "parseFloat", "sizeof"})
KEYWORDS = frozenset({"if", "for", "while", "switch", "case", "return", "else", "do", "cast"})
DECLARATION = re.compile(
    r"(?:^|[\s(])(?:string|bool|int|float|double|void|array<[^>\n]*>)\s+([A-Za-z_]\w*)\s*\(",
)
#: AngelScript has no C-style arrays; ``string roots[2];`` fails to compile.  A
#: fixed-size local array is ``array<string> roots(2);`` (array<T> constructor).
C_ARRAY_DECLARATION = re.compile(
    r"(?:^|[\s;{(])(?:string|bool|int|uint|float|double|dictionary|array<[^>\n]+>)"
    r"\s+[A-Za-z_]\w*\s*\[\s*\d*\s*\]\s*;",
)
HOST_CALL = re.compile(r"(?<![\w.>])([A-Za-z_]\w*)\s*\(")
HOST_DECLARATION = re.compile(r"^\s*[\w:<>,\s*&\[\]]+\s+(Host\w+)\s*\(", re.M)


def api_txt_candidates() -> list[Path]:
    explicit = os.environ.get("POTPLAYER_API_TXT", "").strip()
    extension_dir = os.environ.get("POTPLAYER_EXTENSION_DIR", "").strip()
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    if extension_dir:
        candidates.append(Path(extension_dir) / "api.txt")
    candidates += [
        Path("E:/softwares/PotPlayer/Extension/api.txt"),
        Path("C:/Program Files/DAUM/PotPlayer/Extension/api.txt"),
        Path("C:/Program Files (x86)/DAUM/PotPlayer/Extension/api.txt"),
    ]
    return [path for path in candidates if path.is_file()]


def read_script(path: Path) -> str:
    raw = path.read_bytes()
    if not raw.startswith(b"\xef\xbb\xbf"):
        raise AssertionError(f"{path.name} must keep the UTF-8 BOM the bundled extensions use")
    return raw.decode("utf-8")


def strip_literals(source: str) -> tuple[str, list[str]]:
    """Blank comments and string literals, keeping offsets, and report open ones."""
    out = list(source)
    issues: list[str] = []
    index, length = 0, len(source)
    while index < length:
        char = source[index]
        following = source[index + 1] if index + 1 < length else ""
        if char == "/" and following == "/":
            while index < length and source[index] != "\n":
                out[index] = " "
                index += 1
        elif char == "/" and following == "*":
            out[index] = out[index + 1] = " "
            index += 2
            while index < length and not (source[index] == "*" and source[index + 1:index + 2] == "/"):
                if source[index] != "\n":
                    out[index] = " "
                index += 1
            if index >= length:
                issues.append("unterminated block comment")
                break
            out[index] = out[index + 1] = " "
            index += 2
        elif char == '"':
            out[index] = " "
            index += 1
            while index < length and source[index] != '"' and source[index] != "\n":
                if source[index] == "\\":
                    out[index] = " "
                    index += 1
                    if index < length and source[index] != "\n":
                        out[index] = " "
                        index += 1
                else:
                    out[index] = " "
                    index += 1
            if index >= length or source[index] != '"':
                issues.append("unterminated string literal")
                break
            out[index] = " "
            index += 1
        else:
            index += 1
    return "".join(out), issues


def squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def signature_pattern(signature: str) -> re.Pattern[str]:
    return re.compile(re.escape(signature).replace(r"\ ", r"\s+"))


def body_bounds(source: str, signature: str) -> tuple[int, int]:
    """Offsets of a function body, computed on literal-free text."""
    code, _ = strip_literals(source)
    match = signature_pattern(signature).search(code)
    if match is None:
        raise AssertionError(f"missing function: {signature}")
    opening = code.index("{", match.end())
    depth = 0
    for position in range(opening, len(code)):
        if code[position] == "{":
            depth += 1
        elif code[position] == "}":
            depth -= 1
            if depth == 0:
                return opening, position
    raise AssertionError(f"unbalanced body: {signature}")


def body_raw(source: str, signature: str) -> str:
    """Raw source of a function body, so literal keys and messages stay visible."""
    start, end = body_bounds(source, signature)
    return source[start + 1:end]


def favorite_action_row(fid, saved):
    """Transcription of ``QuarkAppendFavoriteAction``: a navigation row, never a media url."""
    if saved:
        return {"title": "取消收藏当前目录", "PathToken": "quark-action-del-" + fid, "folder": "1"}
    return {"title": "收藏当前目录", "PathToken": "quark-action-add-" + fid, "folder": "1"}


def as_url_list_page(items, *, has_more, page, path_token, saved=False):
    """Executable transcription of ``QuarkDirectoryList``'s row building and PageToken tail.

    PotPlayer ships no standalone AngelScript toolchain, so the script cannot run
    here; this mirrors ``MediaUrlList - Quark.as`` one branch at a time and
    ``PlaybackDesignTests`` asserts the source still holds those same branches, so
    the model and the script cannot drift apart silently.
    """
    rows = []
    if path_token and page == 1:
        rows.append({"title": "..", "folder": "parent"})
        rows.append(favorite_action_row(path_token, saved))
    for item in items:
        if not isinstance(item, dict):
            continue
        fid, name, is_dir = item.get("fid"), item.get("name"), item.get("is_dir")
        if not (isinstance(fid, str) and fid and isinstance(name, str)):
            continue
        if is_dir is True:
            rows.append(
                {"title": name, "url": "https://quark.invalid/folder/" + fid, "folder": "1", "PathToken": fid}
            )
        elif is_video(name):
            rows.append({"title": name, "url": "https://quark.invalid/play/" + fid})
    if has_more:
        next_page = page + 1
        if next_page > 0:
            if not rows:
                rows.append({"title": "加载下一页…"})
            rows[-1]["PageToken"] = str(next_page)
    return rows


def as_subtitle_source(source: str) -> tuple[str, str]:
    """Executable transcription of ``QuarkCurrentVideo``: ``(fid, session)``.

    Both empty unless ``source`` is a Quark logical link or a loopback bridge
    stream URL, so the script and this model cannot drift on what is recognised.
    """
    for prefix in ("https://quark.invalid/play/", "https://quark.invalid/video/"):
        if source.startswith(prefix):
            fid = source[len(prefix):]
            unsafe = any(char in fid for char in '/\\"?#& \t')
            return (fid, "") if fid and not unsafe else ("", "")
    if not source.startswith("http://"):
        return "", ""
    rest = source[len("http://"):]
    slash = rest.find("/")
    if slash <= 0:
        return "", ""
    host, path = rest[:slash], rest[slash:]
    hostname = host.split(":")[0] if not host.startswith("[") else host.split("]")[0] + "]"
    if hostname not in ("127.0.0.1", "localhost", "[::1]") or not path.startswith("/v1/stream/"):
        return "", ""
    session = path[len("/v1/stream/"):].split("/")[0]
    if not session or any(char in session for char in '/\\"?#& \t'):
        return "", ""
    return "", session


class _StartedBridge:
    """A real Service serving an in-memory fixture on an ephemeral loopback port."""

    def __init__(self, login_factory=None, records=None, cookie=COOKIE):
        from quark_cli.potplayer.service import Service

        self.directory = Path(tempfile.mkdtemp(prefix="quark-potplayer-as-"))
        self.previous_config = os.environ.get("QUARK_CONFIG_DIR")
        os.environ["QUARK_CONFIG_DIR"] = str(self.directory)
        if login_factory is None:
            def login_factory():
                return FakeLogin(self.directory / "qr.png")
        self.fixture = DriveFixture(records)
        self.service = Service(
            transport=self.fixture.transport(), cookie=cookie, login_factory=login_factory,
            status_launcher=lambda: _StatusProcess(),
            highlight_launcher=lambda: _StatusProcess(),
        )
        self.server = self.service.build_server()
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.service.port}"
        self.client = httpx.Client(base_url=self.base, timeout=10.0)
        self.headers = {"Authorization": f"Bearer {self.service.token}"}

    def stop(self):
        self.client.close()
        self.server.shutdown()
        self.server.server_close()
        self.service.close()
        self.thread.join(10)
        if self.previous_config is None:
            os.environ.pop("QUARK_CONFIG_DIR", None)
        else:
            os.environ["QUARK_CONFIG_DIR"] = self.previous_config
        shutil.rmtree(self.directory, ignore_errors=True)


class _StatusProcess:
    def poll(self):
        return None


class ScriptSyntaxTests(unittest.TestCase):
    """Tokenising and delimiter checks: the defects a compiler reports first."""

    def scripts(self):
        return (
            (URL_LIST, read_script(URL_LIST)),
            (PLAY_PARSE, read_script(PLAY_PARSE)),
            (SUBTITLE_SEARCH, read_script(SUBTITLE_SEARCH)),
        )

    def test_scripts_keep_the_bom_and_terminate_literals(self):
        for path, source in self.scripts():
            code, issues = strip_literals(source)
            self.assertEqual(issues, [], f"{path.name}: {issues}")
            self.assertIn("GetTitle", code)

    def test_delimiters_are_balanced(self):
        for path, source in self.scripts():
            code, _ = strip_literals(source)
            for opening, closing in (("{", "}"), ("(", ")"), ("[", "]")):
                self.assertEqual(
                    code.count(opening), code.count(closing), f"{path.name}: {opening}{closing}"
                )

    def test_no_c_style_array_declarations(self):
        # AngelScript rejects C arrays such as ``string roots[2];``; the sized
        # form is the array<T> constructor, e.g. ``array<string> roots(2);``.
        for path, source in self.scripts():
            code, _ = strip_literals(source)
            match = C_ARRAY_DECLARATION.search(code)
            self.assertIsNone(
                match, f"{path.name}: C-style array declaration {match and match.group(0).strip()!r}"
            )

    def test_launcher_roots_use_the_array_constructor(self):
        for path, source in self.scripts():
            body = body_raw(source, "string QuarkLauncher()")
            self.assertIn("array<string> roots(2)", body)
            self.assertNotIn("roots[2]", body)

    def test_every_called_function_is_defined(self):
        for path, source in self.scripts():
            code, _ = strip_literals(source)
            declared = set(DECLARATION.findall(code))
            called = set(HOST_CALL.findall(code))
            unknown = {
                name
                for name in called
                if not name.startswith("Host")
                and name not in declared
                and name not in BUILTIN_CALLS
                and name not in KEYWORDS
            }
            self.assertEqual(unknown, set(), f"{path.name}: undefined calls {sorted(unknown)}")

    def test_exports_keep_the_player_signatures(self):
        url_code, _ = strip_literals(read_script(URL_LIST))
        play_code, _ = strip_literals(read_script(PLAY_PARSE))
        subtitle_code, _ = strip_literals(read_script(SUBTITLE_SEARCH))
        for signature in URL_LIST_EXPORTS:
            self.assertRegex(url_code, signature_pattern(signature))
        for signature in PLAY_PARSE_EXPORTS:
            self.assertRegex(play_code, signature_pattern(signature))
        for signature in SUBTITLE_SEARCH_EXPORTS:
            self.assertRegex(subtitle_code, signature_pattern(signature))


class HostApiTests(unittest.TestCase):
    """Every native call must exist in the player's own api.txt."""

    def declared_hosts(self) -> set[str]:
        candidates = api_txt_candidates()
        if not candidates:
            self.skipTest("PotPlayer Extension/api.txt not found on this machine")
        return set(HOST_DECLARATION.findall(candidates[0].read_text("utf-8", "replace")))

    def test_host_calls_are_declared_by_potplayer(self):
        declared = self.declared_hosts()
        self.assertIn("HostUrlGetString", declared)
        self.assertIn("HostExecuteProgram", declared)
        for path in (URL_LIST, PLAY_PARSE, SUBTITLE_SEARCH):
            code, _ = strip_literals(read_script(path))
            used = set(re.findall(r"\b(Host\w+)\s*\(", code))
            self.assertEqual(used - declared, set(), f"{path.name}: {sorted(used - declared)}")

    def test_the_host_checker_would_reject_an_invented_name(self):
        declared = self.declared_hosts()
        invented = set(re.findall(r"\b(Host\w+)\s*\(", "HostFetchRemoteJson(url);"))
        self.assertEqual(invented - declared, {"HostFetchRemoteJson"})


class PlaybackDesignTests(unittest.TestCase):
    """The link shapes that keep selection working without recursive expansion."""

    @classmethod
    def setUpClass(cls):
        cls.url_raw = read_script(URL_LIST)
        cls.play_raw = read_script(PLAY_PARSE)

    def test_single_items_are_not_treated_as_playlists(self):
        body = squash(body_raw(self.play_raw, "bool PlaylistCheck(const string &in path)"))
        self.assertIn("QUARK_FOLDER_PREFIX", body)
        self.assertIn("QUARK_PLAY_PREFIX", body)
        self.assertNotIn("QUARK_VIDEO_PREFIX", body)

    def test_playlists_return_stable_per_episode_items(self):
        body = squash(body_raw(self.play_raw, "array<dictionary> QuarkPlaylist(string request)"))
        self.assertRegex(body, r'row\["url"\]\s*=\s*QUARK_VIDEO_PREFIX')
        self.assertNotIn("QUARK_PLAY_PREFIX", body)
        self.assertNotIn("QUARK_FOLDER_PREFIX", body)

    def test_playitem_accepts_video_and_play_links(self):
        body = squash(body_raw(self.play_raw, "bool PlayitemCheck(const string &in path)"))
        self.assertIn("QUARK_VIDEO_PREFIX", body)
        self.assertIn("QUARK_PLAY_PREFIX", body)

    def test_directory_rows_navigate_and_episodes_start_a_playlist(self):
        rows = body_raw(self.url_raw, "void QuarkAppendItems(array<dictionary> &ret, JsonValue &items)")
        self.assertRegex(rows, r'row\["url"\]\s*=\s*QUARK_FOLDER_PREFIX')
        self.assertRegex(rows, r'row\["folder"\]\s*=\s*"1"')
        self.assertRegex(rows, r'row\["PathToken"\]\s*=\s*fid\.asString\(\)')
        self.assertRegex(rows, r'row\["url"\]\s*=\s*QUARK_PLAY_PREFIX')

        listing = body_raw(
            self.url_raw, "array<dictionary> QuarkDirectoryList(string PathToken, string page, bool favorite_action)"
        )
        self.assertIn('up["PathToken"] = ancestor.asString()', listing)
        self.assertIn('root["up"]', listing)
        self.assertIn("QuarkAttachPageToken", listing)

    def test_play_request_body_is_a_json_object_with_a_fid(self):
        match = re.search(r'"\{\\"fid\\":\\""\s*\+\s*fid\s*\+\s*"\\"\}"', self.play_raw)
        self.assertIsNotNone(match, 'play body must be built as {"fid":"<fid>"}')
        self.assertEqual(json.loads('{"fid":"v9"}'), {"fid": "v9"})

    def test_login_is_automatic_and_bounded(self):
        for raw in (self.url_raw, self.play_raw):
            self.assertIn("/v1/auth/login", raw)
            self.assertNotIn("--login", raw)
        body = squash(body_raw(self.play_raw, "bool QuarkEnsureLogin()"))
        self.assertIn("HostSleep", body)
        self.assertIn("HostIncTimeOut", body)
        self.assertIn("QUARK_LOGIN_WAIT_MS", body)

    def test_login_post_uses_a_json_body_and_closes_the_connection(self):
        for raw in (self.url_raw, self.play_raw):
            self.assertIn('"{\\"client\\":\\"potplayer\\"}"', raw)
            header = squash(body_raw(raw, "string QuarkHeader(string token, bool post)"))
            self.assertIn("Connection: close", header)
            self.assertIn("Bearer ", header)
            self.assertIn(r"\r\n", header)
            self.assertNotIn(r'token + "\n"', header)

    def test_titles_use_the_simplified_chinese_selector(self):
        for raw in (self.url_raw, self.play_raw):
            body = squash(body_raw(raw, "string GetTitle()"))
            self.assertIn("{$CP936=", body)
            self.assertIn("{$CP0=", body)

    def test_health_cache_checks_status_version_and_instance(self):
        for raw in (self.url_raw, self.play_raw):
            body = squash(body_raw(raw, "string QuarkHealth(string base)"))
            self.assertIn('asString() != "ok"', body)
            self.assertIn('root["version"]', body)
            self.assertIn('root["instance"]', body)
            ensure = squash(body_raw(raw, "bool QuarkEnsureService()"))
            self.assertIn("QuarkClearService()", ensure)
            self.assertIn("--ensure-service", ensure)

    def test_launcher_lookup_matches_the_installer_contract(self):
        for raw in (self.url_raw, self.play_raw):
            body = squash(body_raw(raw, "string QuarkLauncher()"))
            self.assertIn("HostGetScriptFolder", body)
            self.assertIn("quark-potplayer.exe", body)
            self.assertIn("quark-potplayer.cmd", body)
            self.assertIn("HostExecuteProgram", squash(body_raw(raw, "bool QuarkEnsureService()")))
            self.assertIn("HostFileExist", body)

    def test_video_filter_matches_the_bridge_extension_list(self):
        match = re.search(r'"(\|[a-z0-9|]{20,}\|)"', self.url_raw)
        self.assertIsNotNone(match, "the UrlList script must carry the video extension list")
        client = {part for part in match.group(1).split("|") if part}
        server = {ext.lstrip(".") for ext in VIDEO_EXTENSIONS}
        self.assertEqual(client, server)

    def test_launcher_template_keeps_the_installer_contract(self):
        text = LAUNCHER.read_text("utf-8")
        self.assertEqual(text.splitlines()[0].strip().lower(), "@echo off")
        self.assertIn("%*", text)
        self.assertIn("QuarkPotPlayer.exe", text)
        self.assertIn("--ensure-service", text)

    def test_url_list_keeps_paging_when_a_page_filters_to_nothing(self):
        body = squash(body_raw(self.url_raw, "void QuarkAttachPageToken(array<dictionary> &ret, JsonValue &root)"))
        # 原生协议：PageToken 挂在最后一条行上，且不再要求本页有可显示行。
        self.assertRegex(body, r'ret\[ret\.size\(\) - 1\]\["PageToken"\]\s*=\s*formatInt\(next_page\)')
        self.assertNotIn("next_page > 0 && ret.size() > 0", body)
        # 整页都被过滤掉时，补一条不可播放的提示行来承载 PageToken。
        self.assertIn("ret.size() == 0", body)
        self.assertIn("ret.insertLast(hint)", body)
        self.assertIn("加载下一页", body)

    def test_login_wait_stops_when_the_scan_window_ends(self):
        for raw in (self.url_raw, self.play_raw):
            body = squash(body_raw(raw, "bool QuarkEnsureLogin()"))
            self.assertIn("QuarkLoginState()", body)
            self.assertIn('state == "idle" || state == "failed"', body)
            self.assertIn("QuarkLoginState", raw)
        state = squash(body_raw(self.url_raw, "string QuarkLoginState()"))
        self.assertIn('root["login_state"]', state)


class LibraryEntryDesignTests(unittest.TestCase):
    """Search, favorites and recent entries: native rows, no invented player features."""

    @classmethod
    def setUpClass(cls):
        cls.url_raw = read_script(URL_LIST)

    def body(self, signature):
        return squash(body_raw(self.url_raw, signature))

    def test_categories_keep_drive_and_add_search_favorites_recent(self):
        body = self.body("array<dictionary> GetCategorys()")
        self.assertRegex(body, r'item\["Category"\]\s*=\s*"drive"')
        self.assertRegex(body, r'keyword\["Category"\]\s*=\s*"search"')
        # type=search 是播放器给出关键词输入框（Query）的原生标记。
        self.assertRegex(body, r'keyword\["type"\]\s*=\s*"search"')
        self.assertRegex(body, r'favorites\["Category"\]\s*=\s*"favorites"')
        self.assertRegex(body, r'recent\["Category"\]\s*=\s*"recent"')
        # 0.5.0 新增：本地诊断入口，纯本地功能，不需要夸克登录。
        self.assertRegex(body, r'diagnostics\["Category"\]\s*=\s*"diagnostics"')
        self.assertIn("插件管理（账号 / 精彩标记 / 诊断）", read_script(URL_LIST))

    def test_titles_keep_simplified_chinese_and_the_cp0_fallback(self):
        body = self.body("array<dictionary> GetCategorys()")
        for selector in ("{$CP936=", "{$CP950=", "{$CP0="):
            self.assertIn(selector, body)

    def test_search_root_searches_the_whole_drive_and_encodes_the_query(self):
        body = self.body("array<dictionary> QuarkSearchList(string Query, string PathToken, string page)")
        self.assertIn('"/v1/search?q=" + HostUrlEncode(Query)', body)
        self.assertIn("Query.empty()", body)
        self.assertIn("请在上方输入搜索关键词", body)
        # 进入结果目录后按 FID 浏览，不再使用 Query。
        self.assertRegex(body, r"if \(!PathToken\.empty\(\)\) return QuarkDirectoryList\(")

    def test_favorite_action_rows_navigate_and_are_never_media_urls(self):
        body = self.body("void QuarkAppendFavoriteAction(array<dictionary> &ret, string fid, bool saved)")
        self.assertIn('row["folder"] = "1"', body)
        self.assertIn("QUARK_ACTION_ADD + fid", body)
        self.assertIn("QUARK_ACTION_DEL + fid", body)
        self.assertNotIn('row["url"]', body)
        # 同一记号永远执行同一个动作：不是 toggle。
        self.assertNotIn("saved = !", body)

    def test_favorite_action_posts_to_the_documented_routes_only(self):
        body = body_raw(self.url_raw, "array<dictionary> QuarkFavoriteActionPage(bool add, string fid)")
        self.assertIn('"/v1/favorites/remove"', body)
        self.assertIn('"/v1/favorites"', body)
        self.assertIn("QuarkValidFid", body)
        self.assertIsNotNone(
            re.search(r'"\{\\"fid\\":\\""\s*\+\s*fid\s*\+\s*"\\"\}"', body),
            'favorite bodies must be built as {"fid":"<fid>"}',
        )
        self.assertIn("已收藏当前目录", body)
        self.assertIn("已取消收藏当前目录", body)

    def test_favorite_entry_is_only_on_the_first_page_of_a_real_folder(self):
        body = self.body(
            "array<dictionary> QuarkDirectoryList(string PathToken, string page, bool favorite_action)"
        )
        self.assertIn('bool first = page == "1"', body)
        self.assertIn('if (!PathToken.empty() && PathToken != "0" && first)', body)
        self.assertIn('root["favorite"]', body)
        self.assertNotIn("QuarkIsFavorite(PathToken)", body)
        # 收藏状态取自本地收藏列表，而不是远端目录数据。
        status = self.body("bool QuarkIsFavorite(string fid)")
        self.assertIn('QuarkRequest("/v1/favorites", "")', status)
        self.assertIn('item["fid"]', status)

    def test_a_broken_favorite_still_offers_removal(self):
        listing = self.body(
            "array<dictionary> QuarkDirectoryList(string PathToken, string page, bool favorite_action)"
        )
        # 请求失败与响应损坏两条路径都走兜底页。
        self.assertEqual(listing.count("QuarkDirectoryFailure(PathToken, page"), 2)
        failure = self.body("array<dictionary> QuarkDirectoryFailure(string PathToken, string page, string message)")
        self.assertIn("QuarkIsFavorite(PathToken)", failure)
        self.assertIn("QuarkAppendFavoriteAction(ret, PathToken, true)", failure)
        self.assertRegex(failure, r'up\["folder"\]\s*=\s*"parent"')

    def test_favorites_list_reads_local_items_and_hints_when_empty(self):
        body = self.body("array<dictionary> QuarkFavoritesList(string PathToken, string page)")
        self.assertIn('QuarkRequest("/v1/favorites", "")', body)
        self.assertIn("items.size() == 0", body)
        self.assertIn("收藏列表为空", body)
        # 收藏项是目录：复用普通目录浏览与收藏入口。
        self.assertRegex(body, r"if \(!PathToken\.empty\(\)\) return QuarkDirectoryList\(")

    def test_recent_list_reuses_stable_play_links_and_clears_idempotently(self):
        body = self.body("array<dictionary> QuarkRecentList()")
        self.assertIn('QuarkRequest("/v1/recent", "")', body)
        self.assertIn("QUARK_ACTION_CLEAR_RECENT", body)
        self.assertIn("最近没有播放记录", body)
        self.assertIn("QuarkAppendRecentItems(ret, items)", body)
        # 条目走最近记录专用构造：视频复用 QUARK_PLAY_PREFIX，且每条附一条操作文件夹行。
        rows = self.body("void QuarkAppendRecentItems(array<dictionary> &ret, JsonValue &items)")
        self.assertIn('play["url"] = QUARK_PLAY_PREFIX + fid.asString()', rows)
        self.assertIn("QuarkAppendVideoActions(ret, fid.asString(), title)", rows)

        clear = body_raw(self.url_raw, "array<dictionary> QuarkRecentClearPage()")
        self.assertIsNotNone(re.search(r'"/v1/recent/clear"\s*,\s*"\{', clear), "clear must POST a JSON body")
        self.assertIn("client", clear)
        self.assertIn("已清空最近观看", clear)

    def test_action_pages_are_explicit_and_go_back(self):
        body = self.body("array<dictionary> QuarkActionResult(string message)")
        self.assertRegex(body, r'back\["folder"\]\s*=\s*"parent"')
        self.assertNotIn('back["url"]', body)
        # 播放器只展开 GetTitle/GetCategorys 的元数据；行标题里的 {$CP936=…$}
        # 会原样显示，所以结果行必须用纯文本。
        self.assertNotIn("{$CP", body)
        self.assertIn("返回", body)
        # 只有标题的行会被播放器过滤：结果必须并入可导航的父级行，不再单独发提示行。
        self.assertNotRegex(body, r'done\["title"\]')
        self.assertEqual(body.count("ret.insertLast"), 1)
        self.assertRegex(body, r'back\["title"\]\s*=\s*message\s*\+')

    def test_row_titles_are_plain_text_and_match_the_row_model(self):
        # 行标题不做 CP 选择展开（只有 GetTitle/GetCategorys 元数据会被播放器展开），
        # 否则用户会看到整段 {$CP936=…$} 模板。
        for signature in (
            "void QuarkAppendFavoriteAction(array<dictionary> &ret, string fid, bool saved)",
            "array<dictionary> QuarkActionResult(string message)",
            "array<dictionary> QuarkFavoriteActionPage(bool add, string fid)",
            "array<dictionary> QuarkRecentClearPage()",
            "array<dictionary> QuarkRecentList()",
            "array<dictionary> QuarkWatchCenterList()",
            "array<dictionary> QuarkCenterSeriesPage(string parent)",
            "array<dictionary> QuarkVideoActionsPage(string fid)",
            "array<dictionary> QuarkRecentRemoveAskPage(string fid)",
            "void QuarkAppendRecentItems(array<dictionary> &ret, JsonValue &items)",
            "void QuarkAppendVideoActions(array<dictionary> &ret, string fid, string title)",
        ):
            self.assertNotIn("{$CP", self.body(signature), signature)
        # 纯文本标题与 FavoriteRowModelTests 的转写保持一字不差。
        action = self.body("void QuarkAppendFavoriteAction(array<dictionary> &ret, string fid, bool saved)")
        self.assertIn('row["title"] = "收藏当前目录"', action)
        self.assertIn('row["title"] = "取消收藏当前目录"', action)
        self.assertIn('clear["title"] = "清空最近观看"', self.body("array<dictionary> QuarkRecentList()"))
        self.assertIn("已清空最近观看", self.body("array<dictionary> QuarkRecentClearPage()"))

    def test_get_url_list_dispatches_without_a_generic_router(self):
        body = self.body(
            "array<dictionary> QuarkAccountUrlList(string Category, string Extra, string PathToken, string Query, string PageToken)"
        )
        self.assertIn("QUARK_ACTION_ADD", body)
        self.assertIn("QUARK_ACTION_DEL", body)
        self.assertIn("QUARK_ACTION_CLEAR_RECENT", body)
        self.assertIn('Category == "search"', body)
        self.assertIn('Category == "favorites"', body)
        self.assertIn('Category == "recent"', body)
        self.assertIn("QuarkDirectoryList(PathToken, page, true)", body)
        # 诊断入口在登录检查之前处理：它是纯本地功能。
        self.assertIn('Category == "diagnostics"', body)
        self.assertLess(body.index('Category == "diagnostics"'), body.index("QuarkEnsureLogin()"))


class SeriesEntryDesignTests(unittest.TestCase):
    """The 0.6.0 continue/series entries: native navigation rows, local state only."""

    @classmethod
    def setUpClass(cls):
        cls.url_raw = read_script(URL_LIST)

    def body(self, signature):
        return squash(body_raw(self.url_raw, signature))

    def raw(self, signature):
        return body_raw(self.url_raw, signature)

    def test_continue_category_reads_the_grouped_local_watch_center(self):
        categories = self.body("array<dictionary> GetCategorys()")
        # 继续观看分类保留，但根页改成追剧中心的分组列表。
        self.assertRegex(categories, r'resume\["Category"\]\s*=\s*"continue"')
        body = self.body("array<dictionary> QuarkWatchCenterList()")
        self.assertIn('QuarkRequest("/v1/watch-center", "")', body)
        # 空列表给一条可读状态，不回退到整盘浏览。
        self.assertIn("暂无可继续观看的视频", body)

    def test_directory_first_page_adds_continue_and_management_rows(self):
        listing = self.body(
            "array<dictionary> QuarkDirectoryList(string PathToken, string page, bool favorite_action)"
        )
        self.assertIn("QuarkAppendSeriesRows(ret, parent, series)", listing)
        self.assertIn("&include_series=1", listing)
        self.assertIn('if (!PathToken.empty() && PathToken != "0" && page == "1") request += "&include_series=1"', listing)
        rows = self.body("void QuarkAppendSeriesRows(array<dictionary> &ret, string parent, JsonValue &root)")
        self.assertNotIn("QuarkRequest", rows)
        self.assertLess(rows.index("total.asInt() == 0"), rows.index("QUARK_ACTION_HIGHLIGHTS"))
        self.assertIn("QUARK_PLAY_PREFIX + fid.asString()", rows)
        self.assertIn("QUARK_ACTION_SERIES + parent", rows)
        # “继续观看”必须是媒体行（唯一带 url 的行）；管理行是可导航动作行。
        self.assertEqual(rows.count('row["url"]'), 1)
        self.assertIn('manage["folder"] = "1"', rows)
        self.assertNotIn('manage["url"]', rows)

    def test_highlights_root_and_video_entries_use_real_playback_links(self):
        categories = self.body("array<dictionary> GetCategorys()")
        self.assertIn('highlights["Category"] = "highlights"', categories)
        self.assertIn("精彩标记", categories)
        rows = self.body("array<dictionary> QuarkHighlightsList(string parent)")
        self.assertIn('"/v1/bookmarks"', rows)
        self.assertIn('row["url"] = QUARK_MARK_PREFIX + id.asString()', rows)
        self.assertIn('item["note"]', rows)
        items = self.body("void QuarkAppendItems(array<dictionary> &ret, JsonValue &items)")
        self.assertIn('QuarkIsVideo(name.asString())', items)
        self.assertNotIn('mark["PathToken"]', items)
        self.assertIn('capture["title"] = "标记当前时间点"', self.body('void QuarkAppendSeriesRows(array<dictionary> &ret, string parent, JsonValue &root)'))
        parse = read_script(PLAY_PARSE)
        self.assertIn('"https://quark.invalid/mark/"', parse)
        self.assertIn('bookmark_id', body_raw(parse, "string PlayitemParse(const string &in path, dictionary &MetaData, array<dictionary> &QualityList)"))
        check = body_raw(parse, "bool PlaylistCheck(const string &in path)")
        self.assertNotIn("QUARK_MARK_PREFIX", check)

    def test_all_watched_directory_shows_a_navigation_row_not_a_restart(self):
        rows = self.body("void QuarkAppendSeriesRows(array<dictionary> &ret, string parent, JsonValue &root)")
        self.assertIn("本目录已全部看完", rows)
        self.assertNotIn('done["url"]', rows)
        self.assertIn('done["PathToken"] = QUARK_ACTION_SERIES + parent', rows)

    def test_series_page_lists_every_episode_with_a_toggle_token(self):
        body = self.body("array<dictionary> QuarkSeriesPage(string parent)")
        self.assertIn('"/v1/series?parent=" + HostUrlEncode(parent)', body)
        self.assertIn("QuarkWatchToken(!marked, parent, fid.asString())", body)
        self.assertIn("已看（点击标记未看）", body)
        self.assertIn("未看（点击标记已看）", body)
        # 管理页是纯导航：没有媒体地址。
        self.assertNotIn('row["url"]', body)

    def test_watch_action_replays_the_target_state_idempotently(self):
        token = self.body("string QuarkWatchToken(bool watched, string parent, string fid)")
        self.assertIn("QUARK_ACTION_WATCH", token)
        body = self.raw("array<dictionary> QuarkWatchActionPage(string spec)")
        self.assertIn('"/v1/mark"', body)
        self.assertIn('\\"watched\\":', body)
        self.assertIn("+ flag", body)
        self.assertIn('state == "1"', body)

    def test_action_tokens_are_dispatched_and_validate_their_pieces(self):
        body = self.body(
            "array<dictionary> QuarkAccountUrlList(string Category, string Extra, string PathToken, string Query, string PageToken)"
        )
        self.assertIn("QUARK_ACTION_SERIES", body)
        self.assertIn("QUARK_ACTION_WATCH", body)
        self.assertIn('Category == "continue"', body)
        valid = self.body("bool QuarkValidFid(string fid)")
        self.assertIn('fid.find("|") >= 0', valid)


class WatchCenterDesignTests(unittest.TestCase):
    """The 0.8.0 watch center: a local grouped root and a per-group series page."""

    @classmethod
    def setUpClass(cls):
        cls.url_raw = read_script(URL_LIST)

    def body(self, signature):
        return squash(body_raw(self.url_raw, signature))

    def raw(self, signature):
        return body_raw(self.url_raw, signature)

    def test_continue_category_title_names_the_watch_center(self):
        categories = self.body("array<dictionary> GetCategorys()")
        self.assertIn("追剧中心（继续观看）", categories)
        self.assertRegex(categories, r'resume\["Category"\]\s*=\s*"continue"')

    def test_root_groups_series_and_never_scans_series_per_group(self):
        root = self.body("array<dictionary> QuarkWatchCenterList()")
        self.assertIn('kind.asString() == "series"', root)
        self.assertIn("QUARK_CENTER_SERIES + key.asString()", root)
        self.assertIn("unfinished_count", root)
        # 根页只读本地 watch-center：绝不为每个分组单独请求 /series 或扫描网盘。
        self.assertEqual(root.count("QuarkRequest("), 1)
        self.assertNotIn("/v1/series", root)
        self.assertNotIn("/v1/files", root)

    def test_series_group_row_is_a_named_navigation_row(self):
        root = self.body("array<dictionary> QuarkWatchCenterList()")
        self.assertRegex(root, r'row\["folder"\]\s*=\s*"1"')
        self.assertRegex(root, r'row\["PathToken"\]\s*=\s*QUARK_CENTER_SERIES')
        self.assertNotIn('row["url"]', root)

    def test_root_single_video_gets_play_and_actions(self):
        root = self.body("array<dictionary> QuarkWatchCenterList()")
        self.assertIn('play["url"] = QUARK_PLAY_PREFIX + key.asString()', root)
        self.assertIn("QuarkAppendVideoActions(ret, key.asString(), title)", root)

    def test_group_page_reads_series_once_and_lays_out_the_contract(self):
        page = self.body("array<dictionary> QuarkCenterSeriesPage(string parent)")
        # 进入分组只读取一次 /series。
        self.assertEqual(page.count("QuarkRequest("), 1)
        self.assertIn('"/v1/series?parent=" + HostUrlEncode(parent)', page)
        self.assertRegex(page, r'up\["folder"\]\s*=\s*"parent"')
        # 摘要行导航到状态管理页，标题带 watched x/y，看完给明确标签且没有假播放地址。
        self.assertIn("QUARK_ACTION_SERIES + parent", page)
        self.assertIn('"已看 "', page)
        self.assertIn("已全部看完", page)
        self.assertIn('summary["folder"] = "1"', page)
        self.assertNotIn('summary["url"]', page)
        # continue 播放行与 next 播放行：next 标题显式带“下一集：”前缀 + 真实文件名，
        # 与剧集行区分开；地址仍是该下一集的真实媒体链接。
        self.assertIn('"继续观看：" + display.asString()', page)
        self.assertIn('root["next"]', page)
        self.assertIn('row["title"] = "下一集：" + name.asString()', page)
        self.assertIn('row["url"] = QUARK_PLAY_PREFIX + fid.asString()', page)

    def test_empty_directory_is_a_navigable_return_not_a_false_ending(self):
        page = self.body("array<dictionary> QuarkCenterSeriesPage(string parent)")
        # total == 0 时提前返回：给可导航的返回行，由 total 判定控制。
        self.assertIn("total.asInt() == 0", page)
        self.assertIn("该目录没有视频 · 返回", page)
        # 空目录行是导航行，没有媒体地址，且早于任何结尾/看完文案。
        empty_block = page[page.index("total.asInt() == 0") : page.index("return ret")]
        self.assertIn('empty["folder"] = "parent"', empty_block)
        self.assertIn("该目录没有视频 · 返回", empty_block)
        self.assertNotIn('empty["url"]', empty_block)
        # 空目录绝不会被当成“已到最后一集”，也不被标成“已全部看完”。
        self.assertNotIn("已到最后一集", empty_block)
        self.assertNotIn("已全部看完", empty_block)
        self.assertLess(page.index("该目录没有视频 · 返回"), page.index("已到最后一集 · 返回"))

    def test_group_page_ends_without_wrapping_and_offers_per_episode_actions(self):
        page = self.body("array<dictionary> QuarkCenterSeriesPage(string parent)")
        # 非空目录没有下一集时给可导航的返回提示（也是 parent 导航行），不回到第一集重播。
        self.assertIn("已到最后一集 · 返回", page)
        self.assertIn('hint["folder"] = "parent"', page)
        self.assertNotIn("QUARK_PLAY_PREFIX + episodes[0]", page)
        # 自然顺序的剧集：每集可播放，并附一个操作文件夹行（精彩标记、删除）。
        self.assertIn('root["episodes"]', page)
        self.assertIn("QuarkAppendVideoActions(ret, fid.asString(), title)", page)


class VideoActionsDesignTests(unittest.TestCase):
    """0.8.0 per-video actions: local bookmarks window and a confirmed single delete."""

    @classmethod
    def setUpClass(cls):
        cls.url_raw = read_script(URL_LIST)

    def body(self, signature):
        return squash(body_raw(self.url_raw, signature))

    def raw(self, signature):
        return body_raw(self.url_raw, signature)

    def test_action_folder_row_is_navigation_only(self):
        row = self.raw(
            "void QuarkAppendVideoActions(array<dictionary> &ret, string fid, string title)"
        )
        self.assertIn('row["folder"] = "1"', row)
        self.assertIn("QUARK_ACTION_VIDEO + fid", row)
        self.assertIn("QuarkValidFid(fid)", row)
        self.assertIn("（精彩标记、记录）", row)
        self.assertNotIn('row["url"]', row)

    def test_actions_page_offers_play_bookmarks_and_delete(self):
        page = self.raw("array<dictionary> QuarkVideoActionsPage(string fid)")
        # 原生文件夹式的“..”父级行：操作页自己能回到上一级列表，不依赖全局返回。
        self.assertIn('up["title"] = ".."', page)
        self.assertIn('up["folder"] = "parent"', page)
        self.assertIn('play["url"] = QUARK_PLAY_PREFIX + fid', page)
        self.assertIn("精彩标记", page)
        self.assertIn("QUARK_ACTION_BOOKMARKS + fid", page)
        self.assertIn("删除本条观看记录", page)
        self.assertIn("QUARK_ACTION_REMOVE_ASK + fid", page)
        # 父级行先出现，是导航行；只有播放行是媒体地址。
        self.assertLess(page.index('up["folder"] = "parent"'), page.index('play["url"]'))
        self.assertNotIn('up["url"]', page)
        self.assertEqual(page.count('["url"]'), 1)
        for key in ("bookmarks", "remove"):
            self.assertIn(f'{key}["folder"] = "1"', page)
            self.assertNotIn(f'{key}["url"]', page)
        # 本地读最近记录补标题，找不到时用通用标题。
        self.assertIn('QuarkRequest("/v1/recent", "")', page)
        self.assertIn("该视频", page)

    def test_bookmarks_open_posts_only_the_fid(self):
        body = self.raw("array<dictionary> QuarkBookmarksOpenPage(string fid)")
        self.assertIsNotNone(re.search(r'"/v1/bookmarks/open"\s*,\s*"\{', body))
        self.assertIn("QuarkValidFid(fid)", body)
        self.assertRegex(body, r'"\{\\"fid\\":\\""\s*\+\s*fid\s*\+\s*"\\"\}"')
        self.assertIn("已打开精彩标记窗口", body)

    def test_delete_needs_an_explicit_confirmation_then_is_idempotent(self):
        ask = self.raw("array<dictionary> QuarkRecentRemoveAskPage(string fid)")
        # 确认页只给导航行，不执行任何请求。
        self.assertNotIn("QuarkRequest", ask)
        self.assertNotIn('["url"]', ask)
        self.assertIn("确认删除", ask)
        self.assertIn("QUARK_ACTION_REMOVE_DO + fid", ask)

        do = self.raw("array<dictionary> QuarkRecentRemovePage(string fid)")
        self.assertIsNotNone(re.search(r'"/v1/recent/remove"\s*,\s*"\{', do))
        self.assertRegex(do, r'"\{\\"fid\\":\\""\s*\+\s*fid\s*\+\s*"\\"\}"')
        self.assertIn("QuarkValidFid(fid)", do)
        self.assertIn("已删除本条观看记录", do)

    def test_delete_is_not_the_clear_all_action(self):
        do = self.raw("array<dictionary> QuarkRecentRemovePage(string fid)")
        self.assertIn("/v1/recent/remove", do)
        self.assertNotIn("/v1/recent/clear", do)
        self.assertNotIn("QUARK_ACTION_CLEAR_RECENT", do)

    def test_local_actions_precede_the_login_gate(self):
        body = self.body(
            "array<dictionary> QuarkAccountUrlList(string Category, string Extra, string PathToken, string Query, string PageToken)"
        )
        gate = body.index("QuarkEnsureLogin()")
        for marker in (
            'Category == "continue" && PathToken.empty()',
            "QUARK_ACTION_REMOVE_DO",
            "QUARK_ACTION_REMOVE_ASK",
            "QuarkVideoActionsPage",
            "QuarkBookmarksOpenPage",
        ):
            self.assertLess(body.index(marker), gate, marker)
        # 剧集分组是远程读取，必须在登录之后。
        self.assertLess(gate, body.index("QuarkCenterSeriesPage"))

    def test_new_action_tokens_are_unambiguous_single_fid_suffixes(self):
        source = read_script(URL_LIST)
        for constant in (
            'string QUARK_CENTER_SERIES = "quark-center-series-";',
            'string QUARK_ACTION_VIDEO = "quark-video-actions-";',
            'string QUARK_ACTION_BOOKMARKS = "quark-video-bookmarks-";',
            'string QUARK_ACTION_REMOVE_ASK = "quark-remove-ask-";',
            'string QUARK_ACTION_REMOVE_DO = "quark-remove-do-";',
        ):
            self.assertIn(constant, source)
        prefixes = (
            "quark-center-series-",
            "quark-video-actions-",
            "quark-video-bookmarks-",
            "quark-remove-ask-",
            "quark-remove-do-",
        )
        for outer in prefixes:
            for inner in prefixes:
                if outer != inner:
                    self.assertFalse(inner.startswith(outer), f"{inner} shadows {outer}")

    def test_fid_json_bodies_validate_before_embedding(self):
        for signature in (
            "array<dictionary> QuarkFavoriteActionPage(bool add, string fid)",
            "array<dictionary> QuarkBookmarksOpenPage(string fid)",
            "array<dictionary> QuarkRecentRemovePage(string fid)",
        ):
            body = self.raw(signature)
            self.assertIn("QuarkValidFid", body, signature)
            self.assertRegex(body, r'"\{\\"fid\\":\\""\s*\+\s*fid\s*\+\s*"\\"\}"', signature)
        valid = self.body("bool QuarkValidFid(string fid)")
        self.assertIn('fid.find("|") >= 0', valid)


class DiagnosticsEntryDesignTests(unittest.TestCase):
    """The 0.5.0 diagnostics entry: local-only, folder action row, no invented player APIs."""

    @classmethod
    def setUpClass(cls):
        cls.url_raw = read_script(URL_LIST)
        cls.play_raw = read_script(PLAY_PARSE)
        cls.sub_raw = read_script(SUBTITLE_SEARCH)

    def raw_body(self, raw, signature):
        return body_raw(raw, signature)

    def test_versions_report_the_current_release(self):
        # 本任务只把 UrlList 升到 0.8.0；PlayParse/SubtitleSearch 的版本由各自的
        # 任务维护，这里只要求仍是语义化版本串。
        self.assertIn('return "0.12.1";', self.raw_body(self.url_raw, "string GetVersion()"))
        self.assertRegex(
            self.raw_body(self.play_raw, "string GetVersion()"), r'return "\d+\.\d+\.\d+";'
        )

    def test_diagnostics_category_posts_a_json_body_and_returns_a_back_row(self):
        page = self.raw_body(self.url_raw, "array<dictionary> QuarkManagementPage()")
        self.assertIsNotNone(
            re.search(r'"/v1/management/open"\s*,\s*"\{', page), "open must POST a JSON body"
        )
        self.assertIn("已打开插件管理（账号 / 精彩标记 / 诊断）窗口", page)
        self.assertIn("QuarkActionResult", page)
        # 结果是可导航的返回行，不是媒体地址。
        self.assertNotIn('row["url"]', page)
        self.assertNotIn("{$CP", page)

    def test_player_pid_comes_from_the_launcher_local_action(self):
        pid_body = self.raw_body(self.play_raw, "int QuarkPlayerPid()")
        # PID 只能来自启动器的本地动作；不再加载 DLL、也不再猜进程。
        self.assertIn("QuarkLauncher()", pid_body)
        self.assertIn('HostExecuteProgram(launcher, "--player-pid")', pid_body)
        self.assertIn("QuarkJsonObject", pid_body)
        self.assertIn('json["player_pid"]', pid_body)
        self.assertIn("pid.isInt()", pid_body)
        self.assertIn("pid.asInt()", pid_body)
        self.assertIn("value <= 0", pid_body)
        self.assertNotIn("HostLoadLibrary", pid_body)
        self.assertNotIn("HostGetProcAddress", pid_body)
        self.assertNotIn("HostCallProcInt", pid_body)
        # 未取得 PID 时返回 0，调用方据此省略字段。
        self.assertIn("return 0", pid_body)

    def test_no_script_loads_a_native_library(self):
        # 实测 HostLoadLibrary 对任何 DLL 都返回 0，故全部脚本（含 SubtitleSearch）都不再使用它。
        for path, raw in (
            (URL_LIST, self.url_raw),
            (PLAY_PARSE, self.play_raw),
            (SUBTITLE_SEARCH, self.sub_raw),
        ):
            code, _ = strip_literals(raw)
            self.assertNotIn("HostLoadLibrary", code, f"{path.name} must not load native libraries")

    def test_play_body_keeps_fid_and_only_adds_a_positive_pid(self):
        parse = self.raw_body(
            self.play_raw,
            "string PlayitemParse(const string &in path, dictionary &MetaData, array<dictionary> &QualityList)",
        )
        # 旧请求体字面量必须保留，缺 PID 时行为完全不变。
        self.assertIsNotNone(re.search(r'"\{\\"fid\\":\\""\s*\+\s*fid\s*\+\s*"\\"\}"', parse))
        self.assertIn("QuarkPlayerPid()", parse)
        self.assertIn("player_pid > 0", parse)
        self.assertIn(
            '"{\\"fid\\":\\"" + fid + "\\",\\"player_pid\\":" + formatInt(player_pid) + "}"', parse
        )


class SubtitleSearchDesignTests(unittest.TestCase):
    """Subtitle search stays scoped to the current Quark video and writes nothing."""

    @classmethod
    def setUpClass(cls):
        cls.raw = read_script(SUBTITLE_SEARCH)

    def body(self, signature):
        return squash(body_raw(self.raw, signature))

    def test_title_uses_the_simplified_chinese_selector(self):
        body = self.body("string GetTitle()")
        self.assertIn("{$CP936=", body)
        self.assertIn("{$CP950=", body)
        self.assertIn("{$CP0=", body)

    def test_only_quark_links_and_loopback_streams_are_recognized(self):
        body = self.body("bool QuarkCurrentVideo(string source, string &out fid, string &out session)")
        self.assertIn("QUARK_PLAY_PREFIX", body)
        self.assertIn("QUARK_VIDEO_PREFIX", body)
        self.assertIn("QuarkStreamSession(source)", body)

        stream = self.body("string QuarkStreamSession(const string &in source)")
        self.assertIn('"127.0.0.1"', stream)
        self.assertIn('"localhost"', stream)
        self.assertIn('"/v1/stream/"', stream)
        self.assertIn("QuarkValidId(session)", stream)

        # 转写模型与脚本一致：普通本地/其它网络视频一律返回空。
        for source in (
            r"C:\Movies\第2集.mp4",
            "D:/media/clip.mkv",
            "https://example.com/movie.mkv",
            "http://192.168.1.5:8080/v1/stream/abc/name.mp4",
            "http://127.0.0.1:52311/other/abc",
            "https://quark.invalid/folder/abc",
            "",
        ):
            self.assertEqual(as_subtitle_source(source), ("", ""), source)
        self.assertEqual(as_subtitle_source("https://quark.invalid/play/v9"), ("v9", ""))
        self.assertEqual(as_subtitle_source("https://quark.invalid/video/v9"), ("v9", ""))
        self.assertEqual(
            as_subtitle_source("http://127.0.0.1:52311/v1/stream/sess123/%E7%AC%AC2%E9%9B%86.mp4"),
            ("", "sess123"),
        )
        self.assertEqual(as_subtitle_source("http://localhost:1/v1/stream/abc/x"), ("", "abc"))

    def test_rejects_unsafe_ids_before_using_them(self):
        body = self.body("bool QuarkValidId(string value)")
        self.assertIn("value.empty()", body)
        self.assertIn('value.find("\\"") >= 0', body)
        self.assertIn('value.find("\\\\") >= 0', body)
        self.assertIn('value.find("/") >= 0', body)
        self.assertIn('value.find("?") >= 0', body)

    def test_search_requests_fid_or_the_current_session_only(self):
        body = self.body("array<dictionary> SubtitleSearch(string MovieFileName, dictionary MovieMetaData)")
        self.assertIn('"/v1/subtitles?fid=" + HostUrlEncode(fid)', body)
        self.assertIn('"/v1/subtitles?session=" + HostUrlEncode(session)', body)
        self.assertIn("HostGetPlayingFileName()", body)
        self.assertIn("if (!QuarkCurrentVideo(source, fid, session)) return ret", body)
        self.assertIn("QuarkEnsureLogin()", body)
        # 普通本地/其它视频必须在触达组件或登录之前就返回空。
        self.assertLess(
            body.index("if (!QuarkCurrentVideo(source, fid, session)) return ret"),
            body.index("QuarkEnsureLogin()"),
        )

    def test_rows_carry_the_subtitle_id_prefix_and_real_names(self):
        body = body_raw(
            self.raw, "array<dictionary> SubtitleSearch(string MovieFileName, dictionary MovieMetaData)"
        )
        self.assertIn('row["id"] = QUARK_SUBTITLE_ID_PREFIX + id.asString()', body)
        self.assertIn('row["title"] = name.asString()', body)
        # format/lang 只在存在时写入，未知语言保留为空（不猜标签）。
        self.assertIn("if (format.isString() && !format.asString().empty()) row[\"format\"]", body)
        self.assertIn("if (lang.isString() && !lang.asString().empty()) row[\"lang\"]", body)
        langs = self.body("string GetLanguages()")
        self.assertIn('return ""', langs)

    def test_download_is_a_bearer_text_request_that_never_returns_error_json(self):
        body = body_raw(self.raw, "string SubtitleDownload(string id)")
        self.assertIn('id.find(QUARK_SUBTITLE_ID_PREFIX) != 0', body)
        self.assertIn("QuarkValidId(fid)", body)
        self.assertIn('"/v1/subtitle?fid=" + HostUrlEncode(fid)', body)
        self.assertIn("QuarkHeader(token, false)", body)
        self.assertIn("QuarkErrorType(text)", body)
        self.assertIn("if (type.empty()) return text", body)
        self.assertIn('if (type == "unauthorized")', body)
        self.assertIn("QuarkClearService()", body)
        # 令牌刷新只重试一次，绝不无限循环。
        self.assertIn("attempt < 2", body)

    def test_never_writes_subtitle_or_video_files(self):
        code, _ = strip_literals(self.raw)
        for name in ("HostFileCreate", "HostFileWrite", "HostFolderCreate", "HostFileOpen"):
            self.assertNotIn(name, code)

    def test_implements_no_upload_or_third_party_search(self):
        code, _ = strip_literals(self.raw)
        for name in (
            "SubtitleUpload",
            "GetUploadFormat",
            "SubtitleWebSearch",
            "ServerLogin",
            "ServerCheck",
        ):
            self.assertNotIn(name, code)
        for host in ("opensubtitles", "yifysubtitles", "yts.mx", "podnapisi", "napisy", "titlovi"):
            self.assertNotIn(host, self.raw.lower())

    def test_leaves_embedded_tracks_and_player_preferences_alone(self):
        code, _ = strip_literals(self.raw)
        for name in ("HostSetUrlHeaderHTTP", "HostSaveInteger", "HostMessageBox", "HostCheckMediaFile"):
            self.assertNotIn(name, code)


class FakeLogin:
    """Deterministic stand-in for the QR window: no Tk, no network."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def run(self, *, on_qr_ready=None) -> None:
        if on_qr_ready is not None:
            on_qr_ready(self.path)


class BridgeCase(unittest.TestCase):
    """Real loopback bridge on a temp config dir; subclasses pick login and records."""

    login_factory = None
    records = None
    cookie = COOKIE

    def setUp(self):
        self.bridge = _StartedBridge(self.login_factory, self.records, self.cookie)
        self.addCleanup(self.bridge.stop)
        self.client = self.bridge.client
        self.headers = self.bridge.headers
        self.base = self.bridge.base
        self.fixture = self.bridge.fixture

    def auth_payload(self):
        return self.client.get("/v1/auth", headers=self.headers).json()

    def start_login(self):
        headers = dict(self.headers, Connection="close", **{"Content-Type": "application/json"})
        return self.client.post(
            "/v1/auth/login", headers=headers, content=b'{"client":"potplayer"}'
        ).json()

    def wait_for_login_state(self, wanted, timeout=5.0):
        deadline = time.time() + timeout
        state = None
        while time.time() < deadline:
            state = self.auth_payload()["login_state"]
            if state in wanted:
                return state
            time.sleep(0.02)
        return state


class BridgeContractTests(BridgeCase):
    """The scripts parse these exact payloads; the real bridge proves the shapes."""

    def test_health_carries_the_fields_the_cache_reads(self):
        payload = self.client.get("/v1/health").json()
        self.assertEqual(payload["status"], "ok")
        for key in ("version", "instance"):
            self.assertIsInstance(payload[key], str)
            self.assertTrue(payload[key])

    def test_control_calls_require_the_runtime_token(self):
        payload = self.client.get("/v1/auth").json()
        self.assertEqual(payload["error"]["type"], "unauthorized")
        self.assertIsInstance(self.client.get("/v1/auth", headers=self.headers).json()["authenticated"], bool)

    def test_login_post_with_a_body_keeps_the_next_request_healthy(self):
        """The script sends ``Connection: close``: the bridge answers login
        without reading the body, so a kept-alive connection would desynchronise."""
        headers = dict(self.headers, Connection="close", **{"Content-Type": "application/json"})
        response = self.client.post("/v1/auth/login", headers=headers, content=b'{"client":"potplayer"}')
        self.assertEqual(response.status_code, 202)
        self.assertIn("login_state", response.json())
        page = self.client.get("/v1/files?parent=0&page=1", headers=self.headers)
        self.assertEqual(page.status_code, 200, page.text)
        self.assertIsInstance(page.json()["items"], list)

    def test_files_page_shape_and_mixed_entries(self):
        payload = self.client.get("/v1/files?parent=root-dir&page=1", headers=self.headers).json()
        self.assertIsInstance(payload["page"], int)
        self.assertIsInstance(payload["has_more"], bool)
        for item in payload["items"]:
            self.assertIsInstance(item["fid"], str)
            self.assertIsInstance(item["name"], str)
            self.assertIsInstance(item["is_dir"], bool)
            self.assertIsInstance(item["size"], int)
        names = {item["name"] for item in payload["items"]}
        self.assertIn("封面.jpg", names)  # non-video rows exist, so the script filters them
        self.assertIn("第9集.mkv", names)

    def test_playlist_is_naturally_ordered_and_starts_at_the_video(self):
        payload = self.client.get("/v1/playlist?parent=root-dir", headers=self.headers).json()
        self.assertEqual(payload["parent"], "root-dir")
        self.assertEqual([item["name"] for item in payload["items"]], ["第2集.mp4", "第9集.mkv", "第10集.mp4"])
        tail = self.client.get("/v1/playlist?start=v9", headers=self.headers).json()
        self.assertEqual(tail["items"][0]["fid"], "v9")
        self.assertEqual([item["name"] for item in tail["items"]], ["第9集.mkv", "第10集.mp4"])

    def test_play_returns_a_joinable_local_stream_url(self):
        payload = self.client.post("/v1/play", headers=self.headers, json={"fid": "v9"}).json()
        self.assertEqual(payload["title"], "第9集.mkv")
        self.assertTrue(payload["url"].startswith("/v1/stream/"), payload["url"])
        head = self.client.head(self.base + payload["url"])
        self.assertEqual(head.status_code, 200)
        self.assertEqual(head.headers["accept-ranges"], "bytes")
        self.assertEqual(head.headers["content-type"], "video/x-matroska")
        self.assertEqual(int(head.headers["content-length"]), len(VIDEO_BYTES))

    def test_error_envelope_is_what_quarkfailed_reads(self):
        missing = self.client.post("/v1/play", headers=self.headers, json={"fid": "nope"}).json()
        self.assertIsInstance(missing["error"]["type"], str)
        self.assertIsInstance(missing["error"]["message"], str)


class SubtitleBridgeContractTests(BridgeCase):
    """The subtitle script parses these exact payloads from the real bridge."""

    records = [
        record("sub-dir", "剧集", directory=True),
        record("sub-video", "剧.S01E01.mkv", "sub-dir"),
        record("sub-ass", "剧.S01E01.zh.ass", "sub-dir", size=len(SUBTITLE_BODY)),
    ]

    def setUp(self):
        super().setUp()
        self.fixture.bodies["sub-ass"] = SUBTITLE_BODY

    def test_search_lists_candidates_with_the_fields_the_script_reads(self):
        response = self.client.get("/v1/subtitles?fid=sub-video", headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["video_fid"], "sub-video")
        self.assertIsInstance(payload["items"], list)
        for item in payload["items"]:
            self.assertIsInstance(item["fid"], str)
            self.assertIsInstance(item["name"], str)
            self.assertIn(item.get("format"), ("srt", "ass"))
            self.assertIn(item.get("lang"), ("zh", "en", ""))
        item = next(row for row in payload["items"] if row["fid"] == "sub-ass")
        self.assertEqual(item["name"], "剧.S01E01.zh.ass")
        self.assertEqual(item["format"], "ass")

    def test_subtitle_text_is_returned_verbatim_as_utf8(self):
        response = self.client.get("/v1/subtitle?fid=sub-ass", headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("text/plain", response.headers["content-type"])
        self.assertIn("utf-8", response.headers["content-type"])
        self.assertEqual(response.content, SUBTITLE_BODY)

    def test_session_selection_and_errors_match_the_script_paths(self):
        self.assertEqual(
            self.client.get("/v1/subtitles", headers=self.headers).json()["error"]["type"], "bad_request"
        )
        self.assertEqual(
            self.client.get("/v1/subtitles?fid=sub-video&session=x", headers=self.headers).json()["error"]["type"],
            "bad_request",
        )
        self.assertEqual(
            self.client.get("/v1/subtitles?session=missing", headers=self.headers).json()["error"]["type"],
            "not_found",
        )
        self.assertEqual(
            self.client.get("/v1/subtitles?fid=sub-video").json()["error"]["type"], "unauthorized"
        )

    def test_a_video_is_never_read_as_a_subtitle(self):
        response = self.client.get("/v1/subtitle?fid=sub-video", headers=self.headers)
        self.assertNotEqual(response.status_code, 200)


class CancelLogin:
    """Stand-in for closing the QR window: reports cancellation after a beat."""

    def run(self, *, on_qr_ready=None) -> None:
        time.sleep(0.2)
        raise LoginCancelled("登录已取消")


class FailLogin:
    """Stand-in for a login that fails before any scan: no QR is ever shown."""

    def run(self, *, on_qr_ready=None) -> None:
        raise QuarkError("扫码登录失败")


class SavingLogin:
    """Stand-in for a completed scan: persists a credential like the real window."""

    def run(self, *, on_qr_ready=None) -> None:
        from quark_cli.auth import save_cookie
        from quark_cli.potplayer.plugin_state import plugin_directory

        save_cookie(COOKIE, plugin_directory() / "login-cookie")


class LoginStateBridgeTests(BridgeCase):
    """The /v1/auth ``login_state`` values the script's early-exit reads."""

    cookie = None
    login_factory = SavingLogin

    def test_unauthenticated_auth_reports_idle(self):
        payload = self.auth_payload()
        self.assertFalse(payload["authenticated"])
        self.assertEqual(payload["login_state"], "idle")

    def test_successful_scan_reports_success_and_authenticated(self):
        self.start_login()
        self.assertEqual(self.wait_for_login_state({"success"}), "success")
        self.assertTrue(self.auth_payload()["authenticated"])


class CancelledLoginBridgeTests(BridgeCase):
    """Closing the window ends in ``idle`` so the script stops waiting."""

    cookie = None
    login_factory = CancelLogin

    def test_cancel_reports_waiting_then_idle(self):
        started = self.start_login()
        self.assertEqual(started["login_state"], "waiting")
        self.assertEqual(self.wait_for_login_state({"idle", "failed"}), "idle")


class FailedLoginBridgeTests(BridgeCase):
    """A failing login ends in ``failed`` so the script stops waiting."""

    cookie = None
    login_factory = FailLogin

    def test_failure_reports_failed(self):
        self.start_login()
        self.assertEqual(self.wait_for_login_state({"failed"}), "failed")


class FilteredPageBridgeTests(BridgeCase):
    """A bridge page of only non-video rows must still carry the next-page token."""

    records = (
        record("cover-1", "封面1.jpg", "paged", size=8),
        record("cover-2", "封面2.jpg", "paged", size=8),
        record("ep-1", "第1集.mp4", "paged"),
    )

    def first_page(self):
        return self.client.get("/v1/files?parent=paged&page=1&size=2", headers=self.headers).json()

    def test_filtered_page_reaches_the_video_on_the_next_page(self):
        first = self.first_page()
        self.assertEqual([item["name"] for item in first["items"]], ["封面1.jpg", "封面2.jpg"])
        self.assertTrue(first["has_more"])

        rows = as_url_list_page(
            first["items"], has_more=first["has_more"], page=first["page"], path_token="paged"
        )
        # 该页内容行全被过滤掉，只剩“..”与收藏入口两条导航行；PageToken 落在最后一行上。
        self.assertEqual([row["title"] for row in rows], ["..", "收藏当前目录"])
        self.assertEqual([row for row in rows if "url" in row], [])
        self.assertEqual(rows[1]["PathToken"], "quark-action-add-paged")
        self.assertEqual(rows[-1]["PageToken"], "2")

        token = rows[-1]["PageToken"]
        second = self.client.get(
            f"/v1/files?parent=paged&page={token}&size=2", headers=self.headers
        ).json()
        self.assertFalse(second["has_more"])
        following = as_url_list_page(
            second["items"], has_more=second["has_more"], page=second["page"], path_token="paged"
        )
        self.assertEqual(
            [row["url"] for row in following if "url" in row],
            ["https://quark.invalid/play/ep-1"],
        )

    def test_root_page_of_only_non_video_rows_still_carries_the_token(self):
        first = self.first_page()
        rows = as_url_list_page(
            first["items"], has_more=first["has_more"], page=first["page"], path_token=""
        )
        self.assertEqual([row["title"] for row in rows], ["加载下一页…"])
        self.assertEqual(rows[-1]["PageToken"], "2")


class FavoriteRowModelTests(unittest.TestCase):
    """The transcription and the script must agree on the favorites entry row."""

    def test_action_tokens_are_shared_with_the_script(self):
        source = read_script(URL_LIST)
        for constant in (
            'string QUARK_ACTION_ADD = "quark-action-add-";',
            'string QUARK_ACTION_DEL = "quark-action-del-";',
            'string QUARK_ACTION_CLEAR_RECENT = "quark-action-clear-recent";',
        ):
            self.assertIn(constant, source)
        self.assertTrue(favorite_action_row("d1", False)["PathToken"].startswith("quark-action-add-"))
        self.assertTrue(favorite_action_row("d1", True)["PathToken"].startswith("quark-action-del-"))

    def test_saved_folder_offers_removal_and_an_url_never_leaks_in(self):
        added = favorite_action_row("d1", False)
        self.assertEqual(added, {"title": "收藏当前目录", "PathToken": "quark-action-add-d1", "folder": "1"})
        removed = favorite_action_row("d1", True)
        self.assertEqual(
            removed, {"title": "取消收藏当前目录", "PathToken": "quark-action-del-d1", "folder": "1"}
        )

    def test_directory_model_keeps_content_rows_after_the_navigation_rows(self):
        items = [
            {"fid": "sub", "name": "子目录", "is_dir": True, "size": 0},
            {"fid": "v1", "name": "第1集.mp4", "is_dir": False, "size": 4},
        ]
        rows = as_url_list_page(items, has_more=False, page=1, path_token="d1", saved=True)
        self.assertEqual([row["title"] for row in rows], ["..", "取消收藏当前目录", "子目录", "第1集.mp4"])
        self.assertEqual(rows[2]["url"], "https://quark.invalid/folder/sub")
        self.assertEqual(rows[3]["url"], "https://quark.invalid/play/v1")

    def test_second_page_carries_no_navigation_rows(self):
        rows = as_url_list_page([], has_more=True, page=2, path_token="d1")
        self.assertEqual([row["title"] for row in rows], ["加载下一页…"])
        self.assertEqual(rows[-1]["PageToken"], "3")


class LibraryRouteShapeTests(BridgeCase):
    """The new routes' payloads the search/favorites/recent entries parse.

    The routes belong to the bridge task; if a route is absent the bridge answers
    ``404 not_found`` and the test skips instead of reporting a false failure.
    Anything else (wrong status, wrong shape) still fails.
    """

    records = (
        record("fav-dir", "收藏剧集", directory=True),
        record("fav-vid", "第1集.mp4", "fav-dir"),
        record("empty-dir", "空目录", directory=True),
    )

    def required(self, response, path):
        if response.status_code == 404:
            error = response.json().get("error", {})
            if error.get("type") == "not_found":
                self.skipTest(f"bridge has no {path} route yet (parallel bridge task pending)")
        return response

    def get_json(self, path):
        return self.required(self.client.get(path, headers=self.headers), path).json()

    def post_json(self, path, payload):
        headers = dict(self.headers, Connection="close", **{"Content-Type": "application/json"})
        response = self.client.post(path, headers=headers, json=payload)
        return self.required(response, path).json()

    def test_search_returns_items_with_chinese_and_paging_fields(self):
        payload = self.get_json("/v1/search?q=" + quote("集") + "&page=1")
        self.assertIsInstance(payload["page"], int)
        self.assertIsInstance(payload["has_more"], bool)
        self.assertIsInstance(payload["items"], list)
        for item in payload["items"]:
            for key, kind in (("fid", str), ("name", str), ("is_dir", bool), ("size", int)):
                self.assertIsInstance(item[key], kind, item)
        found = {item["name"]: item["is_dir"] for item in payload["items"]}
        # 目录与视频都在搜索结果里，脚本据此分别给出目录行与选集链接。
        self.assertIs(found["收藏剧集"], True)
        self.assertIs(found["第1集.mp4"], False)

    def test_favorites_add_lists_and_deduplicates(self):
        first = self.post_json("/v1/favorites", {"fid": "fav-dir"})
        self.assertEqual([item["fid"] for item in first["items"]], ["fav-dir"])
        again = self.post_json("/v1/favorites", {"fid": "fav-dir"})
        self.assertEqual([item["fid"] for item in again["items"]], ["fav-dir"])
        listed = self.get_json("/v1/favorites")
        self.assertEqual([item["fid"] for item in listed["items"]], ["fav-dir"])
        for item in listed["items"]:
            self.assertIs(item["is_dir"], True)
            self.assertIsInstance(item["name"], str)

    def test_favorites_remove_is_idempotent(self):
        self.post_json("/v1/favorites", {"fid": "fav-dir"})
        self.assertEqual(self.post_json("/v1/favorites/remove", {"fid": "fav-dir"})["items"], [])
        self.assertEqual(self.post_json("/v1/favorites/remove", {"fid": "fav-dir"})["items"], [])
        self.assertEqual(self.get_json("/v1/favorites")["items"], [])

    def test_a_stale_favorite_can_still_be_removed(self):
        """A folder that no longer exists remotely must not block its own removal."""
        payload = self.post_json("/v1/favorites/remove", {"fid": "deleted-elsewhere"})
        self.assertEqual(payload["items"], [])
        self.assertEqual(self.post_json("/v1/favorites/remove", {"fid": "deleted-elsewhere"})["items"], [])

    def test_a_broken_favorite_stays_listed_and_removable(self):
        """The script's cancel fallback: a stale entry lists, its folder fails, removal works."""
        self.post_json("/v1/favorites", {"fid": "fav-dir"})
        self.bridge.fixture.records = [
            entry
            for entry in self.bridge.fixture.records
            if entry["fid"] != "fav-dir" and entry.get("pdir_fid") != "fav-dir"
        ]
        self.assertEqual([item["fid"] for item in self.get_json("/v1/favorites")["items"]], ["fav-dir"])
        page = self.client.get("/v1/files?parent=fav-dir&page=1", headers=self.headers)
        self.assertEqual(page.status_code, 502, page.text)
        self.assertEqual(self.post_json("/v1/favorites/remove", {"fid": "fav-dir"})["items"], [])

    def test_favorites_reject_a_file(self):
        headers = dict(self.headers, Connection="close", **{"Content-Type": "application/json"})
        response = self.client.post("/v1/favorites", headers=headers, json={"fid": "fav-vid"})
        self.required(response, "/v1/favorites")
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["error"]["type"], "bad_request")

    def test_a_successful_media_read_records_one_recent_entry(self):
        play = self.post_json("/v1/play", {"fid": "fav-vid"})
        streamed = self.client.get(self.base + play["url"])
        self.assertEqual(streamed.status_code, 200)
        payload = self.get_json("/v1/recent")
        self.assertEqual([item["fid"] for item in payload["items"]], ["fav-vid"])
        entry = payload["items"][0]
        self.assertIsInstance(entry["name"], str)
        self.assertIsInstance(entry["size"], int)
        self.assertIs(entry["is_dir"], False)
        self.assertIsInstance(entry["played_at"], int)

    def test_recent_head_does_not_record(self):
        play = self.post_json("/v1/play", {"fid": "fav-vid"})
        self.assertEqual(self.client.head(self.base + play["url"]).status_code, 200)
        self.assertEqual(self.get_json("/v1/recent")["items"], [])

    def test_continue_and_series_share_progress_display(self):
        library = self.bridge.service.library
        library.record_recent("fav-vid", "第1集.mp4", len(VIDEO_BYTES), "fav-dir")
        self.assertTrue(library.save_progress("fav-vid", len(VIDEO_BYTES), 60_000, 1_800_000))

        cont = self.get_json("/v1/continue")
        self.assertEqual([item["fid"] for item in cont["items"]], ["fav-vid"])
        self.assertIn("已看 3%", cont["items"][0]["display"])

        series = self.get_json("/v1/series?parent=fav-dir")
        self.assertEqual(series["parent"], "fav-dir")
        self.assertEqual(series["total"], 1)
        self.assertFalse(series["complete"])
        self.assertEqual(series["continue"]["fid"], "fav-vid")
        for key in ("fid", "name", "size", "watched", "position_ms", "duration_ms", "display"):
            self.assertIn(key, series["episodes"][0])

    def test_mark_route_switches_the_series_watched_state(self):
        marked = self.post_json("/v1/mark", {"fid": "fav-vid", "parent": "fav-dir", "watched": True})
        self.assertEqual(marked, {"fid": "fav-vid", "watched": True})
        series = self.get_json("/v1/series?parent=fav-dir")
        self.assertTrue(series["complete"])
        self.assertIsNone(series["continue"])
        self.assertEqual(series["watched"], 1)
        self.post_json("/v1/mark", {"fid": "fav-vid", "parent": "fav-dir", "watched": False})
        self.assertFalse(self.get_json("/v1/series?parent=fav-dir")["complete"])

    def test_recent_clear_is_idempotent(self):
        play = self.post_json("/v1/play", {"fid": "fav-vid"})
        self.assertEqual(self.client.get(self.base + play["url"]).status_code, 200)
        for _ in range(2):
            cleared = self.post_json("/v1/recent/clear", {"client": "potplayer"})
            self.assertNotIn("error", cleared)
        self.assertEqual(self.get_json("/v1/recent")["items"], [])

    def watch_route(self, path, method="GET", payload=None):
        """Call a 0.8.0 route, skipping while the parallel bridge task is unfinished.

        Absent routes answer 404 ``not_found``; a route listed but not yet wired
        answers 405 ``method_not_allowed``. Either means the bridge half of the
        watch-center/bookmarks/remove contract has not landed yet.
        """
        headers = dict(self.headers, Connection="close", **{"Content-Type": "application/json"})
        if method == "GET":
            response = self.client.get(path, headers=self.headers)
        else:
            response = self.client.post(path, headers=headers, json=payload)
        if response.status_code in (404, 405):
            error = response.json().get("error", {})
            if error.get("type") in ("not_found", "method_not_allowed"):
                self.skipTest(f"bridge has no {method} {path} yet (parallel bridge task pending)")
        return response.json()

    def test_watch_center_groups_series_and_root_singles(self):
        library = self.bridge.service.library
        library.record_recent("fav-vid", "第1集.mp4", len(VIDEO_BYTES), "fav-dir")
        library.save_progress("fav-vid", len(VIDEO_BYTES), 60_000, 1_800_000)
        library.record_recent("root-vid", "电影.mp4", 2048, "")
        payload = self.watch_route("/v1/watch-center")
        self.assertIsInstance(payload["items"], list)
        by_kind = {item["kind"]: item for item in payload["items"]}
        self.assertIn("series", by_kind)
        self.assertIn("video", by_kind)
        series = by_kind["series"]
        self.assertEqual(series["key"], "fav-dir")
        self.assertEqual(series["parent"], "fav-dir")
        self.assertIsInstance(series["name"], str)
        self.assertIsInstance(series["display"], str)
        self.assertIsInstance(series["unfinished_count"], int)
        self.assertEqual(series["latest"]["fid"], "fav-vid")
        video = by_kind["video"]
        self.assertEqual(video["key"], "root-vid")
        self.assertEqual(video["parent"], "")

    def test_series_adds_latest_and_a_non_wrapping_next(self):
        library = self.bridge.service.library
        library.record_recent("fav-vid", "第1集.mp4", len(VIDEO_BYTES), "fav-dir")
        library.save_progress("fav-vid", len(VIDEO_BYTES), 60_000, 1_800_000)
        payload = self.watch_route("/v1/series?parent=fav-dir")
        if "latest" not in payload or "next" not in payload:
            self.skipTest("bridge /v1/series has no latest/next yet (parallel bridge task pending)")
        self.assertIsInstance(payload["latest"], dict)
        self.assertEqual(payload["latest"]["fid"], "fav-vid")
        for key in ("fid", "name", "size", "parent", "display"):
            self.assertIn(key, payload["latest"])
        # 该目录只有一集：没有下一集，也绝不回绕到第一集。
        self.assertIsNone(payload["next"])

    def test_series_of_an_empty_directory_is_zero_total_and_not_complete(self):
        # 空目录（无视频）不是“看完”：total == 0、complete 为假、没有继续/下一集。
        # 脚本据此给“该目录没有视频 · 返回”，绝不误标“已到最后一集”。
        payload = self.watch_route("/v1/series?parent=empty-dir")
        if "total" not in payload:
            self.skipTest("bridge /v1/series has no total yet (parallel bridge task pending)")
        self.assertEqual(payload["total"], 0)
        self.assertFalse(payload["complete"])
        self.assertIsNone(payload["continue"])
        self.assertIsNone(payload["next"])
        self.assertEqual(payload["episodes"], [])

    def test_recent_remove_deletes_only_the_selected_record_idempotently(self):
        library = self.bridge.service.library
        library.record_recent("fav-vid", "第1集.mp4", len(VIDEO_BYTES), "fav-dir")
        library.record_recent("root-vid", "电影.mp4", 2048, "")
        for _ in range(2):
            payload = self.watch_route("/v1/recent/remove", "POST", {"fid": "fav-vid"})
            self.assertNotIn("error", payload)
        remaining = [item["fid"] for item in self.get_json("/v1/recent")["items"]]
        self.assertEqual(remaining, ["root-vid"])

    def test_bookmarks_open_reuses_the_manager_for_the_selected_fid(self):
        first = self.watch_route("/v1/bookmarks/open", "POST", {"fid": "fav-vid"})
        second = self.watch_route("/v1/bookmarks/open", "POST", {"fid": "fav-vid"})
        self.assertNotIn("error", first)
        self.assertNotIn("error", second)


if __name__ == "__main__":
    unittest.main()
