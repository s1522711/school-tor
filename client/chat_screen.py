"""Chat room screen — messages, user list, send bar."""

import base64
import os
import threading
import tkinter
import tkinter.filedialog
import customtkinter as ctk

from network import Connection

_RED       = "#c0392b"
_RED_HOVER = "#922b21"
_COPY_BG   = ("gray78", "gray32")
_COPY_HOVER = ("gray68", "gray26")


class ChatScreen(ctk.CTkFrame):
    def __init__(
        self,
        master,
        connection: Connection,
        username: str,
        room_code: str,
        members: list,
        on_leave,       # on_leave(conn) — returns the still-open connection to App
    ):
        """
        Build the chat UI, start the background receiver, and display initial state.

        How it works:
            Calls ctk.CTkFrame.__init__() with fg_color="transparent". Stores
            all constructor arguments. Initialises _leaving=False (used as a
            flag to suppress spurious disconnect errors during intentional leave).

            _build_ui() constructs the three-section layout (top bar, main area,
            input bar). start_receiver() starts the background daemon thread that
            delivers incoming messages (IncomingMessage, UserJoined, etc.) to the
            main thread via on_message callbacks scheduled with self.after(0, ...).

            Appends two system messages to the message box: the room code and
            the initial member list. These are displayed immediately so the user
            has context without waiting for any server push.

        Why start_receiver is called in __init__:
            The receiver must be running as soon as the screen is visible, because
            other users may send messages between the moment this screen is
            created and the moment the user types their first message. Starting
            it here guarantees no messages are missed.

        Args:
            master     — the parent App window.
            connection — the open Connection (direct or Tor) to the chat server.
            username   — the user's display name in this room.
            room_code  — UUID of the room (shown in the top bar).
            members    — initial member list from the server's join/create response.
            on_leave   — on_leave(conn) callback to return to HomeScreen with
                         the still-open connection.
        """
        super().__init__(master, fg_color="transparent")
        self._conn      = connection
        self._username  = username
        self._room_code = room_code
        self._members   = list(members)
        self._on_leave  = on_leave
        self._leaving   = False

        self._build_ui()
        self._conn.start_receiver(
            on_message=lambda t, d: self.after(0, lambda: self._handle_message(t, d)),
            on_disconnect=lambda: self.after(0, self._handle_disconnect),
        )
        self._append_system(f"Joined room {room_code}")
        self._append_system(f"Members: {', '.join(self._members)}")

    # ── Layout ────────────────────────────────────────────────────────────────

    def _build_ui(self):
        """
        Configure the grid and call sub-builders for each UI section.

        How it works:
            Sets row 1 (the main chat area) to weight=1 so it expands to fill
            the available height. Rows 0 (top bar) and 2 (input bar) are fixed
            height. Column 0 fills the full width (both the chat box and the
            user list are inside the main area). Delegates to three _build_*
            methods.

        Why it exists:
            Separates the "how the sections are arranged" concern from the
            "what each section contains" concern. Each _build_* method can be
            read independently.
        """
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)
        self._build_topbar()
        self._build_main_area()
        self._build_input_bar()

    def _build_topbar(self):
        """
        Build the top bar: room code display, Copy Code button, Leave Room button.

        How it works:
            Creates a full-width frame (corner_radius=0 for a flat edge). Uses
            grid() inside the bar: the room code label is at column 1, the Copy
            button at column 2 (sticky="w"), and the Leave button at column 3
            (sticky="e"). Column 2 has weight=1 so it absorbs slack, keeping
            the Leave button flush right.
            Stores _leave_btn so _do_leave() can disable it to prevent
            double-clicks.

        Why the Leave button is red:
            A destructive action (leaving the room, losing real-time messaging)
            conventionally uses a red / warning colour to signal to the user
            that this action has consequences.
        """
        bar = ctk.CTkFrame(self, corner_radius=0, fg_color=("gray88", "gray17"))
        bar.grid(row=0, column=0, sticky="ew")
        bar.grid_columnconfigure(2, weight=1)

        ctk.CTkLabel(
            bar, text="Room:",
            font=ctk.CTkFont(size=12),
        ).grid(row=0, column=0, padx=(16, 4), pady=11)

        ctk.CTkLabel(
            bar, text=self._room_code,
            font=ctk.CTkFont(size=12, weight="bold"),
        ).grid(row=0, column=1, pady=11)

        ctk.CTkButton(
            bar, text="Copy Code",
            width=90, height=28,
            fg_color=_COPY_BG, hover_color=_COPY_HOVER,
            text_color=("gray20", "gray90"),
            command=self._copy_room_code,
        ).grid(row=0, column=2, padx=(8, 0), pady=9, sticky="w")

        self._leave_btn = ctk.CTkButton(
            bar, text="Leave Room",
            width=100, height=28,
            fg_color=_RED, hover_color=_RED_HOVER,
            command=self._do_leave,
        )
        self._leave_btn.grid(row=0, column=3, padx=(0, 16), pady=9, sticky="e")

    def _build_main_area(self):
        """
        Build the main area: a scrollable message box on the left and a user
        list on the right.

        How it works:
            Creates an inner pane frame with two columns: column 0 (message box,
            weight=1) and column 1 (user list, fixed at 170 px). The message box
            is a CTkTextbox in state="disabled" (read-only); the text widget's
            internal _textbox is accessed directly to configure colour tags
            because CTkTextbox does not expose tag_configure. Five named tags
            are configured with different colours and fonts for different message
            types.

            The user list is a CTkScrollableFrame populated by _add_user_label()
            as UserJoined events arrive. Stores initial members from the
            constructor. The _user_labels dict maps username → CTkLabel for O(1)
            lookup in _remove_user_label().

        Why access _textbox directly:
            CTkTextbox wraps a tk.Text widget but does not expose tag_configure
            or window_create through its public API. Accessing the internal
            _textbox is necessary to apply colour-coded text and to embed the
            Save button inside the message flow for file notices.

        Why state="disabled":
            Prevents users from typing directly into the message box (it is
            display-only). The _append() method temporarily enables it, inserts
            text, and re-disables it for each update.
        """
        pane = ctk.CTkFrame(self, fg_color="transparent")
        pane.grid(row=1, column=0, sticky="nsew", padx=10, pady=(8, 0))
        pane.grid_columnconfigure(0, weight=1)
        pane.grid_columnconfigure(1, weight=0, minsize=170)
        pane.grid_rowconfigure(0, weight=1)

        # ── Message box ──────────────────────────────────────────────────────
        msg_frame = ctk.CTkFrame(pane, corner_radius=8)
        msg_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        msg_frame.grid_rowconfigure(0, weight=1)
        msg_frame.grid_columnconfigure(0, weight=1)

        self._msg_box = ctk.CTkTextbox(
            msg_frame,
            state="disabled",
            wrap="word",
            font=ctk.CTkFont(family="Consolas", size=13),
            corner_radius=8,
        )
        self._msg_box.grid(row=0, column=0, sticky="nsew", padx=2, pady=2)

        tb = self._msg_box._textbox
        tb.tag_configure("username", foreground="#4a9eff",
                         font=("Consolas", 13, "bold"))
        tb.tag_configure("self_tag", foreground="#5cb85c",
                         font=("Consolas", 13, "bold"))
        tb.tag_configure("system",   foreground="#888888",
                         font=("Consolas", 12, "italic"))
        tb.tag_configure("file_tag", foreground="#f0a500",
                         font=("Consolas", 12, "italic"))
        tb.tag_configure("error",    foreground="#e05252",
                         font=("Consolas", 12))

        # ── User list ─────────────────────────────────────────────────────────
        user_frame = ctk.CTkFrame(pane, corner_radius=8, width=170)
        user_frame.grid(row=0, column=1, sticky="nsew", padx=(5, 0))
        user_frame.grid_propagate(False)
        user_frame.grid_rowconfigure(1, weight=1)
        user_frame.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            user_frame, text="Users",
            font=ctk.CTkFont(size=13, weight="bold"),
        ).grid(row=0, column=0, sticky="w", padx=12, pady=(12, 4))

        ctk.CTkFrame(
            user_frame, height=1, fg_color=("gray75", "gray35"),
        ).grid(row=0, column=0, sticky="ew", padx=12, pady=(36, 0))

        self._user_scroll = ctk.CTkScrollableFrame(
            user_frame, fg_color="transparent", corner_radius=0)
        self._user_scroll.grid(row=1, column=0, sticky="nsew", padx=4, pady=(4, 8))

        self._user_labels: dict[str, ctk.CTkLabel] = {}
        for m in self._members:
            self._add_user_label(m)

    def _build_input_bar(self):
        """
        Build the bottom input bar: Attach File button, message entry, Send button.

        How it works:
            Creates a full-width frame (corner_radius=0). Uses grid() with the
            entry at column 1 (weight=1) to absorb all available horizontal
            space. Binds <Return> on the entry to _send_message() so Enter key
            sends without clicking. Stores _msg_entry and _send_btn for later
            use (disabling on disconnect in _handle_disconnect).

        Why it exists:
            The input bar is a separate method from _build_main_area so the
            layout of the three rows (top bar, main area, input bar) can be
            understood at a glance from _build_ui.
        """
        bar = ctk.CTkFrame(self, corner_radius=0, fg_color=("gray86", "gray18"))
        bar.grid(row=2, column=0, sticky="ew", pady=(4, 0))
        bar.grid_columnconfigure(1, weight=1)

        ctk.CTkButton(
            bar, text="Attach File",
            width=90, height=34,
            fg_color=_COPY_BG, hover_color=_COPY_HOVER,
            text_color=("gray20", "gray90"),
            command=self._pick_file,
        ).grid(row=0, column=0, padx=(12, 6), pady=10)

        self._msg_entry = ctk.CTkEntry(
            bar,
            placeholder_text="Type a message…",
            height=34,
            font=ctk.CTkFont(size=13),
        )
        self._msg_entry.grid(row=0, column=1, padx=6, pady=10, sticky="ew")
        self._msg_entry.bind("<Return>", lambda _e: self._send_message())

        self._send_btn = ctk.CTkButton(
            bar, text="Send", width=72, height=34,
            command=self._send_message,
        )
        self._send_btn.grid(row=0, column=2, padx=(6, 12), pady=10)

    # ── User list helpers ─────────────────────────────────────────────────────

    def _add_user_label(self, username: str):
        """
        Add a username label to the scrollable user list.

        How it works:
            Creates a CTkLabel with leading spaces for indentation, packs it
            into the scrollable frame, and stores it in _user_labels keyed by
            username. The pack layout adds labels in order of arrival (new
            joiners appear at the bottom).

        Why store in _user_labels:
            _remove_user_label needs O(1) access to the label widget to call
            lbl.destroy(). Iterating all children of the scrollable frame every
            time a user leaves would be O(n).

        Args:
            username — the display name to add to the list.
        """
        lbl = ctk.CTkLabel(
            self._user_scroll,
            text=f"  {username}",
            anchor="w",
            font=ctk.CTkFont(size=12),
        )
        lbl.pack(fill="x", padx=4, pady=2)
        self._user_labels[username] = lbl

    def _remove_user_label(self, username: str):
        """
        Remove a username label from the scrollable user list.

        How it works:
            Pops the label from _user_labels (returns None if absent — handles
            the edge case where the same username is removed twice). Calls
            lbl.destroy() to remove the widget from the scrollable frame.

        Why it exists:
            Called by _handle_message() on UserLeft events. The label must be
            destroyed (not just hidden) so the space it occupied is reclaimed
            and the user list reflects the true current membership.

        Args:
            username — the display name to remove.
        """
        lbl = self._user_labels.pop(username, None)
        if lbl:
            lbl.destroy()

    # ── Message box helpers ───────────────────────────────────────────────────

    def _append(self, parts: list):
        """
        Insert one or more tagged text segments into the message box.

        How it works:
            Temporarily enables the CTkTextbox (setting it back to "disabled"
            would prevent the user from editing; state="normal" is needed to
            insert). For each (text, tag) pair: if tag is not None, calls
            tb.insert("end", text, tag) to apply the colour/font tag; otherwise
            calls tb.insert("end", text) for unstyled text. Re-disables the
            textbox and scrolls to the end with tb.see("end") so new messages
            are always visible.

        Why pairs instead of a single string:
            A single message line may contain mixed styling — e.g., the username
            part in blue-bold and the message text in normal white. Passing a
            list of (text, tag) pairs allows fine-grained per-run tagging within
            a single logical line.

        Args:
            parts — list of (text: str, tag: str | None) tuples.
        """
        tb = self._msg_box._textbox
        tb.configure(state="normal")
        for text, tag in parts:
            if tag:
                tb.insert("end", text, tag)
            else:
                tb.insert("end", text)
        tb.configure(state="disabled")
        tb.see("end")

    def _append_chat(self, sender: str, message: str, self_msg: bool = False):
        """
        Append a formatted chat message line to the message box.

        How it works:
            Chooses "self_tag" (green) for messages from the local user and
            "username" (blue) for messages from others. Calls _append() with
            two parts: the sender name (tagged) and ": message\n" (untagged).

        Why distinguish self vs others:
            Green for your own messages gives instant visual confirmation that
            the send succeeded and helps you track the conversation flow.

        Args:
            sender   — display name of the message author.
            message  — the message text.
            self_msg — True if the message was sent by this user.
        """
        tag = "self_tag" if self_msg else "username"
        self._append([(f"{sender}", tag), (f": {message}\n", None)])

    def _append_system(self, text: str):
        """
        Append a grey italic system message to the message box.

        How it works:
            Calls _append() with a single part using the "system" tag (grey
            italic). Prepends two spaces for visual indentation.

        Why it exists:
            System events (joined, left, stats output) should be visually
            distinct from chat messages. The grey italic style is a conventional
            IRC/chat convention for system-level events.

        Args:
            text — the system message to display.
        """
        self._append([(f"  {text}\n", "system")])

    def _append_file_notice(self, sender: str, filename: str,
                            filedata: str = "", size_hint: str = ""):
        """
        Append a file-received notice with an embedded Save button.

        How it works:
            Temporarily enables the textbox. Inserts the sender/filename text
            with the "file_tag" (amber italic). If filedata is non-empty,
            creates a tkinter.Button (not CTkButton — CTkButton cannot be
            embedded in a text widget) and inserts it into the textbox using
            tb.window_create("end", window=btn). The button's command is a
            lambda that captures filename and filedata at creation time, so
            clicking it later (after the function has returned and the local
            variables are gone) still works correctly. Re-disables the textbox
            and scrolls to end.

        Why a tkinter.Button inside a CTkTextbox:
            CTkTextbox wraps tk.Text, which supports window_create() to embed
            arbitrary Tk widgets. There is no CTk equivalent for this. The
            embedded button must be a tk.Button (or any tk widget), not a
            CTkButton, because window_create requires a tk widget ID.

        Why capture filedata in the lambda:
            File data can be megabytes of base64. We do not store it in self
            to avoid accumulating large strings in memory for every received
            file. The lambda captures a reference to the string at the moment
            the button is created; clicking Save later decodes and writes it.

        Args:
            sender    — display name of the file sender.
            filename  — the original filename.
            filedata  — base64-encoded file content (may be empty for previews).
            size_hint — human-readable size string like "~42 KB".
        """
        tb = self._msg_box._textbox
        tb.configure(state="normal")
        suffix = f"  ({size_hint})" if size_hint else ""
        tb.insert("end", f"  {sender} sent: {filename}{suffix}  ", "file_tag")

        if filedata:
            btn = tkinter.Button(
                tb,
                text="Save",
                command=lambda fn=filename, fd=filedata: self._save_file(fn, fd),
                relief="flat",
                bg="#b07800",
                fg="white",
                activebackground="#f0a500",
                activeforeground="white",
                font=("Consolas", 10),
                padx=5, pady=1,
                cursor="hand2",
                bd=0,
            )
            tb.window_create("end", window=btn)

        tb.insert("end", "\n")
        tb.configure(state="disabled")
        tb.see("end")

    def _append_error(self, text: str):
        """
        Append a red error message to the message box.

        How it works:
            Calls _append() with the "error" tag (red text). The leading spaces
            and trailing newline match the format of _append_system().

        Why it exists:
            Error messages (send failures, server errors, disconnect notices)
            need to stand out immediately in the message history. Red is the
            conventional error colour.

        Args:
            text — the error description to display.
        """
        self._append([(f"  Error: {text}\n", "error")])

    # ── Incoming message handler ──────────────────────────────────────────────

    def _handle_message(self, msg_type: str, data: dict):
        """
        Route one server-pushed message to the appropriate UI update.

        How it works:
            Always called on the main thread (scheduled via self.after(0, ...)).
            A series of if/elif branches handles each known message type:

            IncomingMessage — appends a chat line with the sender's name.
            IncomingFile    — computes an approximate file size from the base64
                             length, appends a file notice with a Save button.
            UserJoined      — adds the username to the user list (guarded against
                             duplicates) and appends a system message.
            UserLeft        — removes the username from the user list, appends
                             a system message.
            RoomLeft        — the server confirmed the user has left (e.g. kicked
                             or the room was closed); calls _cleanup_and_leave().
            Stats           — formats and appends a system message with all four
                             counters.
            Error           — appends a red error message.

        Why always on the main thread:
            Tkinter is not thread-safe. All widget updates (insert, configure,
            destroy, etc.) must run on the thread that created the widgets — the
            main thread. The lambda in start_receiver wraps every call in
            self.after(0, ...) to ensure this.

        Args:
            msg_type — the message type string from the server.
            data     — the message payload dict.
        """
        if msg_type == 'IncomingMessage':
            self._append_chat(
                data.get('from_username', '?'),
                data.get('message', ''),
            )
        elif msg_type == 'IncomingFile':
            raw_b64 = data.get('filedata', '')
            approx  = f"~{len(raw_b64) * 3 // 4 // 1024} KB" if raw_b64 else ""
            self._append_file_notice(
                data.get('from_username', '?'),
                data.get('filename', ''),
                filedata=raw_b64,
                size_hint=approx,
            )
        elif msg_type == 'UserJoined':
            u = data.get('username', '')
            if u not in self._user_labels:
                self._members.append(u)
                self._add_user_label(u)
            self._append_system(f"*** {u} joined the room ***")
        elif msg_type == 'UserLeft':
            u = data.get('username', '')
            self._members = [m for m in self._members if m != u]
            self._remove_user_label(u)
            self._append_system(f"*** {u} left the room ***")
        elif msg_type == 'RoomLeft':
            self._append_system("You left the room.")
            self._cleanup_and_leave()
        elif msg_type == 'Stats':
            self._append_system(
                f"Stats — messages: {data.get('total_messages')}  "
                f"files: {data.get('total_files')}  "
                f"users: {data.get('total_users')}  "
                f"rooms: {data.get('total_rooms')}"
            )
        elif msg_type == 'Error':
            self._append_error(data.get('error_message', 'Unknown error'))

    def _handle_disconnect(self):
        """
        Handle an unexpected server disconnect while in a chat room.

        How it works:
            Checks _leaving first — if True, the disconnect was triggered by
            _do_leave() or _cleanup_and_leave(), which already handled the UI
            transition. In that case, silently returns to avoid a spurious error.

            If the disconnect was genuinely unexpected: appends a red error
            message, and disables the Leave, Send, and message entry widgets to
            prevent the user from attempting further interaction on a dead socket.

        Why the _leaving guard:
            stop_receiver() is called from a worker thread inside _do_leave(),
            which causes the receiver thread to exit. When the receiver exits
            after being stopped (rather than due to a real disconnect), it
            should not call on_disconnect. However, there is a small race window
            between stop_receiver() setting _recv_stop and the receiver thread
            checking it. _leaving acts as a backup guard to prevent a false
            "Disconnected" error in that window.

        Always called on the main thread via self.after(0, ...).
        """
        if self._leaving:
            return
        self._append_error("Disconnected from server.")
        self._leave_btn.configure(state="disabled")
        self._send_btn.configure(state="disabled")
        self._msg_entry.configure(state="disabled")

    # ── Actions ───────────────────────────────────────────────────────────────

    def _copy_room_code(self):
        """
        Copy the room code to the system clipboard and confirm in the message box.

        How it works:
            Calls self.clipboard_clear() then self.clipboard_append(room_code)
            to overwrite the clipboard. Appends a system message confirming the
            copy so the user knows it succeeded.

        Why it exists:
            The room code is a UUID (e.g. "3f1a9c4d-...") — too long to read
            aloud. Users share it by copying and pasting. A one-click copy
            button removes friction and prevents transcription errors.
        """
        self.clipboard_clear()
        self.clipboard_append(self._room_code)
        self._append_system(f"Room code copied to clipboard: {self._room_code}")

    def _send_message(self):
        """
        Send the typed message and display it locally immediately.

        How it works:
            Reads and clears the message entry. Returns early if the text is
            empty (prevents sending blank messages). Appends the message locally
            with self_tag (green) immediately — this gives instant visual
            feedback without waiting for the network round-trip.

            Worker thread: calls conn.send_to('SendMessage', ...). For a Tor
            connection, this blocks for the full circuit round-trip; running it
            on a worker thread prevents the GUI from freezing. On exception,
            schedules an error message on the main thread.

        Why display before the send completes:
            In direct mode the send is nearly instant and the local display acts
            as confirmation. In Tor mode the circuit round-trip may take 100–500 ms;
            showing the message immediately makes the chat feel responsive rather
            than sluggish. If the send fails, the error message appears after.
        """
        text = self._msg_entry.get().strip()
        if not text:
            return
        self._msg_entry.delete(0, "end")
        self._append_chat("you", text, self_msg=True)

        def worker():
            try:
                self._conn.send_to('SendMessage', {'message': text})
            except Exception as e:
                self.after(0, lambda: self._append_error(f"Send failed: {e}"))

        threading.Thread(target=worker, daemon=True).start()

    def _pick_file(self):
        """
        Open a file picker dialog and send the selected file.

        How it works:
            Opens a tkinter.filedialog.askopenfilename dialog. Returns early if
            no file is selected (user cancelled). Reads the file, base64-encodes
            the raw bytes, and records the human-readable size. Appends a
            "Sending..." system message. Worker thread sends SendFile with the
            filename and base64 filedata. On success, appends a "Sent" message;
            on failure, appends an error.

        Why base64:
            JSON is UTF-8 text and cannot contain arbitrary bytes. Base64 encodes
            any binary file as a safe ASCII string. The receiving side (chat server
            → other clients) passes the filedata string through unchanged;
            the recipient calls base64.b64decode() in _save_file().

        Why run the send on a worker thread:
            For large files over a Tor circuit, base64 encoding can be megabytes
            and the circuit round-trip adds latency. Blocking the main thread for
            this would freeze the entire UI.
        """
        path = tkinter.filedialog.askopenfilename(title="Select file to send")
        if not path:
            return
        try:
            with open(path, 'rb') as f:
                raw = f.read()
            filedata = base64.b64encode(raw).decode()
            filename = os.path.basename(path)
        except OSError as e:
            self._append_error(f"Could not read file: {e}")
            return

        size_str = f"{len(raw) / 1024:.1f} KB"
        self._append_system(f"Sending {filename} ({size_str})…")

        def worker():
            try:
                self._conn.send_to('SendFile', {
                    'filename': filename,
                    'filedata': filedata,
                })
                self.after(0, lambda: self._append_system(
                    f"Sent {filename} ({size_str})"))
            except Exception as e:
                self.after(0, lambda: self._append_error(f"File send failed: {e}"))

        threading.Thread(target=worker, daemon=True).start()

    def _save_file(self, filename: str, filedata: str):
        """
        Save a received file to disk using a save-as dialog.

        How it works:
            Opens tkinter.filedialog.asksaveasfilename with initialfile=filename
            so the user sees the original filename pre-filled. Returns early if
            cancelled. Decodes the base64 filedata and writes the raw bytes to
            the chosen path in binary mode. Appends a system message on success
            or an error message on failure.

        Why it runs on the main thread:
            This function is the button command in _append_file_notice(), so it
            is always called from the main thread (Tkinter event dispatch).
            File I/O here is synchronous, but for typical file sizes (< few MB)
            it completes instantly. Very large files could block the UI briefly,
            but this is an acceptable trade-off for simplicity.

        Args:
            filename — the original filename (suggested save name).
            filedata — base64-encoded file content.
        """
        save_path = tkinter.filedialog.asksaveasfilename(
            title="Save received file",
            initialfile=filename,
        )
        if not save_path:
            return
        try:
            with open(save_path, 'wb') as f:
                f.write(base64.b64decode(filedata))
            self._append_system(f"Saved: {os.path.basename(save_path)}")
        except Exception as e:
            self._append_error(f"Could not save file: {e}")

    def _do_leave(self):
        """
        Handle the Leave Room button click (voluntary leave).

        How it works:
            Sets _leaving=True immediately to suppress any spurious
            "Disconnected" error from the receiver thread. Disables the Leave
            button to prevent double-clicks.

            Worker thread:
                1. Sends LeaveRoom to the server (the server broadcasts UserLeft
                   to remaining members and sends RoomLeft back to this client).
                2. Calls conn.stop_receiver() to stop the receiver thread.
                   The socket stays open — HomeScreen will reuse it for stats.
                3. Schedules on_leave(conn) on the main thread →
                   App.show_home(conn=conn), which creates HomeScreen with the
                   still-open connection.

        Why stop_receiver before on_leave:
            on_leave() → show_home() → HomeScreen() runs on the main thread.
            HomeScreen may immediately call recv_one() to fetch stats. If the
            receiver thread is still running, it could steal that stats response,
            causing recv_one() to block indefinitely. Stopping the receiver first
            guarantees the socket is exclusively available for synchronous use.

        Why the socket stays open:
            Building a new Tor circuit (in Tor mode) takes hundreds of
            milliseconds and triggers a new RSA key negotiation. Reusing the
            existing connection is much faster and makes navigation between rooms
            and the home screen feel instant.
        """
        self._leaving = True
        self._leave_btn.configure(state="disabled")

        def worker():
            try:
                self._conn.send_to('LeaveRoom', {})
            except Exception:
                pass
            # Stop receiver but keep socket open — home screen will reuse it
            self._conn.stop_receiver()
            self.after(0, lambda: self._on_leave(self._conn))

        threading.Thread(target=worker, daemon=True).start()

    def _cleanup_and_leave(self):
        """
        Handle a server-initiated leave (RoomLeft pushed by the server).

        How it works:
            Sets _leaving=True, stops the receiver without sending LeaveRoom
            (the server already knows this client left — sending again would
            produce an error), and calls on_leave(conn) on the main thread.

        Why no LeaveRoom message:
            _cleanup_and_leave is called when the server sends RoomLeft — the
            server has already removed this client from the room. Sending
            LeaveRoom again would arrive after the client is no longer in any
            room and produce a "Not in a room" error response.

        Why it exists as a separate method from _do_leave:
            _do_leave is user-initiated (button click): it sends LeaveRoom first,
            then cleans up. _cleanup_and_leave is server-initiated (receiving
            RoomLeft): it cleans up without sending. Having two methods makes the
            intent explicit and avoids a flag argument.
        """
        self._leaving = True
        self._conn.stop_receiver()
        self._on_leave(self._conn)
