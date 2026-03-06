"""Home screen — connection settings, server stats, create/join room."""

import threading
import customtkinter as ctk

from network import Connection, connect_direct, connect_tor

_ONION_PURPLE = "#7c3aed"
_GREEN        = "#27ae60"
_RED          = "#c0392b"
_STATS_INTERVAL_MS = 5_000   # auto-refresh every 5 s while connected


class HomeScreen(ctk.CTkFrame):
    def __init__(self, master, initial_conn: Connection | None, on_connected, on_enter_room):
        super().__init__(master, fg_color="transparent")
        self._conn         = initial_conn
        self._on_connected = on_connected   # on_connected(conn) — notifies App of new conn
        self._on_enter_room = on_enter_room  # on_enter_room(conn, username, room_code, members)
        self._stats_after_id = None

        self._build_ui()

        if self._conn is not None:
            # Returning from a chat room — reuse the open connection
            self._mark_connected()
            self._schedule_stats_refresh(delay=0)
        else:
            self._set_status("Fill in connection settings and click Connect.")
            self._set_room_buttons(False)

    # ── Layout ────────────────────────────────────────────────────────────────

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=0, minsize=240)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=0)
        self.grid_rowconfigure(1, weight=1)
        self.grid_rowconfigure(2, weight=0)

        self._build_topbar()
        self._build_stats_panel()
        self._build_room_panel()
        self._build_status_bar()

    def _build_topbar(self):
        bar = ctk.CTkFrame(self, corner_radius=0, fg_color=("gray88", "gray17"))
        bar.grid(row=0, column=0, columnspan=2, sticky="ew")
        bar.grid_columnconfigure(99, weight=1)

        ctk.CTkLabel(
            bar, text="Onion Chat",
            font=ctk.CTkFont(size=17, weight="bold"),
        ).grid(row=0, column=0, padx=(16, 24), pady=12)

        ctk.CTkLabel(bar, text="Server:").grid(row=0, column=1, padx=(0, 4), pady=12)
        self._host_var = ctk.StringVar(value="127.0.0.1")
        ctk.CTkEntry(bar, textvariable=self._host_var, width=115).grid(
            row=0, column=2, padx=4, pady=12)

        ctk.CTkLabel(bar, text="Port:").grid(row=0, column=3, padx=(0, 4), pady=12)
        self._port_var = ctk.StringVar(value="8001")
        ctk.CTkEntry(bar, textvariable=self._port_var, width=62).grid(
            row=0, column=4, padx=4, pady=12)

        self._tor_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            bar, text="Route via Tor",
            variable=self._tor_var,
            command=self._on_tor_toggle,
            fg_color=_ONION_PURPLE,
            hover_color="#5b21b6",
        ).grid(row=0, column=5, padx=(16, 4), pady=12)

        # Tor-only dir fields (hidden until checkbox ticked)
        self._dir_host_label = ctk.CTkLabel(bar, text="Dir Host:")
        self._dir_host_var   = ctk.StringVar(value="127.0.0.1")
        self._dir_host_entry = ctk.CTkEntry(bar, textvariable=self._dir_host_var, width=115)
        self._dir_port_label = ctk.CTkLabel(bar, text="Dir Port:")
        self._dir_port_var   = ctk.StringVar(value="8000")
        self._dir_port_entry = ctk.CTkEntry(bar, textvariable=self._dir_port_var, width=62)
        for col, w in enumerate(
            [self._dir_host_label, self._dir_host_entry,
             self._dir_port_label, self._dir_port_entry],
            start=6,
        ):
            w.grid(row=0, column=col, padx=4, pady=12)
            w.grid_remove()

        self._connect_btn = ctk.CTkButton(
            bar, text="Connect", width=90,
            command=self._manual_connect,
        )
        self._connect_btn.grid(row=0, column=10, padx=(8, 4), pady=12)

        # Connection status indicator
        self._conn_status_var = ctk.StringVar(value="")
        self._conn_status_label = ctk.CTkLabel(
            bar, textvariable=self._conn_status_var,
            font=ctk.CTkFont(size=11),
        )
        self._conn_status_label.grid(row=0, column=11, padx=(4, 16), pady=12)

    def _on_tor_toggle(self):
        for w in (self._dir_host_label, self._dir_host_entry,
                  self._dir_port_label, self._dir_port_entry):
            if self._tor_var.get():
                w.grid()
            else:
                w.grid_remove()
        # Settings changed — prompt reconnect
        if self._conn is not None:
            self._conn_status_var.set("Settings changed — reconnect to apply")
            self._conn_status_label.configure(text_color=("gray50", "gray60"))

    def _build_stats_panel(self):
        panel = ctk.CTkFrame(self, corner_radius=10)
        panel.grid(row=1, column=0, sticky="nsew", padx=(12, 6), pady=12)

        ctk.CTkLabel(
            panel, text="Server Statistics",
            font=ctk.CTkFont(size=14, weight="bold"),
        ).pack(anchor="w", padx=16, pady=(16, 10))

        ctk.CTkFrame(panel, height=1, fg_color=("gray75", "gray35")).pack(
            fill="x", padx=16, pady=(0, 10))

        self._stat_vars = {}
        for key, label in (
            ("total_messages", "Messages"),
            ("total_files",    "Files Sent"),
            ("total_users",    "Total Users"),
            ("total_rooms",    "Total Rooms"),
        ):
            row = ctk.CTkFrame(panel, fg_color="transparent")
            row.pack(fill="x", padx=16, pady=5)
            ctk.CTkLabel(row, text=label, anchor="w").pack(side="left")
            var = ctk.StringVar(value="—")
            ctk.CTkLabel(
                row, textvariable=var,
                font=ctk.CTkFont(weight="bold"),
                anchor="e",
            ).pack(side="right")
            self._stat_vars[key] = var

        ctk.CTkFrame(panel, height=1, fg_color=("gray75", "gray35")).pack(
            fill="x", padx=16, pady=(10, 10))

        self._refresh_btn = ctk.CTkButton(
            panel, text="Refresh Now",
            command=lambda: self._fetch_stats(manual=True),
        )
        self._refresh_btn.pack(fill="x", padx=16, pady=(0, 16))

    def _build_room_panel(self):
        outer = ctk.CTkFrame(self, fg_color="transparent")
        outer.grid(row=1, column=1, sticky="nsew", padx=(6, 12), pady=12)
        outer.grid_rowconfigure(0, weight=1)
        outer.grid_rowconfigure(1, weight=1)
        outer.grid_columnconfigure(0, weight=1)

        # ── Create Room ──────────────────────────────────────────────────────
        create = ctk.CTkFrame(outer, corner_radius=10)
        create.grid(row=0, column=0, sticky="nsew", pady=(0, 6))

        ctk.CTkLabel(
            create, text="Create Room",
            font=ctk.CTkFont(size=14, weight="bold"),
        ).pack(anchor="w", padx=16, pady=(16, 10))

        ctk.CTkLabel(create, text="Username", anchor="w").pack(anchor="w", padx=16)
        self._create_username = ctk.CTkEntry(
            create, placeholder_text="Your display name")
        self._create_username.pack(fill="x", padx=16, pady=(4, 12))

        self._create_btn = ctk.CTkButton(
            create, text="Create Room", command=self._do_create)
        self._create_btn.pack(fill="x", padx=16, pady=(0, 16))

        # ── Join Room ────────────────────────────────────────────────────────
        join = ctk.CTkFrame(outer, corner_radius=10)
        join.grid(row=1, column=0, sticky="nsew", pady=(6, 0))

        ctk.CTkLabel(
            join, text="Join Room",
            font=ctk.CTkFont(size=14, weight="bold"),
        ).pack(anchor="w", padx=16, pady=(16, 10))

        ctk.CTkLabel(join, text="Username", anchor="w").pack(anchor="w", padx=16)
        self._join_username = ctk.CTkEntry(
            join, placeholder_text="Your display name")
        self._join_username.pack(fill="x", padx=16, pady=(4, 8))

        ctk.CTkLabel(join, text="Room Code", anchor="w").pack(anchor="w", padx=16)
        self._join_code = ctk.CTkEntry(
            join, placeholder_text="Paste room code here")
        self._join_code.pack(fill="x", padx=16, pady=(4, 12))

        self._join_btn = ctk.CTkButton(
            join, text="Join Room", command=self._do_join)
        self._join_btn.pack(fill="x", padx=16, pady=(0, 16))

    def _build_status_bar(self):
        bar = ctk.CTkFrame(self, corner_radius=0, fg_color=("gray83", "gray20"))
        bar.grid(row=2, column=0, columnspan=2, sticky="ew")
        self._status_label = ctk.CTkLabel(
            bar, text="", anchor="w", font=ctk.CTkFont(size=11))
        self._status_label.pack(side="left", padx=12, pady=5)

    def _set_status(self, text: str, error: bool = False):
        color = _RED if error else ("gray40", "gray70")
        self._status_label.configure(text=text, text_color=color)

    # ── Connection management ─────────────────────────────────────────────────

    def _get_params(self):
        host = self._host_var.get().strip()
        try:
            port = int(self._port_var.get().strip())
        except ValueError:
            raise ValueError("Invalid server port")
        use_tor = self._tor_var.get()
        dir_host = self._dir_host_var.get().strip()
        try:
            dir_port = int(self._dir_port_var.get().strip())
        except ValueError:
            if use_tor:
                raise ValueError("Invalid directory port")
            dir_port = 8000
        return host, port, use_tor, dir_host, dir_port

    def _manual_connect(self):
        """User clicked Connect — close old conn and open a fresh one."""
        if self._conn is not None:
            old = self._conn
            self._conn = None
            threading.Thread(target=old.close, daemon=True).start()

        self._cancel_stats_refresh()
        self._set_status("Connecting…")
        self._connect_btn.configure(state="disabled")
        self._set_room_buttons(False)
        for var in self._stat_vars.values():
            var.set("—")

        def worker():
            try:
                host, port, use_tor, dir_host, dir_port = self._get_params()
                if use_tor:
                    self.after(0, lambda: self._set_status("[Tor] Building circuit…"))
                    conn = connect_tor(dir_host, dir_port, host, port)
                else:
                    conn = connect_direct(host, port)
                self.after(0, lambda: self._on_connect_success(conn))
            except Exception as e:
                self.after(0, lambda: self._on_connect_fail(str(e)))

        threading.Thread(target=worker, daemon=True).start()

    def _on_connect_success(self, conn: Connection):
        self._conn = conn
        self._on_connected(conn)
        self._mark_connected()
        self._connect_btn.configure(state="normal")
        self._set_room_buttons(True)
        self._schedule_stats_refresh(delay=0)

    def _on_connect_fail(self, err: str):
        self._set_status(f"Connection failed: {err}", error=True)
        self._conn_status_var.set("Disconnected")
        self._conn_status_label.configure(text_color=_RED)
        self._connect_btn.configure(state="normal")
        self._set_room_buttons(False)

    def _mark_connected(self):
        label = "Tor" if self._conn and self._conn.is_tor else "Direct"
        self._conn_status_var.set(f"Connected ({label})")
        self._conn_status_label.configure(text_color=_GREEN)
        self._connect_btn.configure(state="normal")
        self._set_status(f"Connected ({label}). Stats refresh every 5 s.")

    def _set_room_buttons(self, enabled: bool):
        state = "normal" if enabled else "disabled"
        self._create_btn.configure(state=state)
        self._join_btn.configure(state=state)
        self._refresh_btn.configure(state=state)

    # ── Stats ─────────────────────────────────────────────────────────────────

    def _schedule_stats_refresh(self, delay: int = _STATS_INTERVAL_MS):
        self._cancel_stats_refresh()
        self._stats_after_id = self.after(delay, self._fetch_stats)

    def _cancel_stats_refresh(self):
        if self._stats_after_id is not None:
            self.after_cancel(self._stats_after_id)
            self._stats_after_id = None

    def _fetch_stats(self, manual: bool = False):
        if self._conn is None:
            return
        if manual:
            self._refresh_btn.configure(state="disabled", text="Refreshing…")

        conn = self._conn  # capture in case it changes

        def worker():
            try:
                conn.send_to('GetStats', {})
                resp = conn.recv_one()
                if resp and resp.get('type') == 'Stats':
                    self.after(0, lambda: self._apply_stats(resp['data']))
            except Exception as e:
                if self.winfo_exists():
                    self.after(0, lambda: self._set_status(
                        f"Stats error: {e}", error=True))
            finally:
                if self.winfo_exists():
                    self.after(0, lambda: self._refresh_btn.configure(
                        state="normal", text="Refresh Now"))
                    # Schedule next auto-refresh
                    self.after(0, lambda: self._schedule_stats_refresh())

        threading.Thread(target=worker, daemon=True).start()

    def _apply_stats(self, stats: dict):
        if not self.winfo_exists():
            return
        for key, var in self._stat_vars.items():
            var.set(str(stats.get(key, "—")))

    # ── Create / Join ─────────────────────────────────────────────────────────

    def _do_create(self):
        if self._conn is None:
            self._set_status("Not connected — click Connect first.", error=True)
            return
        username = self._create_username.get().strip()
        if not username:
            self._set_status("Username is required.", error=True)
            return

        self._cancel_stats_refresh()
        self._set_room_buttons(False)
        self._set_status("Creating room…")
        conn = self._conn

        def worker():
            try:
                conn.send_to('CreateRoom', {'my_username': username})
                resp = conn.recv_one()
                if resp is None:
                    raise RuntimeError("Server disconnected")
                if resp.get('type') == 'Error':
                    raise RuntimeError(resp['data']['error_message'])
                room_code = resp['data']['room_code']
                members   = resp['data']['users']
                self.after(0, lambda: self._on_enter_room(
                    conn, username, room_code, members))
            except Exception as e:
                self.after(0, lambda: self._set_status(f"Error: {e}", error=True))
                self.after(0, lambda: self._set_room_buttons(True))
                self.after(0, lambda: self._schedule_stats_refresh())

        threading.Thread(target=worker, daemon=True).start()

    def _do_join(self):
        if self._conn is None:
            self._set_status("Not connected — click Connect first.", error=True)
            return
        username  = self._join_username.get().strip()
        room_code = self._join_code.get().strip()
        if not username:
            self._set_status("Username is required.", error=True)
            return
        if not room_code:
            self._set_status("Room code is required.", error=True)
            return

        self._cancel_stats_refresh()
        self._set_room_buttons(False)
        self._set_status("Joining room…")
        conn = self._conn

        def worker():
            try:
                conn.send_to('JoinRoom', {
                    'room_code': room_code,
                    'my_username': username,
                })
                resp = conn.recv_one()
                if resp is None:
                    raise RuntimeError("Server disconnected")
                if resp.get('type') == 'Error':
                    raise RuntimeError(resp['data']['error_message'])
                members = resp['data']['users']
                self.after(0, lambda: self._on_enter_room(
                    conn, username, room_code, members))
            except Exception as e:
                self.after(0, lambda: self._set_status(f"Error: {e}", error=True))
                self.after(0, lambda: self._set_room_buttons(True))
                self.after(0, lambda: self._schedule_stats_refresh())

        threading.Thread(target=worker, daemon=True).start()

    def destroy(self):
        self._cancel_stats_refresh()
        super().destroy()
