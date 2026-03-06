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
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)
        self._build_topbar()
        self._build_main_area()
        self._build_input_bar()

    def _build_topbar(self):
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
        lbl = ctk.CTkLabel(
            self._user_scroll,
            text=f"  {username}",
            anchor="w",
            font=ctk.CTkFont(size=12),
        )
        lbl.pack(fill="x", padx=4, pady=2)
        self._user_labels[username] = lbl

    def _remove_user_label(self, username: str):
        lbl = self._user_labels.pop(username, None)
        if lbl:
            lbl.destroy()

    # ── Message box helpers ───────────────────────────────────────────────────

    def _append(self, parts: list):
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
        tag = "self_tag" if self_msg else "username"
        self._append([(f"{sender}", tag), (f": {message}\n", None)])

    def _append_system(self, text: str):
        self._append([(f"  {text}\n", "system")])

    def _append_file_notice(self, sender: str, filename: str,
                            filedata: str = "", size_hint: str = ""):
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
        self._append([(f"  Error: {text}\n", "error")])

    # ── Incoming message handler ──────────────────────────────────────────────

    def _handle_message(self, msg_type: str, data: dict):
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
        if self._leaving:
            return
        self._append_error("Disconnected from server.")
        self._leave_btn.configure(state="disabled")
        self._send_btn.configure(state="disabled")
        self._msg_entry.configure(state="disabled")

    # ── Actions ───────────────────────────────────────────────────────────────

    def _copy_room_code(self):
        self.clipboard_clear()
        self.clipboard_append(self._room_code)
        self._append_system(f"Room code copied to clipboard: {self._room_code}")

    def _send_message(self):
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
        """Called when server confirms RoomLeft (e.g. kicked)."""
        self._leaving = True
        self._conn.stop_receiver()
        self._on_leave(self._conn)
