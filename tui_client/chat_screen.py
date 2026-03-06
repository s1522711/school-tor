"""Chat room screen — messages, user list, send bar, file send/receive."""

import sys
import base64
import os

_CLIENT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'client')
if _CLIENT_DIR not in sys.path:
    sys.path.append(_CLIENT_DIR)

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Header, Input, Label, ListItem, ListView, RichLog, Static
from textual.containers import Horizontal, Vertical
from rich.text import Text

from network import Connection


# ── File path modal ───────────────────────────────────────────────────────────

class FilePathModal(ModalScreen[str]):
    """Small modal prompting for a file-system path."""

    DEFAULT_CSS = """
    FilePathModal { align: center middle; }
    #dialog {
        width: 64;
        height: auto;
        border: tall $accent;
        background: $surface;
        padding: 1 2;
    }
    #dialog Label  { margin-bottom: 1; }
    #btn-row        { height: 3; margin-top: 1; }
    #btn-row Button { width: 1fr; margin: 0 1; }
    """

    def __init__(self, prompt: str = "Enter file path:"):
        super().__init__()
        self._prompt = prompt

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self._prompt)
            yield Input(placeholder="/path/to/file", id="path-input")
            with Horizontal(id="btn-row"):
                yield Button("OK", id="ok-btn", variant="primary")
                yield Button("Cancel", id="cancel-btn")

    def on_mount(self) -> None:
        self.query_one("#path-input", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ok-btn":
            self.dismiss(self.query_one("#path-input", Input).value.strip())
        else:
            self.dismiss("")

    @on(Input.Submitted, "#path-input")
    def on_path_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip())


# ── Chat screen ───────────────────────────────────────────────────────────────

class ChatScreen(Screen):
    """In-room chat: message log, user list, send bar, file transfer."""

    BINDINGS = [
        Binding("ctrl+l", "leave_room",  "Leave Room",  priority=True),
        Binding("ctrl+a", "attach_file", "Attach File", priority=True),
        Binding("ctrl+k", "copy_code",   "Copy Code",   priority=True),
    ]

    DEFAULT_CSS = """
    ChatScreen { layout: vertical; }

    /* ── Main split ───────────────────────────────────────────────────── */
    #main { height: 1fr; }

    #messages {
        width: 1fr;
        border: tall $primary;
        margin: 0 1 0 0;
    }

    /* ── User panel ───────────────────────────────────────────────────── */
    #user-panel {
        width: 20;
        border: tall $primary;
    }
    #user-list { height: 1fr; }
    #user-list ListItem { padding: 0 1; }

    /* ── Room bar ─────────────────────────────────────────────────────── */
    #room-bar {
        height: 1;
        background: $boost;
        color: $text-muted;
        padding: 0 1;
    }

    /* ── Input bar ────────────────────────────────────────────────────── */
    #input-bar {
        dock: bottom;
        height: 3;
        background: $boost;
        border-top: tall $primary-darken-2;
        align: left middle;
        padding: 0 1;
    }
    #msg-input  { width: 1fr; margin: 0 1; }
    #send-btn   { width: 10; }
    #attach-btn { width: 10; }
    """

    def __init__(self, connection: Connection, username: str,
                 room_code: str, members: list):
        super().__init__()
        self._conn      = connection
        self._username  = username
        self._room_code = room_code
        self._members   = list(members)
        self._leaving   = False
        self._received_files: list[tuple[str, str]] = []
        self._user_items: dict[str, ListItem] = {}

    # ── Compose ───────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="room-bar")

        with Horizontal(id="main"):
            yield RichLog(id="messages", highlight=False, markup=False, wrap=True)
            with Vertical(id="user-panel"):
                yield ListView(id="user-list")

        with Horizontal(id="input-bar"):
            yield Button("Attach", id="attach-btn")
            yield Input(
                placeholder="Message... (or /save N to save a received file)",
                id="msg-input",
            )
            yield Button("Send", id="send-btn", variant="primary")

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def on_mount(self) -> None:
        self.query_one("#room-bar", Static).update(
            f"Room: {self._room_code}  │  Ctrl+L: Leave  Ctrl+A: Attach  Ctrl+K: Copy Code"
        )
        self.query_one("#user-panel").border_title = "Users"

        lst = self.query_one("#user-list", ListView)
        for m in self._members:
            item = ListItem(Label(m), name=m)
            self._user_items[m] = item
            lst.append(item)

        self._conn.start_receiver(
            on_message=lambda t, d: self.app.call_from_thread(self._handle_message, t, d),
            on_disconnect=lambda: self.app.call_from_thread(self._handle_disconnect),
        )

        log = self.query_one("#messages", RichLog)
        log.write(Text.assemble(("  Joined room ", "dim"), (self._room_code, "bold cyan")))
        log.write(Text.assemble(("  Members: ", "dim"), (", ".join(self._members), "dim italic")))

        self.query_one("#msg-input", Input).focus()

    def on_unmount(self) -> None:
        if not self._leaving:
            self._conn.stop_receiver()

    # ── Bindings / actions ────────────────────────────────────────────────────

    def action_leave_room(self) -> None:
        self._do_leave()

    def action_attach_file(self) -> None:
        self._pick_file()

    def action_copy_code(self) -> None:
        self._copy_room_code()

    # ── Button / input handlers ───────────────────────────────────────────────

    def on_button_pressed(self, event: Button.Pressed) -> None:
        match event.button.id:
            case "send-btn":   self._send_message()
            case "attach-btn": self._pick_file()

    @on(Input.Submitted, "#msg-input")
    def on_msg_submitted(self, _: Input.Submitted) -> None:
        self._send_message()

    # ── User list helpers ─────────────────────────────────────────────────────

    def _add_user(self, username: str) -> None:
        if username in self._user_items:
            return
        self._members.append(username)
        item = ListItem(Label(username), name=username)
        self._user_items[username] = item
        self.query_one("#user-list", ListView).append(item)

    def _remove_user(self, username: str) -> None:
        self._members = [m for m in self._members if m != username]
        item = self._user_items.pop(username, None)
        if item:
            item.remove()

    # ── Log helpers ───────────────────────────────────────────────────────────

    def _log_chat(self, sender: str, message: str, is_self: bool = False) -> None:
        color = "bold green" if is_self else "bold blue"
        self.query_one("#messages", RichLog).write(
            Text.assemble((sender, color), (": ", ""), (message, "")))

    def _log_system(self, text: str) -> None:
        self.query_one("#messages", RichLog).write(
            Text.assemble(("  " + text, "dim italic")))

    def _log_file(self, text: str) -> None:
        self.query_one("#messages", RichLog).write(
            Text.assemble(("  " + text, "yellow italic")))

    def _log_error(self, text: str) -> None:
        self.query_one("#messages", RichLog).write(
            Text.assemble(("  Error: " + text, "bold red")))

    # ── Incoming message handler ──────────────────────────────────────────────

    def _handle_message(self, msg_type: str, data: dict) -> None:
        match msg_type:
            case 'IncomingMessage':
                self._log_chat(data.get('from_username', '?'), data.get('message', ''))

            case 'IncomingFile':
                raw_b64  = data.get('filedata', '')
                filename = data.get('filename', 'file')
                sender   = data.get('from_username', '?')
                approx   = f"~{len(raw_b64) * 3 // 4 // 1024} KB" if raw_b64 else ""
                idx = len(self._received_files) + 1
                self._received_files.append((filename, raw_b64))
                self._log_file(
                    f"[{idx}] {sender} sent: {filename} ({approx})"
                    f"  —  /save {idx} to save"
                )

            case 'UserJoined':
                u = data.get('username', '')
                self._add_user(u)
                self._log_system(f"*** {u} joined the room ***")

            case 'UserLeft':
                u = data.get('username', '')
                self._remove_user(u)
                self._log_system(f"*** {u} left the room ***")

            case 'RoomLeft':
                self._log_system("You left the room.")
                self._cleanup_and_leave()

            case 'Stats':
                self._log_system(
                    f"Stats — messages: {data.get('total_messages')}  "
                    f"files: {data.get('total_files')}  "
                    f"users: {data.get('total_users')}  "
                    f"rooms: {data.get('total_rooms')}"
                )

            case 'Error':
                self._log_error(data.get('error_message', 'Unknown error'))

    def _handle_disconnect(self) -> None:
        if self._leaving:
            return
        self._log_error("Disconnected from server.")
        for btn_id in ("send-btn", "attach-btn"):
            self.query_one(f"#{btn_id}", Button).disabled = True
        self.query_one("#msg-input", Input).disabled = True

    # ── Actions ───────────────────────────────────────────────────────────────

    def _copy_room_code(self) -> None:
        try:
            self.app.copy_to_clipboard(self._room_code)
            self._log_system(f"Room code copied: {self._room_code}")
        except Exception:
            self._log_system(f"Room code: {self._room_code}")

    def _send_message(self) -> None:
        inp  = self.query_one("#msg-input", Input)
        text = inp.value.strip()
        if not text:
            return
        inp.clear()

        if text.startswith("/save"):
            parts = text.split(None, 1)
            if len(parts) < 2:
                self._log_error("Usage: /save <number>")
                return
            try:
                idx = int(parts[1]) - 1
            except ValueError:
                self._log_error("Usage: /save <number>")
                return
            if 0 <= idx < len(self._received_files):
                self._save_file_dialog(idx)
            else:
                self._log_error(
                    f"No file #{parts[1]}. "
                    f"You have {len(self._received_files)} received file(s).")
            return

        self._log_chat("you", text, is_self=True)
        self._send_msg_worker(text)

    @work(thread=True)
    def _send_msg_worker(self, text: str) -> None:
        try:
            self._conn.send_to('SendMessage', {'message': text})
        except Exception as e:
            self.app.call_from_thread(self._log_error, f"Send failed: {e}")

    # ── File sending ──────────────────────────────────────────────────────────

    def _pick_file(self) -> None:
        def on_result(path: str) -> None:
            if path:
                self._send_file_worker(path)
        self.app.push_screen(FilePathModal("Enter path of file to send:"), on_result)

    @work(thread=True)
    def _send_file_worker(self, path: str) -> None:
        try:
            with open(path, 'rb') as f:
                raw = f.read()
            filename = os.path.basename(path)
            filedata = base64.b64encode(raw).decode()
            size_str = f"{len(raw) / 1024:.1f} KB"
            self.app.call_from_thread(self._log_system, f"Sending {filename} ({size_str})...")
            self._conn.send_to('SendFile', {'filename': filename, 'filedata': filedata})
            self.app.call_from_thread(self._log_system, f"Sent {filename} ({size_str})")
        except FileNotFoundError:
            self.app.call_from_thread(self._log_error, f"File not found: {path}")
        except Exception as e:
            self.app.call_from_thread(self._log_error, f"File send failed: {e}")

    # ── File saving ───────────────────────────────────────────────────────────

    def _save_file_dialog(self, idx: int) -> None:
        filename, filedata = self._received_files[idx]

        def on_result(path: str) -> None:
            if path:
                self._save_file_worker(path, filedata)

        self.app.push_screen(
            FilePathModal(f"Save '{filename}' — enter destination path:"), on_result)

    @work(thread=True)
    def _save_file_worker(self, path: str, filedata: str) -> None:
        try:
            with open(path, 'wb') as f:
                f.write(base64.b64decode(filedata))
            self.app.call_from_thread(self._log_system, f"Saved: {os.path.basename(path)}")
        except Exception as e:
            self.app.call_from_thread(self._log_error, f"Could not save: {e}")

    # ── Leave room ────────────────────────────────────────────────────────────

    def _do_leave(self) -> None:
        self._leaving = True
        try:
            self.query_one("#send-btn", Button).disabled = True
        except Exception:
            pass
        self._leave_worker()

    @work(thread=True)
    def _leave_worker(self) -> None:
        try:
            self._conn.send_to('LeaveRoom', {})
        except Exception:
            pass
        self._conn.stop_receiver()
        self.app.call_from_thread(self.app.show_home, self._conn)

    def _cleanup_and_leave(self) -> None:
        self._leaving = True
        self._conn.stop_receiver()
        self.app.show_home(self._conn)
