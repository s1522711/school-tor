"""
Textual TUI client for the Onion Chat server.

Requirements:
    pip install textual pycryptodome

Usage:
    python tui_client/main.py
"""

import sys
import os

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
# tui_client/ must come first so our home_screen/chat_screen shadow client/'s versions
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)
# Append client/ so network.py is importable without shadowing our own modules
_CLIENT_DIR = os.path.join(_THIS_DIR, '..', 'client')
if _CLIENT_DIR not in sys.path:
    sys.path.append(_CLIENT_DIR)

from textual.app import App
from textual.widgets._header import Header, HeaderTitle

# Textual 8.0.2 bug: Header._on_mount's set_title() only catches NoScreen, not
# NoMatches, which is raised during screen transitions when HeaderTitle has
# already been unmounted. Patch it to silently ignore both.
def _patched_header_on_mount(self, _):
    async def set_title() -> None:
        try:
            self.query_one(HeaderTitle).update(self.format_title())
        except Exception:
            pass
    self.watch(self.app, "title",      set_title)
    self.watch(self.app, "sub_title",  set_title)
    self.watch(self.screen, "title",    set_title)
    self.watch(self.screen, "sub_title", set_title)

Header._on_mount = _patched_header_on_mount

from network import Connection
from home_screen import HomeScreen


class OnionChatApp(App):
    """Textual TUI front-end for the onion chat server."""

    TITLE = "Onion Chat"
    CSS = "Screen { background: $surface; }"

    def __init__(self):
        super().__init__()
        self._conn: Connection | None = None

    def on_mount(self) -> None:
        self.push_screen(HomeScreen())

    # ── Screen switching ───────────────────────────────────────────────────────

    def show_home(self, conn: Connection | None = None) -> None:
        if conn is not None:
            self._conn = conn
        self.switch_screen(HomeScreen(initial_conn=self._conn))

    def show_chat(self, conn: Connection, username: str,
                  room_code: str, members: list) -> None:
        from chat_screen import ChatScreen
        self._conn = conn
        self.switch_screen(ChatScreen(
            connection=conn,
            username=username,
            room_code=room_code,
            members=members,
        ))

    def set_conn(self, conn: Connection) -> None:
        self._conn = conn


if __name__ == '__main__':
    OnionChatApp().run()
