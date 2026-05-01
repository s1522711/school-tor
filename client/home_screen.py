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
        """
        Build the home screen UI and start stats auto-refresh if already connected.

        How it works:
            Calls ctk.CTkFrame.__init__() with fg_color="transparent" so the
            frame blends into the window background. Stores the callbacks and
            the initial connection. Calls _build_ui() to construct all widgets.

            If initial_conn is not None (user is returning from a chat room
            with the same connection still open), calls _mark_connected() to
            update the status indicator and _schedule_stats_refresh(delay=0)
            to fetch stats immediately. This delay=0 path fetches stats right
            away rather than waiting the full 5 seconds.

            If initial_conn is None (first launch or after a deliberate
            disconnect), sets the status bar to the "fill in settings" prompt
            and disables the Create/Join/Refresh buttons.

        Why stats fetch on delay=0:
            When the user returns from a chat room, the stats panel shows stale
            "—" values. Fetching immediately (delay=0) means the values are
            updated within the first seconds of seeing the home screen, giving
            a responsive feel.

        Args:
            master         — the parent App window.
            initial_conn   — open Connection to reuse, or None.
            on_connected   — on_connected(conn) callback to notify App of a new
                             connection (so App._conn stays up to date).
            on_enter_room  — on_enter_room(conn, username, room_code, members)
                             callback to switch to ChatScreen.
        """
        super().__init__(master, fg_color="transparent")
        self._conn         = initial_conn
        self._on_connected = on_connected
        self._on_enter_room = on_enter_room
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
        """
        Configure the grid layout and build each UI section.

        How it works:
            Sets up a 2-column grid: column 0 is fixed-width (240 px) for the
            stats panel; column 1 expands to fill the remaining space. Three
            rows: top bar (fixed), main content (expands), status bar (fixed).
            Delegates to four private build methods for each section.

        Why it exists:
            Keeps __init__ clean by separating layout configuration from widget
            creation. Each _build_* method can be read in isolation to understand
            its part of the UI.
        """
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
        """
        Build the top bar: app title, connection settings, Connect button, status.

        How it works:
            Creates a horizontal CTkFrame spanning both columns. Uses grid() for
            fine-grained column placement. Adds (in order): title label, Server
            label + entry, Port label + entry, Route-via-Tor checkbox, and the
            Dir Host/Dir Port fields that are initially hidden (grid_remove).
            The Dir fields are un-hidden by _on_tor_toggle when the checkbox is
            ticked. Adds the Connect button and a status label.

        Why the Dir fields are hidden by default:
            Most users will connect directly. Showing the directory fields always
            would clutter the UI for the common case. grid_remove() hides the
            widgets without destroying them, so _on_tor_toggle can call grid()
            to show them instantly without rebuilding.
        """
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
        """
        Show or hide the directory server fields when the Tor checkbox changes.

        How it works:
            Iterates the four Dir Host/Port widgets and calls grid() (to show)
            or grid_remove() (to hide) based on the checkbox state. If a
            connection is currently open and the setting changed, updates the
            status label to prompt the user to reconnect — the existing
            connection still uses the old settings and must be replaced.

        Why it exists:
            The directory server fields are only relevant in Tor mode. Showing
            them conditionally reduces visual noise for direct-mode users and
            prevents them from entering values that will be silently ignored.
        """
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
        """
        Build the left-side stats panel with four counters and a Refresh button.

        How it works:
            Creates a rounded CTkFrame in column 0. For each stat key, adds a
            row frame with a left-aligned label (name) and a right-aligned
            StringVar label (value, initially "—"). Stores the StringVar
            instances in self._stat_vars so _apply_stats() can update them
            later without knowing the widget structure.

        Why StringVar instead of direct label.configure(text=...):
            StringVar lets _apply_stats() update the displayed value by calling
            var.set(...) — a simpler, thread-safe pattern. The label widget
            automatically re-renders whenever the var changes, so we don't need
            to call label.configure() from the worker thread (which would be
            unsafe without self.after()).
        """
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
        """
        Build the right-side panel with Create Room and Join Room forms.

        How it works:
            Creates an outer transparent frame in column 1 with two sub-frames
            stacked vertically (weight=1 each, so they share the available
            height equally). The top sub-frame holds the Create Room form; the
            bottom holds the Join Room form (which has an additional Room Code
            field). Each form has a username entry and an action button.
            Stores references to the buttons so _set_room_buttons() can enable
            or disable them based on connection state.

        Why the outer frame is transparent:
            The two inner frames have their own rounded corners and background.
            Making the outer frame transparent avoids a visible box around both
            forms together, giving a cleaner look.
        """
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
        """
        Build the bottom status bar that shows one-line informational messages.

        How it works:
            Creates a full-width CTkFrame spanning both columns. Places a
            left-anchored CTkLabel whose text is controlled by _set_status().

        Why it exists:
            Provides a consistent place to display transient status messages
            (e.g. "Connecting...", "Error: connection refused", "Creating
            room...") without modal dialogs that would block the UI.
        """
        bar = ctk.CTkFrame(self, corner_radius=0, fg_color=("gray83", "gray20"))
        bar.grid(row=2, column=0, columnspan=2, sticky="ew")
        self._status_label = ctk.CTkLabel(
            bar, text="", anchor="w", font=ctk.CTkFont(size=11))
        self._status_label.pack(side="left", padx=12, pady=5)

    def _set_status(self, text: str, error: bool = False):
        """
        Update the bottom status bar text and colour.

        How it works:
            Sets text_color to _RED for errors, or the theme-appropriate grey
            for normal messages. CTkLabel.configure() is safe to call from the
            main thread (always the case here — callers use self.after(0, ...)).

        Why it exists:
            Centralises the "error → red, info → grey" logic so callers don't
            need to manage colours themselves.

        Args:
            text  — message to display.
            error — True for red text, False for grey.
        """
        color = _RED if error else ("gray40", "gray70")
        self._status_label.configure(text=text, text_color=color)

    # ── Connection management ─────────────────────────────────────────────────

    def _get_params(self):
        """
        Read and validate the connection settings from the UI widgets.

        How it works:
            Reads host (string, no validation), port (must parse as int),
            the Tor checkbox, and the Dir Host/Dir Port fields. Raises
            ValueError with a descriptive message if any field is invalid.

        Why it exists:
            Centralises field validation so _manual_connect() can call this
            once and handle ValueError in one place rather than repeating
            try/int()/strip() calls inline.

        Returns:
            (host: str, port: int, use_tor: bool, dir_host: str, dir_port: int)

        Raises:
            ValueError — if port or dir_port is not a valid integer.
        """
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
        """
        Handle the Connect button click: close old connection, open a new one.

        How it works:
            Cancels any pending stats refresh (avoids a stats request on a
            half-closed connection). Disables the Connect button and room
            buttons to prevent double-clicks. Clears the stats display to "—".

            Closes any existing connection on a background thread (closing a
            TorSocket blocks briefly for TCP teardown; doing it off the main
            thread prevents the UI from freezing).

            Starts a worker thread that calls _get_params(), then
            connect_tor() or connect_direct() depending on the checkbox.
            On success, schedules _on_connect_success(conn) on the main thread
            via self.after(0, ...). On failure, schedules _on_connect_fail(err).

        Why the close happens on a background thread:
            TorSocket.close() calls entry_sock.close() which triggers TCP FIN
            and may block for up to several seconds if the remote side is slow
            to respond. Blocking the Tkinter main thread during this would make
            the window unresponsive.
        """
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
        """
        Handle a successful connection from the worker thread.

        How it works:
            Stores the new connection, notifies App via on_connected() so
            App._conn is updated, marks the UI as connected, re-enables
            buttons, and schedules an immediate stats fetch.
            Always runs on the main thread (called via self.after(0, ...)).

        Why on_connected is called here:
            App needs to track the current connection so _on_close() always
            closes the most recent socket. If we only stored it in self._conn,
            App._conn would still point to the old (now closed) connection.

        Args:
            conn — the newly established Connection.
        """
        self._conn = conn
        self._on_connected(conn)
        self._mark_connected()
        self._connect_btn.configure(state="normal")
        self._set_room_buttons(True)
        self._schedule_stats_refresh(delay=0)

    def _on_connect_fail(self, err: str):
        """
        Handle a connection failure from the worker thread.

        How it works:
            Updates the status bar with a red error message. Sets the status
            indicator to "Disconnected" in red. Re-enables the Connect button
            (the user may fix the settings and try again). Leaves room buttons
            disabled. Always runs on the main thread.

        Args:
            err — the exception message string.
        """
        self._set_status(f"Connection failed: {err}", error=True)
        self._conn_status_var.set("Disconnected")
        self._conn_status_label.configure(text_color=_RED)
        self._connect_btn.configure(state="normal")
        self._set_room_buttons(False)

    def _mark_connected(self):
        """
        Update the status indicator to show the active connection type.

        How it works:
            Reads is_tor from the current connection to choose the label text
            ("Tor" or "Direct"). Updates the small status label in the top bar
            to green "Connected (Tor/Direct)" text. Updates the status bar
            message to include the 5-second refresh note.

        Why it exists:
            Called both when a new connection succeeds (_on_connect_success) and
            when HomeScreen initialises with an existing connection (returning
            from a chat room). Centralising the "mark as connected" logic avoids
            duplicating the three configure() calls in two places.
        """
        label = "Tor" if self._conn and self._conn.is_tor else "Direct"
        self._conn_status_var.set(f"Connected ({label})")
        self._conn_status_label.configure(text_color=_GREEN)
        self._connect_btn.configure(state="normal")
        self._set_status(f"Connected ({label}). Stats refresh every 5 s.")

    def _set_room_buttons(self, enabled: bool):
        """
        Enable or disable the Create, Join, and Refresh buttons simultaneously.

        How it works:
            Iterates the three buttons and sets their state to "normal" or
            "disabled". CustomTkinter buttons do not respond to clicks when
            disabled, so this prevents sending chat commands without a
            connection.

        Why all three buttons together:
            These three buttons share the same precondition (a live connection
            exists). Enabling/disabling them together avoids partial UI states
            where, e.g., Refresh is enabled but Create is not.

        Args:
            enabled — True to enable, False to disable.
        """
        state = "normal" if enabled else "disabled"
        self._create_btn.configure(state=state)
        self._join_btn.configure(state=state)
        self._refresh_btn.configure(state=state)

    # ── Stats ─────────────────────────────────────────────────────────────────

    def _schedule_stats_refresh(self, delay: int = _STATS_INTERVAL_MS):
        """
        Schedule a stats fetch after `delay` milliseconds.

        How it works:
            Cancels any pending refresh first (prevents double-scheduling if
            _schedule_stats_refresh is called while a refresh is already queued).
            Calls self.after(delay, self._fetch_stats) and stores the returned
            handle in _stats_after_id so it can be cancelled later.

        Why it exists:
            Centralises the scheduling logic. Called with delay=0 for an
            immediate fetch, and with the default 5000 ms at the end of each
            successful fetch to chain the next refresh.

        Args:
            delay — milliseconds to wait before fetching (default 5000).
        """
        self._cancel_stats_refresh()
        self._stats_after_id = self.after(delay, self._fetch_stats)

    def _cancel_stats_refresh(self):
        """
        Cancel any pending stats auto-refresh callback.

        How it works:
            If _stats_after_id is not None, calls self.after_cancel() to
            remove the pending callback from the Tkinter event queue, then
            clears the ID. Called before _manual_connect(), on create/join
            workers starting, and on destroy() to prevent callbacks from
            firing on a destroyed widget.

        Why it exists:
            If the user clicks Connect while a stats refresh is scheduled, the
            refresh would fire on the old (already-closed) connection and
            produce an error. Cancelling first prevents this race.
        """
        if self._stats_after_id is not None:
            self.after_cancel(self._stats_after_id)
            self._stats_after_id = None

    def _fetch_stats(self, manual: bool = False):
        """
        Send a GetStats request and update the stats panel.

        How it works:
            Returns immediately if not connected. If manual=True, shows
            "Refreshing…" and disables the Refresh button to prevent stacking.

            Captures self._conn in a local `conn` variable to avoid the TOCTOU
            race where self._conn changes (user clicks Connect) between the
            check and the actual send.

            Worker thread:
                1. Sends GetStats over the existing connection.
                2. Reads one response with recv_one() — safe because no receiver
                   thread is running (the receiver is not started on HomeScreen).
                3. If the response is a Stats message, schedules _apply_stats
                   on the main thread via self.after(0, ...).
                4. In finally: re-enables Refresh button, schedules the next
                   auto-refresh.

        Why recv_one is safe here:
            The background receiver thread is not running while the user is on
            the home screen (stop_receiver() was called when leaving the chat
            room, or the receiver was never started). So recv_one() is the only
            thread reading from the socket — no race.

        Args:
            manual — True when triggered by the Refresh Now button (shows
                     "Refreshing…" label and disables the button).
        """
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
        """
        Update the four stat StringVars with values from the server response.

        How it works:
            Checks winfo_exists() first — if the widget was destroyed between
            the worker scheduling this call and Tkinter executing it, the update
            is silently skipped to avoid TclError. Iterates self._stat_vars and
            calls var.set(str(value)) for each key.

        Why winfo_exists():
            self.after(0, lambda: self._apply_stats(...)) is scheduled by the
            worker thread. If the user navigates away (e.g., enters a room)
            before the event loop processes it, the widget is already destroyed.
            Without this guard, var.set() would raise a TclError because the
            underlying Tk variable was also destroyed.

        Args:
            stats — dict from the Stats server response, e.g.
                    {'total_messages': 42, 'total_files': 3, ...}.
        """
        if not self.winfo_exists():
            return
        for key, var in self._stat_vars.items():
            var.set(str(stats.get(key, "—")))

    # ── Create / Join ─────────────────────────────────────────────────────────

    def _do_create(self):
        """
        Handle the Create Room button click.

        How it works:
            Validates that a connection exists and that the username field is
            not empty. Cancels the stats refresh (avoids a read race on the
            socket). Disables buttons to prevent double-clicks.

            Worker thread:
                1. Sends CreateRoom with the username.
                2. recv_one() reads the RoomCreated response (no receiver
                   thread running, so this is safe).
                3. On success, schedules on_enter_room(conn, username,
                   room_code, members) on the main thread → App.show_chat().
                4. On error or disconnect: schedules a status bar update,
                   re-enables buttons, and reschedules the stats refresh.

        Why cancel stats before sending:
            A stats refresh running concurrently on the same socket would
            interleave its GetStats/Stats exchange with the CreateRoom/RoomCreated
            exchange, causing recv_one() to read the wrong message.
        """
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
        """
        Handle the Join Room button click.

        How it works:
            Validates connection, username, and room_code fields. Cancels the
            stats refresh. Disables buttons.

            Worker thread:
                1. Sends JoinRoom with room_code and username.
                2. recv_one() reads the RoomJoined (or Error) response.
                3. On success, schedules on_enter_room → App.show_chat().
                4. On error: schedules status update, re-enables buttons,
                   reschedules stats.

        Why the same pattern as _do_create:
            Both operations are synchronous request/response on the same
            connection. The cancel-stats/disable-buttons/worker pattern prevents
            all races between concurrent operations on the socket.
        """
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
        """
        Cancel any pending stats refresh before destroying the widget.

        How it works:
            Calls _cancel_stats_refresh() to remove the pending after() callback
            from the Tkinter event queue, then calls super().destroy() to
            actually destroy the frame and all its children. Without this,
            the pending callback would fire after the frame is gone, raising a
            TclError when it tries to configure the destroyed Refresh button.

        Why override destroy:
            Tkinter's after() callbacks are not automatically cancelled when a
            widget is destroyed. Overriding destroy() ensures cleanup happens
            at exactly the right moment — before the widget tree is torn down.
        """
        self._cancel_stats_refresh()
        super().destroy()
