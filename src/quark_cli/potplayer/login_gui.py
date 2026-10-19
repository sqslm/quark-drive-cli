"""The visible Tkinter QR window that ``--login`` and the bridge open.

Network polling runs on a worker thread; every Tk call stays on the thread that
created the window (the process main thread for ``--login``, the bridge's login
thread for the service). Closing the window cancels the in-flight poll and never
writes a credential.

Tk objects are bound to the thread that created them: the interpreter aborts the
process when a ``Tk``/``PhotoImage`` is finalised anywhere else (``Tcl_AsyncDelete:
async handler deleted by the wrong thread``). :meth:`QrLoginWindow.run` therefore
releases every Tk object in a ``finally`` block on that same thread instead of
letting a later GC pass on some other thread find them.
"""

from __future__ import annotations

import contextlib
import queue
import threading
from pathlib import Path
from typing import Any, Callable

from ..auth import LoginCancelled, QRExpired, QRLogin
from ..errors import LocalIOError, QuarkError
from .window_ui import configure_window, present_window

# A code that keeps expiring is retried a few times, then reported as failed
# instead of requesting a new one forever.
MAX_REFRESHES = 5

# Generated codes are ~610-690 px; the window shrinks them with an integer
# ``subsample`` factor so the whole square (quiet zone included) stays visible
# on ordinary screens without pulling in an image-resizing dependency.
MAX_QR_PIXELS = 360


class LoginRunner:
    """Run one QR attempt on a worker thread and report events to ``sink``.

    ``sink(kind, value)`` is called from the worker thread with one of
    ``qr``/``success``/``cancelled``/``expired``/``failed``; it must only hand
    the event to the Tk thread (a :class:`queue.Queue` satisfies that).
    """

    def __init__(self, new_login: Callable[[threading.Event], Any], sink: Callable[[str, Any], None]) -> None:
        self._new_login = new_login
        self._sink = sink
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._cancel = threading.Event()
        self._thread = threading.Thread(target=self._run, name="quark-potplayer-qr", daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self._cancel.set()

    def _run(self) -> None:
        try:
            self._new_login(self._cancel).run(on_qr_ready=lambda path: self._sink("qr", str(path)))
        except LoginCancelled:
            self._sink("cancelled", None)
        except QRExpired as exc:
            self._sink("expired", str(exc))
        except QuarkError as exc:
            self._sink("failed", exc)
        except OSError:
            self._sink("failed", LocalIOError("扫码登录过程失败"))
        else:
            self._sink("success", None)


def _tkinter() -> Any:
    try:
        import tkinter
    except ImportError:
        raise LocalIOError("缺少 Tkinter，无法显示扫码窗口") from None
    return tkinter


def _widgets(root: Any) -> list[Any]:
    """Every widget under ``root`` in memory (Tk.destroy keeps the links)."""
    found: list[Any] = []
    stack = [root]
    while stack:
        widget = stack.pop()
        found.append(widget)
        stack.extend(getattr(widget, "children", {}).values())
    return found


class QrLoginWindow:
    """A modal QR window: Tk on the calling thread, network on a worker thread.

    ``root_factory`` may return a ``Tk`` root (the CLI and the plugin's login
    thread) or a ``Toplevel`` of a window that already runs its own loop (the
    downloader window); :meth:`run` picks the matching event loop for either.
    """

    def __init__(
        self,
        *,
        transport: Any = None,
        cookie_path: Path | None = None,
        timeout: int = 300,
        login_factory: Callable[[threading.Event], Any] | None = None,
        root_factory: Callable[[], Any] | None = None,
        guard: Any = None,
    ) -> None:
        self._new_login = login_factory or (
            lambda cancel: QRLogin(
                transport=transport,
                timeout=timeout,
                cookie_path=cookie_path,
                cancelled=cancel.is_set,
                guard=guard,
            )
        )
        self._root_factory = root_factory
        self._events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._runner: LoginRunner | None = None
        self._root: Any = None
        self._pump_id: str | None = None
        self._tk: Any = None
        self._image: Any = None
        self._status: Any = None
        self._on_qr_ready: Callable[[Path], None] | None = None
        self._photo: Any = None
        self._refreshes = 0
        self._outcome: str | None = None
        self._failure: BaseException | None = None

    # -- lifecycle -------------------------------------------------------

    def run(self, *, on_qr_ready: Callable[[Path], None] | None = None) -> None:
        tk = self._tk = _tkinter()
        root = self._root = None
        holder = image = status = None
        try:
            try:
                root = self._root = (self._root_factory or tk.Tk)()
            except tk.TclError:
                raise LocalIOError("无法创建扫码窗口，请检查桌面环境") from None
            root.title("夸克网盘扫码登录")
            root.resizable(False, False)
            # The downloader embeds this login as a Toplevel; ttk themes belong
            # to its entire interpreter, so keep that host's existing style.
            plugin_window = isinstance(root, tk.Tk)
            configure_window(root, themed=plugin_window)
            # A bare Label's width/height are text units until an image is set;
            # a fixed-pixel Frame keeps the window small before the QR arrives.
            holder = tk.Frame(root, width=280, height=280)
            holder.pack(padx=18, pady=(18, 6))
            holder.pack_propagate(False)
            image = self._image = tk.Label(holder)
            image.pack()
            status = self._status = tk.Label(root, text="正在获取二维码……", wraplength=280, justify="center")
            status.pack(padx=18, pady=(0, 6))
            tk.Label(root, text="请用手机夸克 App 扫码，并在手机上确认登录",
                     fg="#aaaaaa" if plugin_window else '#444444').pack(padx=18)
            tk.Button(root, text="关闭", width=10, command=self.close).pack(pady=14)
            root.protocol("WM_DELETE_WINDOW", self.close)

            self._on_qr_ready = on_qr_ready
            self._runner = LoginRunner(self._new_login, self._post)
            self._runner.start()

            self._pump()
            # A window that already finished — a scan confirmed before the first
            # poll — destroyed itself in ``_handle``; there is no loop to run.
            if self._root is not None and self._outcome is None:
                self._run_loop(self._root, tk)
        finally:
            # Drop the Tk locals of this frame *before* an exception can carry the
            # traceback (and these widgets) into another thread's garbage.
            holder = image = status = root = None
            self._teardown()
        if self._outcome == "success":
            return
        if self._failure is not None:
            raise self._failure
        raise LoginCancelled("登录已取消")

    @staticmethod
    def _run_loop(root: Any, tk: Any) -> None:
        """Serve Tk events until this window is destroyed.

        A standalone ``Tk`` root owns the process event loop, so ``mainloop`` is
        the loop to run. An embedded ``Toplevel`` — the downloader window opens
        this panel as a modal child — must run a *nested* loop instead:
        ``mainloop`` on a ``Toplevel`` serves the whole interpreter and returns
        only once the **parent** window is destroyed, so the caller would never
        get control back while its own window is still open.
        """
        if isinstance(root, tk.Tk):
            root.mainloop()
        else:
            root.wait_window(root)

    def close(self) -> None:
        """Cancel the in-flight poll and tear the window down (Tk thread only)."""
        self._teardown()

    def request_close(self) -> None:
        """Ask the Tk thread to close the window; safe from any other thread."""
        self._events.put(("stop", None))

    def request_present(self) -> None:
        """Bring an existing QR window forward on its own Tk thread."""
        self._events.put(('present', None))

    def _teardown(self) -> None:
        """Release every Tk object on the thread that created the window."""
        runner, self._runner = self._runner, None
        if runner is not None:
            runner.cancel()
        root, self._root = self._root, None
        image, self._image = self._image, None
        pump_id, self._pump_id = self._pump_id, None
        # ``PhotoImage.__del__`` calls into Tcl, so drop our reference here, on
        # the window's own thread; no other thread's GC may finalise it.
        self._photo = None
        try:
            if root is not None:
                if pump_id is not None:
                    with contextlib.suppress(Exception):
                        root.after_cancel(pump_id)
                if image is not None:
                    with contextlib.suppress(Exception):
                        image.configure(image="")
                with contextlib.suppress(Exception):
                    root.destroy()
                # ``Tk.destroy`` leaves the widget tree linked through
                # ``children``/``master`` (a plain reference cycle). Unlink it
                # now so the Tk objects are freed on this thread instead of a
                # later GC pass running somewhere else.
                for widget in _widgets(root):
                    with contextlib.suppress(Exception):
                        widget.master = None
                        widget.children.clear()
        finally:
            self._status = None
            self._on_qr_ready = None
            self._tk = None

    # -- Tk-thread event handling ----------------------------------------

    def _post(self, kind: str, value: Any) -> None:
        """Hand a worker-thread event to the Tk thread."""
        self._events.put((kind, value))

    def _pump(self) -> None:
        """Poll the worker's events on the Tk thread.

        A bound method rather than a closure, so the Tk callback never forms a
        self-referential cycle around ``root``/the widgets.
        """
        root = self._root
        if root is None:
            return
        try:
            while True:
                kind, value = self._events.get_nowait()
                self._handle(kind, value)
                if self._outcome is not None or self._root is None:
                    return
        except queue.Empty:
            pass
        if self._root is not None and self._outcome is None:
            self._pump_id = root.after(120, self._pump)

    def _handle(self, kind: str, value: Any) -> None:
        if kind == 'present':
            if self._root is not None:
                present_window(self._root)
        elif kind == "qr":
            tk, image, status = self._tk, self._image, self._status
            try:
                photo = tk.PhotoImage(file=value)
            except tk.TclError:
                photo = None
            if photo is not None:
                photo = self._fit_qr(photo)
                self._photo = photo
                image.configure(image=photo)
                image.update_idletasks()
                image.master.configure(width=image.winfo_reqwidth(), height=image.winfo_reqheight())
            status.configure(text="请用手机夸克 App 扫码，并在手机上确认登录")
            if self._on_qr_ready is not None:
                self._on_qr_ready(Path(value))
        elif kind == "expired":
            self._refreshes += 1
            if self._refreshes >= MAX_REFRESHES:
                message = f"{value}；二维码多次过期，请关闭窗口后重试"
                self._failure = QRExpired(message)
                self._outcome = "failed"
                self._status.configure(text=message)
            else:
                self._status.configure(text="二维码已过期，正在刷新……")
                if self._runner is not None:
                    self._runner.start()
        elif kind == "failed":
            self._failure = value
            self._outcome = "failed"
            self._status.configure(text=str(value))
        elif kind == "success":
            self._outcome = "success"
            self._teardown()
        elif kind == "cancelled":
            self._outcome = "cancelled"
            self._teardown()
        elif kind == "stop":
            self._teardown()

    @staticmethod
    def _fit_qr(photo: Any) -> Any:
        """Shrink an oversized code to ``MAX_QR_PIXELS`` with integer subsampling."""
        longest = max(photo.width(), photo.height())
        factor = -(-longest // MAX_QR_PIXELS)
        if factor > 1:
            photo = photo.subsample(factor, factor)
        return photo
