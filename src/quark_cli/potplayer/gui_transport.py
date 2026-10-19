"""Loopback transport and per-page worker lifecycle, independent of page business logic."""
from __future__ import annotations
from typing import Any
import contextlib
import httpx
import queue
import threading
import time
from .window_ui import configure_window
from .gui_common import (
    EXIT_OK,
    Endpoint,
    HEALTH_PATH,
    POLL_INTERVAL,
    REQUEST_TIMEOUT,
    WINDOW_HEIGHT,
    WINDOW_WIDTH,
    _tkinter,
    _widgets,
)
class LocalClient:
    def __init__(
        self,
        endpoint: Endpoint,
        *,
        timeout: float = REQUEST_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._endpoint = endpoint
        self.account_id: str | None = None
        # ``trust_env=False``: environment proxies must never rewrite loopback.
        self._client = httpx.Client(
            base_url=endpoint.base_url,
            timeout=httpx.Timeout(timeout),
            trust_env=False,
            headers={"Authorization": f"Bearer {endpoint.token}"},
            transport=transport,
        )

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._client.close()

    def healthy(self) -> bool:
        """True when the recorded instance still answers ``/v1/health``."""
        try:
            response = self._client.get(HEALTH_PATH)
            if response.status_code != 200:
                return False
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            return False
        return (
            isinstance(payload, dict)
            and payload.get("status") == "ok"
            and payload.get("instance") == self._endpoint.instance
        )

    def _get_object(self, path: str, *, timeout: float | None = None) -> dict[str, Any] | None:
        try:
            response = self._client.get(path, headers=self._account_header(), **({'timeout': timeout} if timeout is not None else {}))
            if response.status_code != 200:
                return None
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def _post_accepted(self, path: str, body: dict[str, Any]) -> bool:
        """One JSON POST: any 2xx means the local bridge accepted the request.

        The response body is never inspected, so no upstream text (and no error
        message) can ever reach the window; failures are a plain ``False``.
        """
        try:
            response = self._client.post(path, json=body, headers=self._account_header())
        except httpx.HTTPError:
            return False
        return 200 <= response.status_code < 300

    def _account_header(self):
        return {} if self.account_id is None else {'X-Quark-Account': self.account_id}


class LocalWorker:
    def __init__(
        self,
        client: BridgeClient,
        events: queue.Queue,
        requests: queue.Queue,
        *,
        interval: float = POLL_INTERVAL,
        stop: threading.Event | None = None,
    ) -> None:
        self._client = client
        self._events = events
        self._requests = requests
        self._interval = interval
        self._stop = stop if stop is not None else threading.Event()
        self.polls = 0
        self._scope: str | None = None

    def _emit(self, kind, value):
        if self._scope is not None:
            self._events.put(('scoped', (self._scope, kind, value)))
        else:
            self._events.put((kind, value))

    def cancel(self) -> None:
        self._stop.set()
        self._requests.put(None)  # Wake an idle queue.get immediately.

    def tick(self) -> None:
        """Exactly one diagnostics GET; a failure is classified by a health probe."""
        if self._stop.is_set():
            return
        self.polls += 1
        snapshot = self._client.snapshot()
        if self._stop.is_set():
            return
        if snapshot is not None:
            self._events.put(("snapshot", snapshot))
            return
        if self._client.healthy():
            self._events.put(("failed", None))
        else:
            self._events.put(("disconnected", None))

    def run(self) -> None:
        try:
            while not self._stop.is_set():
                self.tick()
                deadline = time.monotonic() + self._interval
                while not self._stop.is_set():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    try:
                        request = self._requests.get(timeout=remaining)
                    except queue.Empty:
                        break
                    if self._stop.is_set():
                        break
                    self._serve(request)
        finally:
            # Only this thread touches its active HTTP transport. No Tk objects
            # are owned by the worker; closing the window never waits for I/O.
            self._client.close()


    def _serve(self, request):
        if request and request[0] == 'scoped':
            _, scope, command = request
            self._scope = scope
            self._client.account_id = scope
            try:
                self._command(command)
            finally:
                self._scope = None
                self._client.account_id = None
        else:
            self._command(request)

class LocalPanel:
    """One page's lifecycle: its own client, worker, queues and Tk callbacks."""
    worker_type = LocalWorker
    title = '夸克网盘插件管理'

    def __init__(self, client, endpoint):
        self._client, self._endpoint = client, endpoint
        self._events, self._requests = queue.Queue(), queue.Queue()
        self._worker = self._thread = self._root = self._tk = self._frame = None
        self._pump_id = None
        self._stopping = False
        self._connected = False
        self._account_id = None
        self._snapshot = None

    def mount(self, tk, root, frame):
        self._tk, self._root, self._frame = tk, root, frame
        self._build(tk, frame)
        if self._client is not None:
            self._worker = self.worker_type(self._client, self._events, self._requests)
            self._thread = threading.Thread(target=self._worker.run, daemon=True)
            self._thread.start()
        else:
            self._show_disconnected()
        self._pump()

    def run(self):
        tk = _tkinter()
        root = tk.Tk()
        root.title(self.title)
        root.geometry(f'{WINDOW_WIDTH}x{WINDOW_HEIGHT}')
        root.protocol('WM_DELETE_WINDOW', self._close)
        try:
            configure_window(root)
            self.mount(tk, root, root)
            root.mainloop()
        finally:
            self._shutdown()
            root.destroy()
            for widget in _widgets(root):
                widget.master = None
                widget.children.clear()
        return EXIT_OK

    def _pump(self):
        if self._stopping or self._root is None:
            return
        try:
            while True:
                kind, value = self._events.get_nowait()
                self._handle(kind, value)
        except queue.Empty:
            pass
        self._pump_id = self._root.after(200, self._pump)

    def _shutdown(self):
        self._stopping = True
        if self._pump_id is not None and self._root is not None:
            self._root.after_cancel(self._pump_id)
        if self._worker is not None:
            self._worker.cancel()
        if self._worker is None and self._client is not None:
            self._client.close()
        import tkinter
        for name, value in list(vars(self).items()):
            if isinstance(value, (tkinter.Widget, tkinter.Variable)):
                setattr(self, name, None)
        self._root = self._tk = self._frame = None

    def _queue_command(self, command):
        if self._account_id is None:
            self._requests.put(command)
        else:
            self._requests.put(('scoped', self._account_id, command))

    def _close(self):
        if self._root is not None:
            self._root.withdraw()
            self._root.quit()
