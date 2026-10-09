"""Static and contract checks for the PotPlayer AngelScript extensions.

PotPlayer ships no standalone AngelScript toolchain, so these scripts cannot be
compiled here.  The checks below therefore verify what a compiler and a real
player would catch:

* both scripts tokenise cleanly (terminated literals/comments, balanced
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

import httpx

from quark_cli.auth import LoginCancelled
from quark_cli.errors import QuarkError
from quark_cli.potplayer.naming import VIDEO_EXTENSIONS, is_video

from potplayer_fixture import COOKIE, VIDEO_BYTES, DriveFixture, record

ROOT = Path(__file__).resolve().parents[1]
AS_DIR = ROOT / "potplayer"
URL_LIST = AS_DIR / "MediaUrlList - Quark.as"
PLAY_PARSE = AS_DIR / "MediaPlayParse - Quark.as"
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


def as_url_list_page(items, *, has_more, page, path_token):
    """Executable transcription of ``GetUrlList``'s row building and PageToken tail.

    PotPlayer ships no standalone AngelScript toolchain, so the script cannot run
    here; this mirrors ``MediaUrlList - Quark.as`` one branch at a time and
    ``PlaybackDesignTests`` asserts the source still holds those same branches, so
    the model and the script cannot drift apart silently.
    """
    rows = []
    if path_token and page == 1:
        rows.append({"title": "..", "folder": "parent"})
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


class _StartedBridge:
    """A real Service serving an in-memory fixture on an ephemeral loopback port."""

    def __init__(self, login_factory=None, records=None, cookie=COOKIE):
        from quark_cli.potplayer.service import Service

        self.directory = Path(tempfile.mkdtemp(prefix="quark-potplayer-as-"))
        self.previous_config = os.environ.get("QUARK_CONFIG_DIR")
        os.environ["QUARK_CONFIG_DIR"] = str(self.directory)
        if login_factory is None:
            login_factory = lambda: FakeLogin(self.directory / "qr.png")
        self.fixture = DriveFixture(records)
        self.service = Service(
            transport=self.fixture.transport(), cookie=cookie, login_factory=login_factory
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


class ScriptSyntaxTests(unittest.TestCase):
    """Tokenising and delimiter checks: the defects a compiler reports first."""

    def scripts(self):
        return ((URL_LIST, read_script(URL_LIST)), (PLAY_PARSE, read_script(PLAY_PARSE)))

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
        for signature in URL_LIST_EXPORTS:
            self.assertRegex(url_code, signature_pattern(signature))
        for signature in PLAY_PARSE_EXPORTS:
            self.assertRegex(play_code, signature_pattern(signature))


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
        for path in (URL_LIST, PLAY_PARSE):
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
        body = body_raw(self.url_raw, "array<dictionary> GetUrlList(string Category, string Extra, string PathToken, string Query, string PageToken)")
        self.assertRegex(body, r'row\["url"\]\s*=\s*QUARK_FOLDER_PREFIX')
        self.assertRegex(body, r'row\["folder"\]\s*=\s*"1"')
        self.assertRegex(body, r'row\["PathToken"\]\s*=\s*fid\.asString\(\)')
        self.assertRegex(body, r'row\["url"\]\s*=\s*QUARK_PLAY_PREFIX')
        self.assertIn("PageToken", body)
        self.assertRegex(body, r'up\["folder"\]\s*=\s*"parent"')

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
        body = squash(
            body_raw(
                self.url_raw,
                "array<dictionary> GetUrlList(string Category, string Extra, string PathToken, string Query, string PageToken)",
            )
        )
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
        from quark_cli.potplayer.plugin_state import store_cookie

        store_cookie(COOKIE)


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
        # 该页内容行全被过滤掉，只剩“..”导航行；PageToken 落在最后一行上。
        self.assertEqual([row["title"] for row in rows], [".."])
        self.assertEqual([row for row in rows if "url" in row], [])
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


if __name__ == "__main__":
    unittest.main()
