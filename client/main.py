"""
GUI Chat Client
===============
CustomTkinter front-end for the onion chat server.

Requirements:
    pip install customtkinter pycryptodome

Usage:
    python client/main.py
"""

import sys
import os

# Allow sibling imports when run as `python client/main.py` from the repo root
sys.path.insert(0, os.path.dirname(__file__))

import customtkinter as ctk
from network import Connection
from home_screen import HomeScreen
from chat_screen import ChatScreen

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


class App(ctk.CTk):
    def __init__(self):
        """
        Initialise the application window and display the home screen.

        How it works:
            Calls ctk.CTk.__init__() to set up the CustomTkinter window, then
            configures the title, default size (960×620), and minimum size
            (800×520 — below this the UI becomes unusable). Registers
            _on_close() as the WM_DELETE_WINDOW handler so the connection is
            closed cleanly when the user presses the OS window-close button.
            Sets _conn and _frame to None (no connection or screen yet), then
            immediately calls show_home() to display the home screen.

        Why the window size constraints:
            The home screen has a fixed-width stats panel (240 px) and a
            room panel that must show UUID room codes without truncation. Below
            800 px wide, the UI layout breaks. 520 px tall is the minimum for
            the create/join forms to be fully visible.
        """
        super().__init__()
        self.title("Onion Chat")
        self.geometry("960x620")
        self.minsize(800, 520)
        self._conn:  Connection | None = None
        self._frame: ctk.CTkFrame | None = None
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.show_home()

    # ── Screen switching ──────────────────────────────────────────────────────

    def show_home(self, conn: Connection | None = None):
        """
        Destroy the current screen and display the home screen.

        How it works:
            If conn is provided (passed back by ChatScreen when the user leaves
            a room), updates self._conn so the existing open connection is
            forwarded to the new HomeScreen. Destroys the current frame widget
            (freeing all its child widgets and cancelling any pending after()
            callbacks registered by them). Creates a new HomeScreen, passing
            initial_conn=self._conn so HomeScreen knows whether to start in
            "connected" or "disconnected" state. Packs the new frame to fill
            the window.

        Why conn flows back through here:
            The connection is persistent across screen transitions. When the
            user leaves a room, ChatScreen calls on_leave(conn), which maps to
            show_home(conn=conn). App then stores conn and passes it to the
            new HomeScreen, which immediately starts polling stats over the
            same TCP connection without reconnecting.

        Args:
            conn — an open Connection returned by ChatScreen, or None on first
                   launch / after explicit disconnect.
        """
        if conn is not None:
            self._conn = conn
        if self._frame:
            self._frame.destroy()
        self.title("Onion Chat")
        self._frame = HomeScreen(
            self,
            initial_conn=self._conn,
            on_connected=self._set_conn,
            on_enter_room=self.show_chat,
        )
        self._frame.pack(fill="both", expand=True)

    def show_chat(self, conn: Connection, username: str,
                  room_code: str, members: list):
        """
        Destroy the home screen and display the chat room screen.

        How it works:
            Stores conn (may differ from self._conn if HomeScreen just built a
            new connection). Destroys the current frame. Creates ChatScreen with
            all the room state (connection, username, room_code, full member
            list). Sets the window title to include the room_code so the user
            can identify the window at a glance. Packs the new frame.

        Why all room state is passed at construction:
            ChatScreen needs to display the existing member list immediately
            (before any UserJoined messages arrive) and to start the background
            receiver. Passing everything at construction time avoids a separate
            initialisation call and means ChatScreen is fully ready as soon as
            __init__ returns.

        Args:
            conn      — the Connection to use for this chat session.
            username  — the user's display name in this room.
            room_code — UUID of the room (shown in the title bar and copy button).
            members   — initial member list from the RoomCreated/RoomJoined response.
        """
        self._conn = conn
        if self._frame:
            self._frame.destroy()
        self.title(f"Onion Chat  —  {room_code}")
        self._frame = ChatScreen(
            self,
            connection=conn,
            username=username,
            room_code=room_code,
            members=members,
            on_leave=self.show_home,   # ChatScreen calls show_home(conn=...)
        )
        self._frame.pack(fill="both", expand=True)

    def _set_conn(self, conn: Connection):
        """
        Store a newly established connection from HomeScreen.

        How it works:
            Simply updates self._conn. Called by HomeScreen via the on_connected
            callback whenever it successfully opens a new connection (either
            direct or Tor). This keeps App's reference up to date so that
            _on_close() always closes the most recent connection.

        Why it exists:
            HomeScreen creates new connections on user request. App needs to
            track the current connection so it can close it on window close.
            Using a callback avoids HomeScreen needing a direct reference back
            to App.

        Args:
            conn — the newly established Connection.
        """
        self._conn = conn

    def _on_close(self):
        """
        Handle the OS window-close event (WM_DELETE_WINDOW).

        How it works:
            Closes the current Connection if one exists — this stops the
            background receiver thread, closes the TCP socket (or entry-node
            socket for Tor circuits), and triggers cascade teardown of the
            relay chain. Then calls self.destroy() to close the Tk window and
            terminate the main loop.

        Why close before destroy:
            If the connection is not closed, the background receiver thread
            (daemon thread) might still be blocked in select() or poll() when
            the process exits. Although daemon threads are killed automatically,
            explicitly closing the socket ensures the exit node's dest_sock
            and the relay chain tear down gracefully rather than waiting for
            TCP keepalive timeouts.
        """
        if self._conn is not None:
            self._conn.close()
        self.destroy()


if __name__ == '__main__':
    App().mainloop()
