"""
Connection / home screen.

Top bar   — server settings, Tor toggle, Connect button, status dot
Body      — two columns: stats panel (left) | username + room forms (right)
Status bar — error / hint text
"""

import threading

import wx

from network import Connection, connect_direct, connect_tor

_STATS_MS = 5_000   # auto-refresh interval

# macOS system-palette accents used for status indicators
_GREEN  = wx.Colour(52,  199, 89)
_RED    = wx.Colour(255, 59,  48)
_LABEL3 = wx.Colour(108, 108, 112)   # tertiary label


def _sys_font(pt: int = 13, bold: bool = False) -> wx.Font:
    f = wx.SystemSettings.GetFont(wx.SYS_DEFAULT_GUI_FONT)
    f.SetPointSize(pt)
    if bold:
        f.SetWeight(wx.FONTWEIGHT_BOLD)
    return f


class ConnectView(wx.Panel):
    def __init__(self, parent, initial_conn, on_connected, on_enter_room):
        super().__init__(parent)

        self._conn          = initial_conn
        self._on_connected  = on_connected
        self._on_enter_room = on_enter_room

        self._stats_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER,          self._on_stats_timer, self._stats_timer)
        self.Bind(wx.EVT_WINDOW_DESTROY, self._on_destroy)

        self._build()

        if self._conn is not None:
            self._mark_connected()
        else:
            self._set_rooms_enabled(False)
            self._set_status("Fill in connection settings and click Connect.")

    # ── Build UI ──────────────────────────────────────────────────────────────

    def _build(self):
        root = wx.BoxSizer(wx.VERTICAL)
        root.Add(self._make_top_bar(),    0, wx.EXPAND)
        root.Add(wx.StaticLine(self),     0, wx.EXPAND)
        root.Add(self._make_body(),       1, wx.EXPAND)
        root.Add(wx.StaticLine(self),     0, wx.EXPAND)
        root.Add(self._make_status_bar(), 0, wx.EXPAND)
        self.SetSizer(root)

    # ── Top bar ───────────────────────────────────────────────────────────────

    def _make_top_bar(self) -> wx.Panel:
        bar = wx.Panel(self)
        self._top_bar = bar          # stored so _on_tor_toggle can call bar.Layout()
        sz  = wx.BoxSizer(wx.HORIZONTAL)

        # Title
        title = wx.StaticText(bar, label="🧅  Onion Chat")
        title.SetFont(_sys_font(17, bold=True))
        sz.Add(title, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 18)

        sz.Add(wx.StaticLine(bar, style=wx.LI_VERTICAL), 0,
               wx.EXPAND | wx.TOP | wx.BOTTOM | wx.LEFT | wx.RIGHT, 8)

        # Server address
        sz.Add(wx.StaticText(bar, label="Server"), 0,
               wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 6)
        self._tc_host = wx.TextCtrl(bar, value="127.0.0.1", size=(115, -1))
        sz.Add(self._tc_host, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 6)
        sz.Add(wx.StaticText(bar, label=":"), 0,
               wx.ALIGN_CENTER_VERTICAL | wx.LEFT | wx.RIGHT, 3)
        self._tc_port = wx.TextCtrl(bar, value="8001", size=(52, -1))
        sz.Add(self._tc_port, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 10)

        # Tor checkbox
        self._chk_tor = wx.CheckBox(bar, label="Via Tor")
        self._chk_tor.Bind(wx.EVT_CHECKBOX, self._on_tor_toggle)
        sz.Add(self._chk_tor, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)

        # Directory server fields (hidden until Tor is enabled)
        self._dir_panel = wx.Panel(bar)
        dp = wx.BoxSizer(wx.HORIZONTAL)
        dp.Add(wx.StaticText(self._dir_panel, label="Dir"), 0,
               wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
        self._tc_dir_host = wx.TextCtrl(self._dir_panel, value="127.0.0.1", size=(105, -1))
        dp.Add(self._tc_dir_host, 0, wx.ALIGN_CENTER_VERTICAL)
        dp.Add(wx.StaticText(self._dir_panel, label=":"), 0,
               wx.ALIGN_CENTER_VERTICAL | wx.LEFT | wx.RIGHT, 3)
        self._tc_dir_port = wx.TextCtrl(self._dir_panel, value="8000", size=(48, -1))
        dp.Add(self._tc_dir_port, 0, wx.ALIGN_CENTER_VERTICAL)
        self._dir_panel.SetSizer(dp)
        self._dir_panel.Hide()
        sz.Add(self._dir_panel, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)

        # Connect button
        self._btn_connect = wx.Button(bar, label="Connect")
        self._btn_connect.Bind(wx.EVT_BUTTON, lambda _: self._manual_connect())
        sz.Add(self._btn_connect, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 10)

        # Status dot
        self._dot = wx.StaticText(bar, label="●  Disconnected")
        self._dot.SetFont(_sys_font(11))
        self._dot.SetForegroundColour(_RED)
        sz.Add(self._dot, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 18)

        wrapper = wx.BoxSizer(wx.VERTICAL)
        wrapper.Add(sz, 0, wx.EXPAND | wx.TOP | wx.BOTTOM, 10)
        bar.SetSizer(wrapper)
        return bar

    # ── Body ──────────────────────────────────────────────────────────────────

    def _make_body(self) -> wx.Panel:
        body = wx.Panel(self)
        sz   = wx.BoxSizer(wx.HORIZONTAL)

        sz.Add(self._make_stats_col(body),  0, wx.EXPAND | wx.ALL, 18)
        sz.Add(wx.StaticLine(body, style=wx.LI_VERTICAL), 0, wx.EXPAND)
        sz.Add(self._make_room_col(body),   1, wx.EXPAND | wx.ALL, 22)

        body.SetSizer(sz)
        return body

    def _make_stats_col(self, parent: wx.Panel) -> wx.Sizer:
        box  = wx.StaticBox(parent, label="Server Statistics")
        sbox = wx.StaticBoxSizer(box, wx.VERTICAL)

        # On macOS, wx.StaticBox renders as NSBox (a real container view).
        # All child widgets MUST be parented to `box`, not `parent` — otherwise
        # the NSBox is drawn over them and intercepts their mouse/keyboard events.
        grid = wx.FlexGridSizer(rows=4, cols=2, vgap=9, hgap=24)
        grid.AddGrowableCol(1, 1)

        self._stat_labels: dict[str, wx.StaticText] = {}
        for display, key in [
            ("Messages",   "total_messages"),
            ("Files Sent", "total_files"),
            ("Users",      "total_users"),
            ("Rooms",      "total_rooms"),
        ]:
            name_lbl = wx.StaticText(box, label=display)    # ← box, not parent
            name_lbl.SetForegroundColour(_LABEL3)
            grid.Add(name_lbl, 0, wx.ALIGN_CENTER_VERTICAL)

            val_lbl = wx.StaticText(box, label="—")          # ← box, not parent
            val_lbl.SetFont(_sys_font(13, bold=True))
            grid.Add(val_lbl, 0, wx.ALIGN_CENTER_VERTICAL | wx.ALIGN_RIGHT)
            self._stat_labels[key] = val_lbl

        sbox.Add(grid, 0, wx.EXPAND | wx.ALL, 10)
        sbox.Add(wx.StaticLine(box), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 6)  # ← box

        self._btn_refresh = wx.Button(box, label="↻  Refresh Now")          # ← box
        self._btn_refresh.Bind(wx.EVT_BUTTON, lambda _: self._fetch_stats(manual=True))
        sbox.Add(self._btn_refresh, 0, wx.ALL, 10)

        return sbox

    def _make_room_col(self, parent: wx.Panel) -> wx.Sizer:
        col = wx.BoxSizer(wx.VERTICAL)

        # Username row — plain widgets, no StaticBox nearby
        row = wx.BoxSizer(wx.HORIZONTAL)
        lbl = wx.StaticText(parent, label="Username")
        lbl.SetForegroundColour(_LABEL3)
        row.Add(lbl, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 12)
        self._tc_username = wx.TextCtrl(parent, size=(220, -1))
        row.Add(self._tc_username, 0, wx.ALIGN_CENTER_VERTICAL)
        col.Add(row, 0, wx.BOTTOM, 16)
        col.Add(wx.StaticLine(parent), 0, wx.EXPAND | wx.BOTTOM, 18)

        # Create + Join cards.
        # We avoid wx.StaticBox here entirely: on macOS the NSBox container
        # intercepts events for any sibling widgets (like the username field
        # above) that share the same parent panel.  Plain sub-panels with a
        # bold heading + separator achieve the same visual grouping safely.
        cards = wx.BoxSizer(wx.HORIZONTAL)

        # ── Create card ───────────────────────────────────────────────────────
        create_panel = wx.Panel(parent)
        cp_sz = wx.BoxSizer(wx.VERTICAL)

        cp_title = wx.StaticText(create_panel, label="Create Room")
        cp_title.SetFont(_sys_font(13, bold=True))
        cp_sz.Add(cp_title, 0, wx.BOTTOM, 6)
        cp_sz.Add(wx.StaticLine(create_panel), 0, wx.EXPAND | wx.BOTTOM, 10)

        self._btn_create = wx.Button(create_panel, label="Create New Room")
        self._btn_create.Bind(wx.EVT_BUTTON, lambda _: self._do_create())
        cp_sz.Add(self._btn_create, 0)

        create_panel.SetSizer(cp_sz)
        cards.Add(create_panel, 1, wx.EXPAND | wx.RIGHT, 20)

        # ── Join card ─────────────────────────────────────────────────────────
        join_panel = wx.Panel(parent)
        jp_sz = wx.BoxSizer(wx.VERTICAL)

        jp_title = wx.StaticText(join_panel, label="Join Room")
        jp_title.SetFont(_sys_font(13, bold=True))
        jp_sz.Add(jp_title, 0, wx.BOTTOM, 6)
        jp_sz.Add(wx.StaticLine(join_panel), 0, wx.EXPAND | wx.BOTTOM, 10)

        code_row = wx.BoxSizer(wx.HORIZONTAL)
        code_lbl = wx.StaticText(join_panel, label="Code")
        code_lbl.SetForegroundColour(_LABEL3)
        code_row.Add(code_lbl, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self._tc_code = wx.TextCtrl(join_panel, size=(150, -1))
        mono = wx.Font(12, wx.FONTFAMILY_TELETYPE,
                       wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL)
        self._tc_code.SetFont(mono)
        code_row.Add(self._tc_code, 1, wx.EXPAND)
        jp_sz.Add(code_row, 0, wx.EXPAND | wx.BOTTOM, 10)

        self._btn_join = wx.Button(join_panel, label="Join Room")
        self._btn_join.Bind(wx.EVT_BUTTON, lambda _: self._do_join())
        jp_sz.Add(self._btn_join, 0)

        join_panel.SetSizer(jp_sz)
        cards.Add(join_panel, 1, wx.EXPAND)

        col.Add(cards, 0, wx.EXPAND)
        return col

    def _make_status_bar(self) -> wx.Panel:
        bar = wx.Panel(self)
        sz  = wx.BoxSizer(wx.HORIZONTAL)
        self._status_lbl = wx.StaticText(bar, label="")
        self._status_lbl.SetFont(_sys_font(11))
        self._status_lbl.SetForegroundColour(_LABEL3)
        sz.Add(self._status_lbl, 1, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 6)
        bar.SetSizer(sz)
        return bar

    # ── Tor toggle ────────────────────────────────────────────────────────────

    def _on_tor_toggle(self, _):
        if self._chk_tor.IsChecked():
            self._dir_panel.Show()
        else:
            self._dir_panel.Hide()
        # Layout the toolbar panel (direct owner of _dir_panel's sizer row)
        self._top_bar.Layout()
        self._top_bar.Refresh()

    # ── Connect ───────────────────────────────────────────────────────────────

    def _manual_connect(self):
        self._btn_connect.Disable()
        self._set_status("Connecting…")

        host     = self._tc_host.GetValue().strip()
        port_str = self._tc_port.GetValue().strip()
        use_tor  = self._chk_tor.IsChecked()
        dh       = self._tc_dir_host.GetValue().strip()
        dp_str   = self._tc_dir_port.GetValue().strip()

        old_conn = self._conn
        self._conn = None

        def _work():
            if old_conn is not None:
                try:
                    old_conn.close()
                except Exception:
                    pass
            try:
                port = int(port_str)
                if use_tor:
                    conn = connect_tor(dh, int(dp_str), host, port)
                else:
                    conn = connect_direct(host, port)
            except Exception as e:
                wx.CallAfter(self._on_fail, str(e))
                return
            wx.CallAfter(self._on_success, conn)

        threading.Thread(target=_work, daemon=True).start()

    def _on_success(self, conn: Connection):
        self._conn = conn
        self._on_connected(conn)
        self._mark_connected()
        self._set_rooms_enabled(True)
        self._start_stats(delay=0)

    def _on_fail(self, err: str):
        self._btn_connect.Enable()
        self._dot.SetLabel("●  Disconnected")
        self._dot.SetForegroundColour(_RED)
        self._set_status(f"Connection failed: {err}", _RED)
        self.Layout()

    def _mark_connected(self):
        self._btn_connect.Enable()
        mode = "Tor" if (self._conn and self._conn.is_tor) else "Direct"
        self._dot.SetLabel(f"●  Connected ({mode})")
        self._dot.SetForegroundColour(_GREEN)
        self._set_status("")
        self.Layout()

    # ── Stats ─────────────────────────────────────────────────────────────────

    def _on_stats_timer(self, _):
        self._fetch_stats()

    def _fetch_stats(self, manual: bool = False):
        if self._conn is None:
            return
        if manual:
            self._btn_refresh.Disable()
        conn = self._conn

        def _work():
            try:
                conn.send_to("GetStats", {})
                resp  = conn.recv_one()
                stats = resp.get("data", {}) if resp else {}
            except Exception:
                stats = {}
            wx.CallAfter(self._apply_stats, stats, manual)

        threading.Thread(target=_work, daemon=True).start()

    def _apply_stats(self, stats: dict, manual: bool = False):
        if not self:
            return
        for key, lbl in self._stat_labels.items():
            v = stats.get(key)
            lbl.SetLabel("—" if v is None else str(v))
        if manual:
            self._btn_refresh.Enable()
        self._start_stats(delay=_STATS_MS)

    def _start_stats(self, delay: int):
        self._stats_timer.Stop()
        self._stats_timer.StartOnce(delay)

    # ── Create / Join ─────────────────────────────────────────────────────────

    def _do_create(self):
        username = self._tc_username.GetValue().strip()
        if not username:
            self._set_status("Please enter a username.", _RED)
            return
        if self._conn is None:
            self._set_status("Not connected — click Connect first.", _RED)
            return
        self._set_rooms_enabled(False)
        self._stats_timer.Stop()
        conn = self._conn

        def _work():
            try:
                conn.send_to("CreateRoom", {"my_username": username})
                resp = conn.recv_one()
            except Exception as e:
                wx.CallAfter(self._room_error, str(e))
                return
            if resp and resp.get("type") == "RoomCreated":
                d = resp["data"]
                wx.CallAfter(self._on_enter_room,
                             conn, username, d["room_code"], d.get("users", []))
            else:
                wx.CallAfter(self._room_error, str(resp))

        threading.Thread(target=_work, daemon=True).start()

    def _do_join(self):
        username  = self._tc_username.GetValue().strip()
        room_code = self._tc_code.GetValue().strip()
        if not username:
            self._set_status("Please enter a username.", _RED)
            return
        if not room_code:
            self._set_status("Please enter a room code.", _RED)
            return
        if self._conn is None:
            self._set_status("Not connected — click Connect first.", _RED)
            return
        self._set_rooms_enabled(False)
        self._stats_timer.Stop()
        conn = self._conn

        def _work():
            try:
                conn.send_to("JoinRoom", {"my_username": username, "room_code": room_code})
                resp = conn.recv_one()
            except Exception as e:
                wx.CallAfter(self._room_error, str(e))
                return
            if resp and resp.get("type") == "RoomJoined":
                d = resp["data"]
                wx.CallAfter(self._on_enter_room,
                             conn, username, d["room_code"], d.get("users", []))
            elif resp and resp.get("type") == "Error":
                wx.CallAfter(self._room_error, resp["data"].get("error_message", "Error"))
            else:
                wx.CallAfter(self._room_error, str(resp))

        threading.Thread(target=_work, daemon=True).start()

    def _room_error(self, msg: str):
        self._set_status(msg, _RED)
        self._set_rooms_enabled(True)
        self._start_stats(delay=_STATS_MS)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _set_rooms_enabled(self, on: bool):
        for btn in (self._btn_create, self._btn_join):
            btn.Enable(on)

    def _set_status(self, text: str, colour: wx.Colour = _LABEL3):
        self._status_lbl.SetLabel(text)
        self._status_lbl.SetForegroundColour(colour)
        self._status_lbl.GetParent().Layout()

    def _on_destroy(self, event):
        self._stats_timer.Stop()
        event.Skip()
