"""
Chat room screen.

Toolbar  — room code, Copy, Leave
Splitter — left: user list sidebar | right: message area + input bar
"""

import base64
import os
import threading

import wx

from network import Connection

# ── Palette (macOS system tints) ─────────────────────────────────────────────

_BLUE   = wx.Colour(0,   122, 255)
_GREEN  = wx.Colour(52,  199, 89)
_ORANGE = wx.Colour(255, 149, 0)
_PURPLE = wx.Colour(175, 82,  222)
_PINK   = wx.Colour(255, 45,  85)
_TEAL   = wx.Colour(50,  173, 230)
_GREY   = wx.Colour(142, 142, 147)
_RED    = wx.Colour(255, 59,  48)

_PALETTE = [_BLUE, _GREEN, _ORANGE, _PURPLE, _PINK, _TEAL]


def _user_colour(name: str) -> wx.Colour:
    return _PALETTE[hash(name) % len(_PALETTE)]


def _sys_font(pt: int = 13, bold: bool = False, italic: bool = False) -> wx.Font:
    f = wx.SystemSettings.GetFont(wx.SYS_DEFAULT_GUI_FONT)
    f.SetPointSize(pt)
    if bold:
        f.SetWeight(wx.FONTWEIGHT_BOLD)
    if italic:
        f.SetStyle(wx.FONTSTYLE_ITALIC)
    return f


# ── Message area ──────────────────────────────────────────────────────────────

class MessageArea(wx.ScrolledWindow):
    """
    Scrollable message log built from individual wx.Panel rows.

    Using a ScrolledWindow (rather than RichTextCtrl) lets us place real
    wx.Button widgets — e.g. a Save button — directly inside a file-message
    row, which a text control does not support.
    """

    def __init__(self, parent: wx.Window):
        super().__init__(parent, style=wx.VSCROLL | wx.BORDER_NONE)
        self.SetScrollRate(0, 20)

        self._sizer = wx.BoxSizer(wx.VERTICAL)
        # Leading spacer keeps messages pinned to the top while the log is short
        self._sizer.AddSpacer(8)
        self.SetSizer(self._sizer)

        self._me: str = ""

    def set_me(self, username: str):
        self._me = username

    # ── Row factory ───────────────────────────────────────────────────────────

    def _row(self) -> wx.Panel:
        # No explicit background — inherits the system window colour,
        # which is correct for both light and dark mode.
        return wx.Panel(self)

    def _commit(self, row: wx.Panel):
        self._sizer.Add(row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        self.FitInside()
        self.Scroll(-1, self.GetScrollRange(wx.VERTICAL))

    # ── Public append API ─────────────────────────────────────────────────────

    def append_chat(self, username: str, message: str):
        is_self = username == self._me
        col     = _GREY if is_self else _user_colour(username)
        display = "You" if is_self else username

        row = self._row()
        sz  = wx.BoxSizer(wx.VERTICAL)

        name = wx.StaticText(row, label=display)
        name.SetFont(_sys_font(12, bold=True))
        name.SetForegroundColour(col)
        sz.Add(name, 0)

        body = wx.StaticText(row, label=message)
        body.SetFont(_sys_font(13))
        body.SetForegroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOWTEXT))
        body.Wrap(max(200, self.GetClientSize().width - 40))
        sz.Add(body, 0, wx.LEFT, 2)

        row.SetSizer(sz)
        self._commit(row)

    def append_system(self, text: str):
        row = self._row()
        sz  = wx.BoxSizer(wx.HORIZONTAL)

        lbl = wx.StaticText(row, label=f"— {text} —")
        lbl.SetFont(_sys_font(11, italic=True))
        lbl.SetForegroundColour(_GREY)
        sz.Add(lbl, 1, wx.ALIGN_CENTER | wx.TOP | wx.BOTTOM, 2)

        row.SetSizer(sz)
        self._commit(row)

    def append_file(self, username: str, filename: str, size_kb: float,
                    filedata: str, save_cb):
        """
        Append a file-transfer notice with an inline Save button.
        save_cb(filename, filedata) is called when the user clicks Save.
        """
        is_self = username == self._me
        col     = _GREY if is_self else _user_colour(username)
        display = "You" if is_self else username

        row = self._row()
        sz  = wx.BoxSizer(wx.VERTICAL)

        name = wx.StaticText(row, label=display)
        name.SetFont(_sys_font(12, bold=True))
        name.SetForegroundColour(col)
        sz.Add(name, 0)

        file_row = wx.BoxSizer(wx.HORIZONTAL)

        icon = wx.StaticText(row, label=f"📎  {filename}  (~{size_kb:.1f} KB)")
        icon.SetFont(_sys_font(12))
        icon.SetForegroundColour(_TEAL)
        file_row.Add(icon, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 10)

        save_btn = wx.Button(row, label="Save…")
        save_btn.SetFont(_sys_font(11))
        save_btn.Bind(
            wx.EVT_BUTTON,
            lambda _, fn=filename, fd=filedata: save_cb(fn, fd),
        )
        file_row.Add(save_btn, 0, wx.ALIGN_CENTER_VERTICAL)

        sz.Add(file_row, 0, wx.LEFT | wx.TOP, 2)
        row.SetSizer(sz)
        self._commit(row)

    def append_error(self, text: str):
        row = self._row()
        sz  = wx.BoxSizer(wx.HORIZONTAL)

        lbl = wx.StaticText(row, label=f"⚠  {text}")
        lbl.SetFont(_sys_font(11))
        lbl.SetForegroundColour(_RED)
        sz.Add(lbl, 0, wx.TOP | wx.BOTTOM, 2)

        row.SetSizer(sz)
        self._commit(row)


# ── ChatView ─────────────────────────────────────────────────────────────────

class ChatView(wx.Panel):
    def __init__(
        self,
        parent,
        conn: Connection,
        username: str,
        room_code: str,
        members: list,
        on_leave,          # (conn) -> None
    ):
        super().__init__(parent)

        self._conn      = conn
        self._me        = username
        self._room_code = room_code
        self._on_leave  = on_leave
        self._leaving   = False

        self._build()

        for m in members:
            self._add_user(m)

        self._msg_box.append_system(f"You joined room {room_code}")
        self._conn.start_receiver(self._on_message, self._on_disconnect)

    # ── Build UI ──────────────────────────────────────────────────────────────

    def _build(self):
        root = wx.BoxSizer(wx.VERTICAL)
        root.Add(self._make_toolbar(),   0, wx.EXPAND)
        root.Add(wx.StaticLine(self),    0, wx.EXPAND)
        root.Add(self._make_workspace(), 1, wx.EXPAND)
        root.Add(wx.StaticLine(self),    0, wx.EXPAND)
        root.Add(self._make_input_bar(), 0, wx.EXPAND)
        self.SetSizer(root)

    # ── Toolbar ───────────────────────────────────────────────────────────────

    def _make_toolbar(self) -> wx.Panel:
        bar = wx.Panel(self)
        sz  = wx.BoxSizer(wx.HORIZONTAL)

        room_lbl = wx.StaticText(bar, label="Room:")
        room_lbl.SetForegroundColour(wx.Colour(142, 142, 147))
        sz.Add(room_lbl, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 16)

        code_lbl = wx.StaticText(bar, label=self._room_code)
        mono = wx.Font(13, wx.FONTFAMILY_TELETYPE,
                       wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD)
        code_lbl.SetFont(mono)
        sz.Add(code_lbl, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 6)

        btn_copy = wx.Button(bar, label="Copy Code")
        btn_copy.Bind(wx.EVT_BUTTON, lambda _: self._copy_code())
        sz.Add(btn_copy, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 10)

        sz.AddStretchSpacer()

        self._btn_leave = wx.Button(bar, label="Leave Room")
        self._btn_leave.Bind(wx.EVT_BUTTON, lambda _: self._do_leave())
        sz.Add(self._btn_leave, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 16)

        wrapper = wx.BoxSizer(wx.VERTICAL)
        wrapper.Add(sz, 0, wx.EXPAND | wx.TOP | wx.BOTTOM, 8)
        bar.SetSizer(wrapper)
        return bar

    # ── Splitter: sidebar | messages ─────────────────────────────────────────

    def _make_workspace(self) -> wx.SplitterWindow:
        splitter = wx.SplitterWindow(
            self,
            style=wx.SP_LIVE_UPDATE | wx.SP_NOBORDER,
        )
        splitter.SetMinimumPaneSize(140)

        splitter.SplitVertically(
            self._make_sidebar(splitter),
            self._make_msg_panel(splitter),
            190,
        )
        return splitter

    def _make_sidebar(self, parent: wx.Window) -> wx.Panel:
        panel = wx.Panel(parent)
        panel.SetBackgroundColour(
            wx.SystemSettings.GetColour(wx.SYS_COLOUR_BTNFACE)
        )
        sz = wx.BoxSizer(wx.VERTICAL)

        hdr = wx.StaticText(panel, label="PEOPLE")
        hdr.SetFont(_sys_font(10, bold=True))
        hdr.SetForegroundColour(wx.Colour(174, 174, 178))
        sz.Add(hdr, 0, wx.LEFT | wx.TOP, 14)
        sz.Add(wx.StaticLine(panel), 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 10)

        # User list — native NSTableView rendered by wx.ListBox
        self._user_list = wx.ListBox(panel, style=wx.LB_SINGLE | wx.BORDER_NONE)
        self._user_list.SetBackgroundColour(
            wx.SystemSettings.GetColour(wx.SYS_COLOUR_BTNFACE)
        )
        sz.Add(self._user_list, 1, wx.EXPAND | wx.ALL, 6)

        sz.Add(wx.StaticLine(panel), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)

        # Connection mode badge
        mode   = "Tor" if self._conn.is_tor else "Direct"
        colour = _ORANGE if self._conn.is_tor else _GREEN
        badge  = wx.StaticText(panel, label=f"●  Via {mode}")
        badge.SetFont(_sys_font(11))
        badge.SetForegroundColour(colour)
        sz.Add(badge, 0, wx.ALL, 10)

        panel.SetSizer(sz)
        return panel

    def _make_msg_panel(self, parent: wx.Window) -> wx.Panel:
        panel = wx.Panel(parent)
        sz    = wx.BoxSizer(wx.VERTICAL)

        self._msg_box = MessageArea(panel)
        self._msg_box.set_me(self._me)
        sz.Add(self._msg_box, 1, wx.EXPAND)

        panel.SetSizer(sz)
        return panel

    # ── Input bar ─────────────────────────────────────────────────────────────

    def _make_input_bar(self) -> wx.Panel:
        bar = wx.Panel(self)
        sz  = wx.BoxSizer(wx.HORIZONTAL)

        btn_attach = wx.Button(bar, label="📎", size=(36, -1))
        btn_attach.Bind(wx.EVT_BUTTON, lambda _: self._pick_file())
        sz.Add(btn_attach, 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 8)

        self._entry = wx.TextCtrl(bar, style=wx.TE_PROCESS_ENTER)
        self._entry.SetHint("Message…")
        self._entry.Bind(wx.EVT_TEXT_ENTER, lambda _: self._send_message())
        sz.Add(self._entry, 1, wx.ALIGN_CENTER_VERTICAL | wx.TOP | wx.BOTTOM, 8)

        btn_send = wx.Button(bar, label="Send  ↑")
        btn_send.Bind(wx.EVT_BUTTON, lambda _: self._send_message())
        sz.Add(btn_send, 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 8)

        bar.SetSizer(sz)
        return bar

    # ── User list management ──────────────────────────────────────────────────

    def _add_user(self, username: str):
        if self._user_list.FindString(username) == wx.NOT_FOUND:
            display = f"{username} (you)" if username == self._me else username
            self._user_list.Append(display)

    def _remove_user(self, username: str):
        # Match both "username" and "username (you)"
        for candidate in (username, f"{username} (you)"):
            idx = self._user_list.FindString(candidate)
            if idx != wx.NOT_FOUND:
                self._user_list.Delete(idx)
                return

    # ── Incoming messages ─────────────────────────────────────────────────────

    def _on_message(self, msg_type: str, data: dict):
        wx.CallAfter(self._handle, msg_type, data)

    def _handle(self, msg_type: str, data: dict):
        if msg_type == "IncomingMessage":
            self._msg_box.append_chat(
                data.get("from_username", "?"),
                data.get("message", ""),
            )

        elif msg_type == "IncomingFile":
            raw = data.get("filedata", "")
            try:
                size_kb = len(base64.b64decode(raw)) / 1024
            except Exception:
                size_kb = 0.0
            username = data.get("from_username", "?")
            filename = data.get("filename", "file")
            self._msg_box.append_file(username, filename, size_kb, raw, self._save_file)

        elif msg_type == "UserJoined":
            u = data.get("username", "?")
            self._add_user(u)
            self._msg_box.append_system(f"{u} joined")

        elif msg_type == "UserLeft":
            u = data.get("username", "?")
            self._remove_user(u)
            self._msg_box.append_system(f"{u} left")

        elif msg_type == "RoomLeft":
            self._msg_box.append_system("You left the room.")
            self._cleanup_and_leave()

        elif msg_type == "Stats":
            parts = [
                f"Messages: {data.get('total_messages', '?')}",
                f"Files: {data.get('total_files', '?')}",
                f"Users: {data.get('total_users', '?')}",
                f"Rooms: {data.get('total_rooms', '?')}",
            ]
            self._msg_box.append_system("  ·  ".join(parts))

        elif msg_type == "Error":
            self._msg_box.append_error(data.get("message", "Unknown error"))

    def _on_disconnect(self):
        if not self._leaving:
            wx.CallAfter(self._msg_box.append_error, "Disconnected from server.")

    # ── Send message ──────────────────────────────────────────────────────────

    def _send_message(self):
        text = self._entry.GetValue().strip()
        if not text:
            return
        self._entry.Clear()

        # Show locally immediately
        self._msg_box.append_chat(self._me, text)

        conn = self._conn

        def _work():
            try:
                conn.send_to("SendMessage", {"message": text})
            except Exception as e:
                wx.CallAfter(self._msg_box.append_error, str(e))

        threading.Thread(target=_work, daemon=True).start()

    # ── File transfer ─────────────────────────────────────────────────────────

    def _pick_file(self):
        dlg = wx.FileDialog(self, "Attach a file", style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
        if dlg.ShowModal() != wx.ID_OK:
            dlg.Destroy()
            return
        path = dlg.GetPath()
        dlg.Destroy()

        try:
            with open(path, "rb") as fh:
                raw = fh.read()
        except Exception as e:
            self._msg_box.append_error(f"Could not read file: {e}")
            return

        filename = path.split("/")[-1].split("\\")[-1]
        filedata = base64.b64encode(raw).decode()
        size_kb  = len(raw) / 1024

        self._msg_box.append_file(self._me, filename, size_kb, filedata, self._save_file)

        conn = self._conn

        def _work():
            try:
                conn.send_to("SendFile", {"filename": filename, "filedata": filedata})
            except Exception as e:
                wx.CallAfter(self._msg_box.append_error, str(e))

        threading.Thread(target=_work, daemon=True).start()

    def _save_file(self, filename: str, filedata: str):
        """
        Let the user pick a folder, then write the file there.

        wx.FileDialog with wx.FD_SAVE crashes in wxPython 4.2.x on macOS
        (wxArrayString bounds assertion in the native-sheet filter setup).
        wx.DirDialog is unaffected and gives equivalent UX for this use case.
        """
        safe_name = os.path.basename(filename) or "file"
        dlg = wx.DirDialog(
            wx.GetTopLevelParent(self),
            f'Choose a folder to save "{safe_name}"',
            style=wx.DD_DEFAULT_STYLE | wx.DD_DIR_MUST_EXIST,
        )
        if dlg.ShowModal() != wx.ID_OK:
            dlg.Destroy()
            return
        dest = os.path.join(dlg.GetPath(), safe_name)
        dlg.Destroy()
        try:
            raw = base64.b64decode(filedata + "==")   # tolerate missing padding
            with open(dest, "wb") as fh:
                fh.write(raw)
            self._msg_box.append_system(f'Saved "{safe_name}"')
        except Exception as e:
            wx.MessageBox(str(e), "Save failed", wx.OK | wx.ICON_ERROR)

    # ── Room code ─────────────────────────────────────────────────────────────

    def _copy_code(self):
        if wx.TheClipboard.Open():
            wx.TheClipboard.SetData(wx.TextDataObject(self._room_code))
            wx.TheClipboard.Close()
        self._msg_box.append_system(f"Room code {self._room_code} copied to clipboard.")

    # ── Leave ─────────────────────────────────────────────────────────────────

    def _do_leave(self):
        self._leaving = True
        self._btn_leave.Disable()
        conn = self._conn

        def _work():
            try:
                conn.send_to("LeaveRoom", {})
            except Exception:
                pass
            conn.stop_receiver()
            wx.CallAfter(self._on_leave, conn)

        threading.Thread(target=_work, daemon=True).start()

    def _cleanup_and_leave(self):
        self._leaving = True
        conn = self._conn

        def _work():
            conn.stop_receiver()
            wx.CallAfter(self._on_leave, conn)

        threading.Thread(target=_work, daemon=True).start()
