"""Home screen — connection settings, server stats, create/join room."""

import sys
import os
import threading

_CLIENT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'client')
if _CLIENT_DIR not in sys.path:
    sys.path.append(_CLIENT_DIR)

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.timer import Timer
from textual.widgets import (
    Button, Checkbox, Footer, Header, Input, Label,
    Static, TabbedContent, TabPane,
)
from textual.containers import Horizontal, Vertical

from network import Connection, connect_direct, connect_tor


class HomeScreen(Screen):
    """Connection settings, live server stats, create/join room."""

    BINDINGS = [
        Binding("ctrl+r", "refresh_stats", "Refresh Stats"),
    ]

    DEFAULT_CSS = """
    HomeScreen { layout: vertical; }

    /* ── Connection panel ────────────────────────────────────────────── */
    #conn-panel {
        height: auto;
        border: tall $primary;
        margin: 1 1 0 1;
    }
    #conn-row1, #conn-row2 {
        height: 3;
        align: left middle;
        padding: 0 1;
    }
    #conn-panel Label    { color: $text-muted; margin-right: 1; }
    #conn-panel Input    { margin-right: 1; }
    #conn-panel Checkbox { margin: 0 1; }
    #host     { width: 18; }
    #port     { width: 10; }
    #dir-host { width: 18; }
    #dir-port { width: 10; }
    #connect-btn { margin-right: 1; }
    #conn-status { color: $text-muted; }
    .conn-ok  { color: $success; }
    .conn-err { color: $error;   }

    /* ── Main area ───────────────────────────────────────────────────── */
    #main { height: 1fr; margin: 1; }

    /* ── Stats sidebar ───────────────────────────────────────────────── */
    #stats-panel {
        width: 28;
        border: tall $primary;
        margin-right: 1;
        padding: 1;
    }
    #stats-display {
        height: 1fr;
        color: $text;
    }
    #refresh-btn { width: 100%; margin-top: 1; }

    /* ── Status bar ──────────────────────────────────────────────────── */
    #status-bar {
        height: 1;
        background: $boost;
        color: $text-muted;
        padding: 0 1;
    }
    #status-bar.error { color: $error; }

    .hidden { display: none; }
    """

    def __init__(self, initial_conn: Connection | None = None):
        super().__init__()
        self._conn: Connection | None = initial_conn
        self._stats_timer: Timer | None = None

    # ── Compose ───────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header()

        # Connection settings
        with Vertical(id="conn-panel"):
            with Horizontal(id="conn-row1"):
                yield Label("Server:")
                yield Input("127.0.0.1", id="host")
                yield Label("Port:")
                yield Input("8001", id="port")
                yield Checkbox("Via Tor", id="tor-check")
                yield Button("Connect", id="connect-btn", variant="primary")
                yield Static("", id="conn-status")
            with Horizontal(id="conn-row2", classes="hidden"):
                yield Label("Dir Host:")
                yield Input("127.0.0.1", id="dir-host")
                yield Label("Dir Port:")
                yield Input("8000", id="dir-port")

        # Main split: stats + tabbed forms
        with Horizontal(id="main"):
            with Vertical(id="stats-panel"):
                yield Static("—", id="stats-display")
                yield Button("Refresh Stats", id="refresh-btn")

            with TabbedContent():
                with TabPane("Create Room", id="tab-create"):
                    yield Label("Username")
                    yield Input(placeholder="Your display name", id="create-username")
                    yield Button("Create Room", id="create-btn", variant="success")

                with TabPane("Join Room", id="tab-join"):
                    yield Label("Username")
                    yield Input(placeholder="Your display name", id="join-username")
                    yield Label("Room Code")
                    yield Input(placeholder="Paste room code here", id="join-code")
                    yield Button("Join Room", id="join-btn", variant="success")

        yield Static("Fill in settings and press Connect.", id="status-bar")
        yield Footer()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def on_mount(self) -> None:
        self.query_one("#stats-panel").border_title = "Statistics"
        if self._conn is not None:
            self._mark_connected()
            self._start_stats_refresh(delay=0)
        else:
            self._set_room_buttons_enabled(False)

    def on_unmount(self) -> None:
        self._cancel_stats_refresh()

    # ── Tor toggle ────────────────────────────────────────────────────────────

    @on(Checkbox.Changed, "#tor-check")
    def on_tor_toggled(self, event: Checkbox.Changed) -> None:
        row2 = self.query_one("#conn-row2")
        if event.value:
            row2.remove_class("hidden")
        else:
            row2.add_class("hidden")
        if self._conn is not None:
            self._set_status("Settings changed — reconnect to apply.")

    # ── Button handlers ───────────────────────────────────────────────────────

    def on_button_pressed(self, event: Button.Pressed) -> None:
        match event.button.id:
            case "connect-btn":  self._manual_connect()
            case "refresh-btn":  self._fetch_stats(manual=True)
            case "create-btn":   self._do_create()
            case "join-btn":     self._do_join()

    def action_refresh_stats(self) -> None:
        self._fetch_stats(manual=True)

    # ── Connection management ─────────────────────────────────────────────────

    def _get_params(self):
        host = self.query_one("#host", Input).value.strip()
        try:
            port = int(self.query_one("#port", Input).value.strip())
        except ValueError:
            raise ValueError("Invalid server port")
        use_tor = self.query_one("#tor-check", Checkbox).value
        dir_host = self.query_one("#dir-host", Input).value.strip()
        try:
            dir_port = int(self.query_one("#dir-port", Input).value.strip())
        except ValueError:
            dir_port = 8000
        return host, port, use_tor, dir_host, dir_port

    def _manual_connect(self) -> None:
        if self._conn is not None:
            old, self._conn = self._conn, None
            threading.Thread(target=old.close, daemon=True).start()

        self._cancel_stats_refresh()
        self._set_status("Connecting...")
        self.query_one("#connect-btn", Button).disabled = True
        self._set_room_buttons_enabled(False)
        self._reset_stats_display()

        try:
            params = self._get_params()
        except ValueError as e:
            self._set_status(str(e), error=True)
            self.query_one("#connect-btn", Button).disabled = False
            return

        self._connect_worker(*params)

    @work(thread=True)
    def _connect_worker(self, host: str, port: int, use_tor: bool,
                        dir_host: str, dir_port: int) -> None:
        try:
            if use_tor:
                self.app.call_from_thread(self._set_status, "[Tor] Building circuit...")
                conn = connect_tor(dir_host, dir_port, host, port)
            else:
                conn = connect_direct(host, port)
            self.app.call_from_thread(self._on_connect_success, conn)
        except Exception as e:
            self.app.call_from_thread(self._on_connect_fail, str(e))

    def _on_connect_success(self, conn: Connection) -> None:
        self._conn = conn
        self.app.set_conn(conn)
        self._mark_connected()
        self.query_one("#connect-btn", Button).disabled = False
        self._set_room_buttons_enabled(True)
        self._start_stats_refresh(delay=0)

    def _on_connect_fail(self, err: str) -> None:
        self._set_status(f"Connection failed: {err}", error=True)
        status = self.query_one("#conn-status", Static)
        status.update("● Disconnected")
        status.remove_class("conn-ok")
        status.add_class("conn-err")
        self.query_one("#connect-btn", Button).disabled = False

    def _mark_connected(self) -> None:
        label = "Tor" if self._conn and self._conn.is_tor else "Direct"
        status = self.query_one("#conn-status", Static)
        status.update(f"● Connected ({label})")
        status.add_class("conn-ok")
        status.remove_class("conn-err")
        self._set_status(f"Connected ({label}). Stats auto-refresh every 5 s.")
        self._set_room_buttons_enabled(True)

    def _set_room_buttons_enabled(self, enabled: bool) -> None:
        for btn_id in ("create-btn", "join-btn", "refresh-btn"):
            try:
                self.query_one(f"#{btn_id}", Button).disabled = not enabled
            except Exception:
                pass

    # ── Stats ─────────────────────────────────────────────────────────────────

    def _reset_stats_display(self) -> None:
        self.query_one("#stats-display", Static).update("—")

    def _start_stats_refresh(self, delay: float = 5.0) -> None:
        self._cancel_stats_refresh()
        if delay == 0:
            self._fetch_stats()
        else:
            self._stats_timer = self.set_timer(delay, self._fetch_stats)

    def _cancel_stats_refresh(self) -> None:
        if self._stats_timer is not None:
            self._stats_timer.stop()
            self._stats_timer = None

    def _fetch_stats(self, manual: bool = False) -> None:
        if self._conn is None:
            return
        if manual:
            btn = self.query_one("#refresh-btn", Button)
            btn.label = "Refreshing..."
            btn.disabled = True
        self._fetch_stats_worker()

    @work(thread=True)
    def _fetch_stats_worker(self) -> None:
        try:
            conn = self._conn
            if conn is None:
                return
            conn.send_to('GetStats', {})
            resp = conn.recv_one()
            if resp and resp.get('type') == 'Stats':
                self.app.call_from_thread(self._apply_stats, resp['data'])
        except Exception as e:
            self.app.call_from_thread(self._set_status, f"Stats error: {e}", True)
        finally:
            self.app.call_from_thread(self._after_stats_fetch)

    def _after_stats_fetch(self) -> None:
        try:
            btn = self.query_one("#refresh-btn", Button)
            btn.label = "Refresh Stats"
            btn.disabled = False
        except Exception:
            pass
        self._start_stats_refresh(delay=5.0)

    def _apply_stats(self, stats: dict) -> None:
        def v(key):
            return str(stats.get(key, "—"))
        text = (
            f"Messages:    {v('total_messages'):>6}\n"
            f"Files Sent:  {v('total_files'):>6}\n"
            f"Total Users: {v('total_users'):>6}\n"
            f"Total Rooms: {v('total_rooms'):>6}"
        )
        try:
            self.query_one("#stats-display", Static).update(text)
        except Exception:
            pass

    # ── Create / Join ─────────────────────────────────────────────────────────

    def _do_create(self) -> None:
        if self._conn is None:
            self._set_status("Not connected — press Connect first.", error=True)
            return
        username = self.query_one("#create-username", Input).value.strip()
        if not username:
            self._set_status("Username is required.", error=True)
            return
        self._cancel_stats_refresh()
        self._set_room_buttons_enabled(False)
        self._set_status("Creating room...")
        self._create_worker(username)

    @work(thread=True)
    def _create_worker(self, username: str) -> None:
        conn = self._conn
        try:
            conn.send_to('CreateRoom', {'my_username': username})
            resp = conn.recv_one()
            if resp is None:
                raise RuntimeError("Server disconnected")
            if resp.get('type') == 'Error':
                raise RuntimeError(resp['data']['error_message'])
            room_code = resp['data']['room_code']
            members   = resp['data']['users']
            self.app.call_from_thread(self.app.show_chat, conn, username, room_code, members)
        except Exception as e:
            self.app.call_from_thread(self._set_status, f"Error: {e}", True)
            self.app.call_from_thread(self._set_room_buttons_enabled, True)
            self.app.call_from_thread(self._start_stats_refresh)

    def _do_join(self) -> None:
        if self._conn is None:
            self._set_status("Not connected — press Connect first.", error=True)
            return
        username  = self.query_one("#join-username", Input).value.strip()
        room_code = self.query_one("#join-code", Input).value.strip()
        if not username:
            self._set_status("Username is required.", error=True)
            return
        if not room_code:
            self._set_status("Room code is required.", error=True)
            return
        self._cancel_stats_refresh()
        self._set_room_buttons_enabled(False)
        self._set_status("Joining room...")
        self._join_worker(username, room_code)

    @work(thread=True)
    def _join_worker(self, username: str, room_code: str) -> None:
        conn = self._conn
        try:
            conn.send_to('JoinRoom', {'room_code': room_code, 'my_username': username})
            resp = conn.recv_one()
            if resp is None:
                raise RuntimeError("Server disconnected")
            if resp.get('type') == 'Error':
                raise RuntimeError(resp['data']['error_message'])
            members = resp['data']['users']
            self.app.call_from_thread(self.app.show_chat, conn, username, room_code, members)
        except Exception as e:
            self.app.call_from_thread(self._set_status, f"Error: {e}", True)
            self.app.call_from_thread(self._set_room_buttons_enabled, True)
            self.app.call_from_thread(self._start_stats_refresh)

    # ── Status bar ────────────────────────────────────────────────────────────

    def _set_status(self, text: str, error: bool = False) -> None:
        try:
            bar = self.query_one("#status-bar", Static)
            bar.update(text)
            if error:
                bar.add_class("error")
            else:
                bar.remove_class("error")
        except Exception:
            pass
