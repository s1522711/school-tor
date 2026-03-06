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
        Display the home screen.
        conn — an open Connection to reuse (passed back from ChatScreen on leave).
             If None, HomeScreen will auto-connect with its default settings.
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
        """Called by HomeScreen whenever it establishes a new connection."""
        self._conn = conn

    def _on_close(self):
        if self._conn is not None:
            self._conn.close()
        self.destroy()


if __name__ == '__main__':
    App().mainloop()
