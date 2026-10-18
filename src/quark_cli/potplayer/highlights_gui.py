"""Highlight time points and notes; owns its client, worker and player bindings."""
from __future__ import annotations
from typing import Any
import contextlib
from urllib.parse import quote
from .gui_common import (
    BOOKMARKS_ADD_PATH,
    BOOKMARKS_JUMP_PATH,
    BOOKMARKS_PATH,
    BOOKMARKS_REMOVE_PATH,
    BOOKMARKS_RENAME_PATH,
    BOOKMARK_HEIGHT,
    BOOKMARK_MIN_HEIGHT,
    BOOKMARK_MIN_WIDTH,
    BOOKMARK_NOTICE_WRAP,
    BOOKMARK_WIDTH,
    EXTERNAL_PID,
    MAX_NOTE_CHARS,
    _BOOKMARK_ADDED,
    _BOOKMARK_ADD_FAILED,
    _BOOKMARK_EXTERNAL_REASON,
    _BOOKMARK_JUMPED,
    _BOOKMARK_JUMP_FAILED,
    _BOOKMARK_LOADING,
    _BOOKMARK_LOAD_FAILED,
    _BOOKMARK_NOTE_TOO_LONG,
    _BOOKMARK_REMOVED,
    _BOOKMARK_REMOVE_FAILED,
    _BOOKMARK_RENAMED,
    _BOOKMARK_RENAME_FAILED,
    _BOOKMARK_STALE,
    _BOOKMARK_WORKING,
    _DISCONNECTED_NOTE,
    _OPERABLE_STATES,
    _toggle,
    bookmark_action_enabled,
    bookmark_enabled,
    bookmark_id_of,
    bookmark_request_of,
    external_action_target,
    format_duration_ms,
    match_selection,
    selection_target,
    state_label,
)
from .gui_transport import LocalClient, LocalWorker, LocalPanel
from .bookmark_state import bookmark_groups, group_key


class HighlightsClient(LocalClient):
    def snapshot(self):
        return self._get_object('/v1/bookmarks/state')
    def bookmarks(self, fid: str) -> list[dict[str, Any]] | None:
        """One video's bookmarks by fid; ``None`` when the request failed.

        The fid is always the binding's own identifier, never a title guess, and
        an empty list is a valid answer (the video simply has no bookmark yet).
        """
        path = f"{BOOKMARKS_PATH}?fid={quote(fid, safe='')}" if fid else BOOKMARKS_PATH
        if fid.startswith('parent/'):
            path = f"{BOOKMARKS_PATH}?parent={quote(fid[7:], safe='')}"
        payload = self._get_object(path, timeout=30)
        self.metadata_notice = payload.get('metadata_notice', '') if payload else ''
        items = payload.get("items") if payload is not None else None
        if not isinstance(items, list):
            return None
        return [item for item in items if isinstance(item, dict)]

    def add_bookmark(self, player_pid: int, fid: str, note: str) -> bool:
        """Bookmark the player's current position of one binding."""
        return self._post_accepted(
            BOOKMARKS_ADD_PATH, {"player_pid": player_pid, "fid": fid, "note": note}
        )

    def rename_bookmark(self, bookmark_id: Any, note: str) -> bool:
        return self._post_accepted(BOOKMARKS_RENAME_PATH, {"id": bookmark_id, "note": note})

    def remove_bookmark(self, bookmark_id: Any) -> bool:
        return self._post_accepted(BOOKMARKS_REMOVE_PATH, {"id": bookmark_id})

    def jump_bookmark(self, player_pid: int, fid: str, bookmark_id: Any) -> bool:
        return self._post_accepted(
            BOOKMARKS_JUMP_PATH, {"player_pid": player_pid, "fid": fid, "id": bookmark_id}
        )

    def play_bookmark(self, bookmark_id: Any) -> bool:
        return self._post_accepted("/v1/bookmarks/play", {"id": bookmark_id})


class HighlightsWorker(LocalWorker):
    def _command(self, request):
        kind = request[0] if request else None
        if kind == "bookmarks":
            _, player_pid, fid = request
            items = self._client.bookmarks(fid)
            if getattr(self._client, 'metadata_notice', ''):
                self._emit('metadata_notice', self._client.metadata_notice)
            self._emit("bookmarks", (player_pid, fid, items))
        elif kind == "bookmark_add":
            _, player_pid, fid, note = request
            ok = self._client.add_bookmark(player_pid, fid, note)
            self._emit("bookmark_added", (player_pid, fid, ok))
        elif kind == "bookmark_rename":
            _, player_pid, fid, bookmark_id, note = request
            ok = self._client.rename_bookmark(bookmark_id, note)
            self._emit("bookmark_renamed", (player_pid, fid, ok))
        elif kind == "bookmark_remove":
            _, player_pid, fid, bookmark_id = request
            ok = self._client.remove_bookmark(bookmark_id)
            self._emit("bookmark_removed", (player_pid, fid, ok))
        elif kind == "bookmark_jump":
            _, player_pid, fid, bookmark_id = request
            ok = self._client.jump_bookmark(player_pid, fid, bookmark_id)
            self._emit("bookmark_jumped", (player_pid, fid, ok))
        elif kind == "bookmark_play":
            _, player_pid, fid, bookmark_id = request
            ok = self._client.play_bookmark(bookmark_id)
            self._emit("bookmark_jumped", (player_pid, fid, ok))


class HighlightsPanel(LocalPanel):
    worker_type = HighlightsWorker
    title = '精彩标记'
    def __init__(self, client, endpoint):
        super().__init__(client, endpoint)
        self._playback, self._rows = [], {}
        self._selected = self._external = self._last_request_id = None
        self._rebuilding = False
        self._bookmarks = []
        self._bookmark_pending = None
        self._playback_tree = self._bookmark_button = self._notice_var = None
        self._bookmark_window = self._bookmark_tree = None
        self._bookmark_rows = {}
        self._bookmark_header_var = self._bookmark_notice_var = None
        self._bookmark_add_button = self._bookmark_jump_button = None
        self._bookmark_rename_button = self._bookmark_remove_button = self._bookmark_refresh_button = None
        self._simpledialog = None
        self._embedded = False
        self.consume_requests = True
        self._group = None
        self._group_rows = {}
        self._capture_requested = False

    def _build(self, tk, root):
        from tkinter import ttk
        frame = ttk.Frame(root, padding=16)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='双击剧集文件夹查看各集时间点与备注；双击标记可打开视频并跳转。').pack(anchor='w', pady=(0, 8))
        self._notice_var = tk.StringVar(value='正在连接精彩标记服务……')
        self._embedded = True
        self._build_bookmarks(host=frame)
        ttk.Label(frame, textvariable=self._notice_var).pack(anchor='w')
        self._update_actions()

    def _open_all(self):
        if self._connected:
            self._open_external({'id':'all', 'fid':'', 'name':'全部视频', 'scope':'all'})

    def _apply_snapshot(self, snapshot):
        self._connected = True
        account_id = (snapshot.get('account_id') or '') if 'account_id' in snapshot else self._account_id
        if self._account_id is not None and self._account_id != account_id:
            if self._embedded:
                self._clear_bookmarks()
                self._group = None
                self._capture_requested = False
            else:
                self._close_bookmarks()
            self._selected = self._external = self._last_request_id = None
        self._account_id = account_id
        self._playback = snapshot.get('playback', [])
        if self.consume_requests:
            self._consume_bookmark_request(snapshot)
        if self._embedded and self._external is None:
            self._open_all()
        self._refresh_playback()
        self._notice_var.set('已连接精彩标记服务')
        self._update_actions()

    def _show_disconnected(self):
        self._connected = False
        self._playback = []
        self._external = None
        self._selected = None
        self._notice_var.set(_DISCONNECTED_NOTE)
        self._refresh_playback()
        if self._bookmark_live():
            self._clear_bookmarks()
            self._bookmark_notice(_DISCONNECTED_NOTE)
        self._update_actions()

    def _update_actions(self):
        busy = self._bookmark_pending is not None
        enabled = self._connected and not busy if self._external is not None else bookmark_enabled(self._connected, self._playback, self._selected, busy)
        _toggle(self._bookmark_button, enabled)

    def _handle(self, kind, value):
        if kind == 'scoped':
            scope, event, payload = value
            if scope == self._account_id:
                self._handle(event, payload)
        elif kind == 'snapshot':
            self._apply_snapshot(value)
        elif kind in ('failed', 'disconnected'):
            self._show_disconnected()
        elif kind == 'bookmarks':
            self._apply_bookmarks(value)
        elif kind == 'metadata_notice':
            self._bookmark_notice(value)
        elif kind == "bookmark_added":
            self._apply_bookmark_result(value, _BOOKMARK_ADDED, _BOOKMARK_ADD_FAILED, refresh=True)
        elif kind == "bookmark_renamed":
            self._apply_bookmark_result(
                value, _BOOKMARK_RENAMED, _BOOKMARK_RENAME_FAILED, refresh=True
            )
        elif kind == "bookmark_removed":
            self._apply_bookmark_result(
                value, _BOOKMARK_REMOVED, _BOOKMARK_REMOVE_FAILED, refresh=True
            )
        elif kind == "bookmark_jumped":
            self._apply_bookmark_result(
                value, _BOOKMARK_JUMPED, _BOOKMARK_JUMP_FAILED, refresh=False
            )


    def _shutdown(self):
        self._close_bookmarks()
        super()._shutdown()
    def _consume_bookmark_request(self, snapshot: dict[str, Any]) -> None:
        """Open the manager for a new external request; each id runs once.

        The service keeps only the latest request and invalidates it with a new
        random id, so the last consumed id is enough: a repeated id (a request
        left in the snapshot) never reopens a window the operator closed, while
        a *different* id -- even for the same FID -- opens (or retargets) again.
        """
        request = bookmark_request_of(snapshot)
        if request is None or request["id"] == self._last_request_id:
            return
        self._last_request_id = request["id"]
        self._open_external(request)

    def _open_external(self, request: dict[str, Any]) -> None:
        """Bind the bookmark manager to an external request (Tk thread only)."""
        self._external = request
        self._group = None
        self._capture_requested = request.get('action') == 'add'
        if self._root is None:
            return
        if self._bookmark_live():
            with contextlib.suppress(Exception):
                self._root.lift()
        else:
            self._build_bookmarks()
        # Drop any list or pending answer of the previous target, then read the
        # new FID's list exactly once.
        self._clear_bookmarks()
        self._request_bookmarks()

    def _refresh_playback(self) -> None:
        tree = self._playback_tree
        if tree is None:
            self._refresh_bookmark_header()
            self._update_bookmark_actions()
            return
        # Read the selection before rebuilding: a programmatic selection change
        # during ``delete``/``selection_set`` may fire ``_on_select``, and the
        # row is restored afterwards so the bookmark list keeps its binding.
        previous = self._selected
        self._rebuilding = True
        try:
            tree.delete(*tree.get_children())
            self._rows = {}
            restored = False
            for index, row in enumerate(self._playback):
                if not isinstance(row, dict):
                    continue
                iid = f"row{index}"
                tree.insert(
                    "",
                    "end",
                    iid=iid,
                    values=(
                        row.get("name") or "—",
                        format_duration_ms(row.get("position_ms")),
                        format_duration_ms(row.get("duration_ms")),
                        state_label(row.get("state")),
                    ),
                )
                self._rows[iid] = (row.get("player_pid"), row.get("fid"))
                if previous is not None and self._rows[iid] == previous:
                    tree.selection_set(iid)
                    tree.focus(iid)
                    restored = True
            if previous is not None:
                self._selected = previous if restored else None
        finally:
            self._rebuilding = False
        if self._bookmark_live():
            if self._external is not None:
                # An external target is independent of the playback selection:
                # keep its list and refresh the header/buttons against the new
                # snapshot (a matching live player may have appeared or gone).
                self._refresh_bookmark_header()
                self._update_bookmark_actions()
            elif selection_target(self._playback, self._selected) is None:
                self._clear_bookmarks()
            else:
                self._refresh_bookmark_header()

    def _on_select(self, _event: Any = None) -> None:
        if self._rebuilding:
            # Programmatic re-selection during a snapshot rebuild is not a change.
            return
        selection = self._playback_tree.selection()
        selected = self._rows.get(selection[0]) if selection else None
        if selected == self._selected:
            # Re-selecting the current row is not a change. ``ttk`` queues
            # ``<<TreeviewSelect>>`` and delivers it *after* ``_refresh_playback``
            # has reset its rebuild flag, so this guard is what keeps the periodic
            # refresh from dropping an external target bound to the same row.
            return
        # Explicitly choosing a different playback row leaves external mode and
        # binds the manager to that row again.
        self._external = None
        self._selected = selected
        self._update_actions()
        if self._bookmark_live():
            self._clear_bookmarks()
            self._request_bookmarks()

    def _bookmark_live(self) -> bool:
        """True while the bookmark window exists (Tk thread only)."""
        window = self._bookmark_window
        if window is None:
            return False
        exists = False
        with contextlib.suppress(Exception):
            exists = bool(window.winfo_exists())
        return exists

    def _binding_key(self) -> tuple[int, str] | None:
        """The ``(player_pid, fid)`` the list currently belongs to, or ``None``.

        External mode carries the :data:`EXTERNAL_PID` sentinel, so its answers
        are never mistaken for a playback row's.
        """
        if self._external is not None:
            return (EXTERNAL_PID, self._list_fid())
        return selection_target(self._playback, self._selected)

    def _binding_name(self) -> str:
        """The display name of the current binding (external name or the row's)."""
        if self._external is not None:
            return self._external["name"] or self._external["fid"]
        row = match_selection(self._playback, self._selected)
        name = row.get("name") if row is not None else None
        return name if isinstance(name, str) and name else "—"

    def _external_action_target(self) -> tuple[int, str] | None:
        """The one live binding for the external FID, else ``None``."""
        if self._external is None or not self._connected:
            return None
        if self._external.get("scope") in ('all', 'parent'):
            selected = selection_target(self._playback, self._selected)
            if selected is not None and self._external.get('scope') == 'all' and self._group is None:
                return external_action_target(self._playback, selected[1])
            live = [row for row in self._playback if row.get("state") in _OPERABLE_STATES]
            parent = self._external.get('parent') if self._external.get('scope') == 'parent' else self._group
            if parent is not None:
                live = [row for row in live if row.get('parent') == parent or row.get('series_fid') == parent]
            if len(live) != 1:
                return None
            return external_action_target(self._playback, live[0].get("fid"))
        return external_action_target(self._playback, self._external["fid"])

    def _list_fid(self) -> str:
        if self._external.get('scope') == 'parent':
            return 'parent/' + self._external['parent']
        return self._external['fid']

    def _list_target(self) -> tuple[int, str] | None:
        """The binding the list may be read for, else ``None``.

        An external target is always listable while connected -- the local
        bookmark document needs no live player -- and is addressed with the
        sentinel pid. A playback target needs a matched, connected binding.
        """
        if not self._bookmark_live() or self._bookmark_pending is not None:
            return None
        if self._external is not None:
            if not self._connected:
                return None
            return (EXTERNAL_PID, self._list_fid())
        if not bookmark_enabled(self._connected, self._playback, self._selected):
            return None
        return selection_target(self._playback, self._selected)

    def _action_target(self) -> tuple[int, str] | None:
        """The binding the player can still be asked to sample/seek for add/jump.

        An external target resolves only when exactly one operable binding of
        that FID is live, and always returns its *real* pid, so the sentinel
        never reaches an add or jump endpoint.
        """
        if not self._bookmark_live() or self._bookmark_pending is not None:
            return None
        if self._external is not None:
            return self._external_action_target()
        if not bookmark_action_enabled(self._connected, self._playback, self._selected):
            return None
        return selection_target(self._playback, self._selected)

    def _selected_bookmark(self) -> Any:
        """The id of the selected bookmark row, or ``None``."""
        tree = self._bookmark_tree
        if tree is None:
            return None
        selection = tree.selection()
        return self._bookmark_rows.get(selection[0]) if selection else None

    def _selected_bookmark_entry(self) -> dict[str, Any] | None:
        selected = self._selected_bookmark()
        return next((entry for entry in self._bookmarks if bookmark_id_of(entry) == selected), None)

    def _can_open_bookmark(self) -> bool:
        """Only open a fresh play when this FID has no loading/active player."""
        entry = self._selected_bookmark_entry()
        if self._external is None or entry is None or not self._connected or self._bookmark_pending is not None:
            return False
        fid = entry.get("fid", self._external["fid"])
        return bool(fid) and not any(
            row.get("fid") == fid and row.get("state") in (_OPERABLE_STATES | {"waiting", "suspended"})
            for row in self._playback
        )

    def _bookmark_notice(self, text: str) -> None:
        if self._bookmark_notice_var is not None:
            self._bookmark_notice_var.set(text)

    def _settle_bookmark_notice(self, text: str) -> None:
        """Replace a load-in-progress message with the load's own result.

        The re-read after an operator command runs with ``announce=False`` so
        that command's own success message stays visible; it must not be
        overwritten by a later list answer.
        """
        var = self._bookmark_notice_var
        if var is not None and var.get() == _BOOKMARK_LOADING:
            var.set(text)

    def _open_bookmarks(self) -> None:
        """Open (or re-read) the bookmark manager for the current binding."""
        if self._root is None:
            return
        if self._external is not None:
            if not self._connected or self._bookmark_pending is not None:
                return
        elif not bookmark_enabled(
            self._connected, self._playback, self._selected, self._bookmark_pending is not None
        ):
            return
        if self._bookmark_live():
            with contextlib.suppress(Exception):
                self._bookmark_window.lift()
        else:
            self._build_bookmarks()
        # Only a selection change and an explicit operator action re-read the
        # list; the per-second highlight-state poll never does.
        self._request_bookmarks()

    def _build_bookmarks(self, host=None) -> None:
        """Create the manager window; every Tk call stays on this thread."""
        from tkinter import simpledialog, ttk

        root = self._root
        self._simpledialog = simpledialog
        window = ttk.Frame(host) if host is not None else self._tk.Toplevel(root)
        self._bookmark_window = window
        if host is not None:
            window.pack(fill='both', expand=True)
        else:
            window.title("精彩标记")
            window.geometry(f"{BOOKMARK_WIDTH}x{BOOKMARK_HEIGHT}")
            window.minsize(BOOKMARK_MIN_WIDTH, BOOKMARK_MIN_HEIGHT)
            window.transient(root)
            window.protocol("WM_DELETE_WINDOW", self._close_bookmarks)

        self._bookmark_header_var = self._tk.StringVar(value="—")
        self._bookmark_notice_var = self._tk.StringVar(value="")
        outer = ttk.Frame(window, padding=10)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)
        ttk.Label(
            outer, textvariable=self._bookmark_header_var, anchor="w", justify="left"
        ).grid(row=0, column=0, sticky="ew")

        table = ttk.Frame(outer)
        table.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)
        self._bookmark_tree = ttk.Treeview(
            table, columns=("video", "time", "note", "category"), show="headings", selectmode="browse"
        )
        for column, title, width in (("video", "视频", 220), ("time", "时间", 100), ("note", "备注", 280), ('category', '分类', 80)):
            self._bookmark_tree.heading(column, text=title)
            self._bookmark_tree.column(column, width=width, anchor="w", stretch=(column == "note"))
        self._bookmark_tree.grid(row=0, column=0, sticky="nsew")
        bookmark_scroll = ttk.Scrollbar(
            table, orient="vertical", command=self._bookmark_tree.yview
        )
        self._bookmark_tree.configure(yscrollcommand=bookmark_scroll.set)
        bookmark_scroll.grid(row=0, column=1, sticky="ns")
        self._bookmark_tree.bind("<<TreeviewSelect>>", self._on_bookmark_select)
        self._bookmark_tree.bind('<Double-1>', self._enter_group_or_jump)

        actions = ttk.Frame(outer)
        actions.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        self._bookmark_add_button = ttk.Button(
            actions, text="标记当前时间点", width=16, command=self._add_bookmark
        )
        self._bookmark_jump_button = ttk.Button(
            actions, text="跳转", width=8, command=self._jump_bookmark
        )
        self._bookmark_rename_button = ttk.Button(
            actions, text="编辑备注", width=10, command=self._rename_bookmark
        )
        self._bookmark_remove_button = ttk.Button(
            actions, text="删除", width=8, command=self._remove_bookmark
        )
        self._bookmark_refresh_button = ttk.Button(
            actions, text="刷新", width=8, command=self._request_bookmarks
        )
        for button in (
            self._bookmark_add_button,
            self._bookmark_jump_button,
            self._bookmark_rename_button,
            self._bookmark_remove_button,
            self._bookmark_refresh_button,
        ):
            button.pack(side="left", padx=(0, 6))
        ttk.Button(actions, text='全部剧集', command=self._open_all).pack(side='right')
        ttk.Button(actions, text='返回剧集列表', command=self._back_to_groups).pack(side='right', padx=6)
        ttk.Label(
            outer, textvariable=self._bookmark_notice_var, anchor="w", wraplength=BOOKMARK_NOTICE_WRAP
        ).grid(row=3, column=0, sticky="ew", pady=(8, 0))
        self._refresh_bookmark_header()
        self._update_bookmark_actions()

    def _close_bookmarks(self) -> None:
        """Tk thread only: destroy the manager window and drop its state."""
        # Leaving the manager also leaves external mode; the consumed request
        # ids stay, so the very same request never reopens a closed window.
        self._external = None
        self._clear_bookmarks()
        window, self._bookmark_window = self._bookmark_window, None
        if window is not None:
            with contextlib.suppress(Exception):
                window.destroy()
        self._bookmark_tree = None
        self._bookmark_header_var = None
        self._bookmark_notice_var = None
        self._simpledialog = None
        self._bookmark_add_button = None
        self._bookmark_jump_button = None
        self._bookmark_rename_button = None
        self._bookmark_remove_button = None
        self._bookmark_refresh_button = None
        self._update_actions()

    def _clear_bookmarks(self) -> None:
        """Drop the list and pending request of a binding that is gone."""
        self._bookmarks = []
        self._bookmark_pending = None
        self._bookmark_rows = {}
        tree = self._bookmark_tree
        if tree is not None:
            with contextlib.suppress(Exception):
                tree.delete(*tree.get_children())
        self._bookmark_notice("")
        self._refresh_bookmark_header()
        self._update_bookmark_actions()

    def _request_bookmarks(self, *, announce: bool = True) -> None:
        """Ask the worker for the selected binding's bookmarks (Tk thread only).

        ``announce`` is dropped for the re-read after an operator command so the
        command's own result message stays visible.
        """
        target = self._list_target()
        if target is None:
            return
        player_pid, fid = target
        self._bookmark_pending = target
        self._bookmarks = []
        self._bookmark_rows = {}
        tree = self._bookmark_tree
        if tree is not None:
            with contextlib.suppress(Exception):
                tree.delete(*tree.get_children())
        self._refresh_bookmark_header()
        if announce:
            self._bookmark_notice(_BOOKMARK_LOADING)
        self._update_bookmark_actions()
        self._update_actions()
        self._queue_command(("bookmarks", player_pid, fid))

    def _apply_bookmarks(self, value: Any) -> None:
        """Apply a list answer, dropping any that belongs to another binding."""
        if not self._bookmark_live() or not isinstance(value, tuple) or len(value) != 3:
            return
        player_pid, fid, items = value
        if (player_pid, fid) != self._binding_key():
            return
        self._bookmark_pending = None
        if items is None:
            self._capture_requested = False
            self._bookmarks = []
            self._render_bookmarks()
            self._bookmark_notice(_BOOKMARK_LOAD_FAILED)
            self._update_actions()
            return
        # Only addressable entries are kept: an entry without an id could not be
        # renamed, removed or jumped, and the header count is the visible one.
        self._bookmarks = [entry for entry in items if bookmark_id_of(entry) is not None]
        self._render_bookmarks()
        self._settle_bookmark_notice(self._load_notice())
        self._update_actions()
        if self._capture_requested:
            self._capture_requested = False
            if self._action_target() is not None:
                self._add_bookmark()
            else:
                self._bookmark_notice('当前目录没有唯一且正在播放的视频，无法标记时间点')

    def _load_notice(self) -> str:
        """The list-load result, with the fixed reason when add/jump are offline."""
        loaded = f"已读取 {len(self._bookmarks)} 条精彩标记"
        if self._external is not None and self._external_action_target() is None:
            return f"{loaded}　{_BOOKMARK_EXTERNAL_REASON}"
        return loaded

    def _apply_bookmark_result(self, value: Any, done: str, failed: str, *, refresh: bool) -> None:
        """Apply one add/rename/remove/jump answer for the binding that asked."""
        if not self._bookmark_live() or not isinstance(value, tuple) or len(value) != 3:
            return
        player_pid, fid, ok = value
        if (player_pid, fid) != self._bookmark_pending:
            return
        self._bookmark_pending = None
        if not ok:
            self._bookmark_notice(failed)
            self._update_bookmark_actions()
            self._update_actions()
            return
        self._bookmark_notice(done)
        self._update_actions()
        if refresh:
            self._request_bookmarks(announce=False)
        else:
            self._update_bookmark_actions()

    def _render_bookmarks(self) -> None:
        tree = self._bookmark_tree
        if tree is None:
            return
        tree.delete(*tree.get_children())
        self._bookmark_rows = {}
        self._group_rows = {}
        if self._external is not None and self._external.get('scope') == 'all' and self._group is None:
            for index, group in enumerate(bookmark_groups(self._bookmarks)):
                iid = f'group{index}'
                tree.insert('', 'end', iid=iid, values=('📁 ' + group['name'], '', f"{group['count']} 个精彩点", group['category']))
                self._group_rows[iid] = group['parent']
            self._refresh_bookmark_header()
            self._update_bookmark_actions()
            return
        for index, entry in enumerate(self._bookmarks):
            if self._external is not None and self._external.get('scope') == 'all' and self._group is not None and group_key(entry) != self._group:
                continue
            bookmark_id = bookmark_id_of(entry)
            if bookmark_id is None:
                continue
            iid = f"mark{index}"
            position = format_duration_ms(entry.get("position_ms"))
            note = entry.get("note")
            tree.insert(
                "",
                "end",
                iid=iid,
                # An empty note is valid; the bookmark's own time point stands in
                # as its default name.
                values=('/'.join(part for part in (entry.get('video_prefix'), entry.get('name') or self._binding_name()) if part), position, note if isinstance(note, str) and note else position, entry.get('category', '未分类')),
            )
            self._bookmark_rows[iid] = bookmark_id
        self._refresh_bookmark_header()
        self._update_bookmark_actions()

    def _enter_group_or_jump(self, _event=None):
        selection = self._bookmark_tree.selection()
        iid = selection[0] if selection else None
        if iid in self._group_rows:
            self._group = self._group_rows[iid]
            self._render_bookmarks()
        else:
            self._jump_bookmark()

    def _back_to_groups(self):
        if self._external is not None and self._external.get('scope') == 'all':
            self._group = None
            self._render_bookmarks()
        else:
            self._open_all()

    def _refresh_bookmark_header(self) -> None:
        if self._bookmark_header_var is None:
            return
        prefix = "全部视频" if self._external is not None and self._external.get("scope") == "all" else f"当前视频：{self._binding_name()}"
        if self._external is not None and self._external.get('scope') == 'parent':
            prefix = '当前目录：' + self._binding_name()
        elif self._group is not None:
            group = next((g for g in bookmark_groups(self._bookmarks) if g['parent'] == self._group), None)
            prefix = '剧集：' + (group['name'] if group else '未分类')
        visible = [entry for entry in self._bookmarks if group_key(entry) == self._group] if self._group is not None else self._bookmarks
        header = f"{prefix}　精彩标记：{len(visible)} 条"
        if self._external is not None and self._external_action_target() is None:
            # The list/rename/remove work offline; add/jump cannot, so say so
            # right where the operator looks for the video.
            header = f"{header}　{_BOOKMARK_EXTERNAL_REASON}"
        self._bookmark_header_var.set(header)

    def _on_bookmark_select(self, _event: Any = None) -> None:
        self._update_bookmark_actions()

    def _update_bookmark_actions(self) -> None:
        if self._root is None or not self._bookmark_live():
            return
        busy = self._bookmark_pending is not None
        if self._external is not None:
            listing = self._connected and not busy
            acting = self._external_action_target() is not None and not busy
        else:
            listing = bookmark_enabled(self._connected, self._playback, self._selected, busy)
            acting = bookmark_action_enabled(self._connected, self._playback, self._selected, busy)
        # Jump needs a row of the *attached* list of the current binding: before
        # that list has arrived -- or after it was dropped -- nothing can be
        # selected, so jump stays disabled exactly like rename and remove.
        chosen = self._selected_bookmark() is not None
        jumpable = acting
        if self._external is not None:
            entry = self._selected_bookmark_entry()
            fid = entry.get("fid", self._external["fid"]) if entry else ""
            jumpable = not busy and (external_action_target(self._playback, fid) is not None or self._can_open_bookmark())
        _toggle(self._bookmark_add_button, acting)
        _toggle(self._bookmark_jump_button, listing and chosen and jumpable)
        _toggle(self._bookmark_rename_button, listing and chosen)
        _toggle(self._bookmark_remove_button, listing and chosen)
        _toggle(self._bookmark_refresh_button, listing)

    def _ask_note(self, title: str, prompt: str, initial: str = "") -> str | None:
        """A modal note prompt on the Tk thread; ``None`` when it cannot answer.

        ``simpledialog`` re-enters ``mainloop``, so worker events (including a
        selection change) are handled while the operator types; callers must
        re-check their target afterwards.
        """
        dialog = self._simpledialog
        if dialog is None:
            return None
        try:
            answer = dialog.askstring(
                title, prompt, parent=self._bookmark_window, initialvalue=initial
            )
        except Exception:
            return None
        return None if answer is None else answer.strip()

    def _add_bookmark(self) -> None:
        """Bookmark the player's current position for the selected binding."""
        target = self._action_target()
        if target is None:
            return
        account_id = self._account_id
        note = self._ask_note("标记当前时间点", "记录此处精彩的原因（可留空，最多 200 字）：")
        if note is None:
            return
        if len(note) > MAX_NOTE_CHARS:
            self._bookmark_notice(_BOOKMARK_NOTE_TOO_LONG)
            return
        if account_id != self._account_id or target != self._action_target():
            self._bookmark_notice(_BOOKMARK_STALE)
            return
        self._bookmark_pending = target
        self._bookmark_notice(_BOOKMARK_WORKING)
        self._update_bookmark_actions()
        self._update_actions()
        self._queue_command(("bookmark_add", target[0], target[1], note))

    def _rename_bookmark(self) -> None:
        """Rename the selected bookmark's note."""
        target = self._list_target()
        bookmark_id = self._selected_bookmark()
        if target is None or bookmark_id is None:
            return
        note = self._ask_note("编辑精彩标记备注", "请输入新的备注（可留空，最多 200 字）：", self._note_of(bookmark_id))
        if note is None:
            return
        if len(note) > MAX_NOTE_CHARS:
            self._bookmark_notice(_BOOKMARK_NOTE_TOO_LONG)
            return
        if target != self._list_target() or bookmark_id != self._selected_bookmark():
            self._bookmark_notice(_BOOKMARK_STALE)
            return
        self._bookmark_pending = target
        self._bookmark_notice(_BOOKMARK_WORKING)
        self._update_bookmark_actions()
        self._update_actions()
        self._queue_command(("bookmark_rename", target[0], target[1], bookmark_id, note))

    def _remove_bookmark(self) -> None:
        """Remove the selected bookmark."""
        target = self._list_target()
        bookmark_id = self._selected_bookmark()
        if target is None or bookmark_id is None:
            return
        self._bookmark_pending = target
        self._bookmark_notice(_BOOKMARK_WORKING)
        self._update_bookmark_actions()
        self._update_actions()
        self._queue_command(("bookmark_remove", target[0], target[1], bookmark_id))

    def _jump_bookmark(self) -> None:
        """Ask the player to seek to the selected bookmark."""
        list_target = self._list_target()
        if list_target is None:
            return
        bookmark_id = self._selected_bookmark()
        target = self._action_target()
        entry = next((row for row in self._bookmarks if bookmark_id_of(row) == bookmark_id), None)
        if entry is None or bookmark_id is None:
            return
        if self._external is not None:
            target = external_action_target(self._playback, entry.get("fid", self._external["fid"]))
            if target is None:
                if not self._can_open_bookmark():
                    return
                target = list_target
                self._bookmark_pending = target
                self._bookmark_notice(_BOOKMARK_WORKING)
                self._update_bookmark_actions()
                self._queue_command(("bookmark_play", target[0], target[1], bookmark_id))
                return
        if target is None:
            return
        self._bookmark_pending = target
        self._bookmark_notice(_BOOKMARK_WORKING)
        self._update_bookmark_actions()
        self._update_actions()
        self._queue_command(("bookmark_jump", target[0], target[1], bookmark_id))

    def _note_of(self, bookmark_id: Any) -> str:
        """The stored note of one bookmark id, used as the rename default."""
        for entry in self._bookmarks:
            if bookmark_id_of(entry) == bookmark_id:
                note = entry.get("note")
                return note if isinstance(note, str) else ""
        return ""


def run_highlights_window() -> int:
    from .gui_common import load_endpoint
    from .plugin_state import read_runtime
    endpoint = load_endpoint(read_runtime)
    client = HighlightsClient(endpoint) if endpoint is not None else None
    return HighlightsPanel(client, endpoint).run()

