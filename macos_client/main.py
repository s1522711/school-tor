"""
Onion Chat — macOS client entry point.
Uses wxPython, which renders native Cocoa (AppKit) controls on macOS.
"""

import sys
import os

import wx

sys.path.insert(0, os.path.dirname(__file__))

from network import Connection
from connect_view import ConnectView
from chat_view import ChatView


class MainFrame(wx.Frame):
    def __init__(self):
        super().__init__(
            None,
            title="Onion Chat",
            size=(980, 660),
            style=wx.DEFAULT_FRAME_STYLE,
        )
        self.SetMinSize((720, 500))

        self._conn: Connection | None = None
        self._view: wx.Panel | None = None

        # Root sizer — holds whichever view is active
        self._root_sizer = wx.BoxSizer(wx.VERTICAL)
        self.SetSizer(self._root_sizer)

        self._setup_menu()
        self.show_connect()
        self.Centre()
        self.Show()

    # ── Menu bar ──────────────────────────────────────────────────────────────

    def _setup_menu(self):
        mb = wx.MenuBar()

        file_menu = wx.Menu()
        file_menu.Append(wx.ID_NEW,   "New Connection\tCtrl+N")
        file_menu.AppendSeparator()
        file_menu.Append(wx.ID_CLOSE, "Close Window\tCtrl+W")
        mb.Append(file_menu, "&File")

        self.SetMenuBar(mb)

        self.Bind(wx.EVT_MENU, lambda _: self.show_connect(), id=wx.ID_NEW)
        self.Bind(wx.EVT_MENU, lambda _: self.Close(),        id=wx.ID_CLOSE)
        self.Bind(wx.EVT_CLOSE, self._on_close)

    # ── Screen transitions ────────────────────────────────────────────────────

    def show_connect(self, conn: Connection | None = None):
        if conn is not None:
            self._conn = conn
        self.SetTitle("Onion Chat")
        self._swap(ConnectView(
            self,
            initial_conn=self._conn,
            on_connected=self._set_conn,
            on_enter_room=self.show_chat,
        ))

    def show_chat(self, conn: Connection, username: str, room_code: str, members: list):
        self._conn = conn
        self.SetTitle(f"#{room_code}  —  Onion Chat")
        self._swap(ChatView(
            self,
            conn=conn,
            username=username,
            room_code=room_code,
            members=members,
            on_leave=self.show_connect,
        ))

    def _swap(self, new_view: wx.Panel):
        if self._view is not None:
            self._view.Destroy()
        self._view = new_view
        self._root_sizer.Clear()
        self._root_sizer.Add(self._view, 1, wx.EXPAND)
        self.Layout()

    def _set_conn(self, conn: Connection):
        self._conn = conn

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def _on_close(self, event):
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
        event.Skip()


class App(wx.App):
    def OnInit(self):
        frame = MainFrame()
        self.SetTopWindow(frame)
        return True


if __name__ == "__main__":
    app = App(False)
    app.MainLoop()
