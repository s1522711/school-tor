"""
Chat Server

Manages chat rooms over the onion network. Clients connect through their own
Tor circuit; this server only sees the exit node's address, not the real client.

Rooms are identified by a UUID. Each connection belongs to at most one room.

Usage:
    python Servers/chat_server.py
    python Servers/chat_server.py --host 0.0.0.0 --port 8001 --db chat.db

Protocol  (see protocol.md):
    Frame:  [4-byte big-endian length][JSON body]
    Body:   {"type": "MessageType", "data": {...}}

Architecture:
    ChatServer          — TCP accept loop; spawns one thread per connection
    ChatMessageHandler  — stateless dispatcher; routes messages to UserManager
    UserManager         — all room/user logic, purely in-memory
    Room                — in-memory socket store for one room (conn -> username)
    Database            — SQLite, stats only; rooms and users never touch the DB
"""

import socket
import json
import threading
import uuid
import argparse
import sqlite3
import os


# ── Wire helpers ──────────────────────────────────────────────────────────────

def recv_msg(sock):
    """
    Read one complete length-prefixed message from a TCP socket.

    How it works:
        Reads a 4-byte big-endian header to learn the payload size, then reads
        exactly that many bytes — looping in both cases because a single recv()
        call on a TCP stream can return fewer bytes than requested.
        Returns None on clean EOF (the client disconnected).

    Why it exists:
        TCP is a stream protocol. Without explicit framing, two successive JSON
        messages could arrive fused in one recv() call, or a single message
        could be split across multiple calls. This function makes the chat
        server completely independent of packet boundaries.

    Returns:
        bytes — the raw JSON payload, or None if the socket closed.
    """
    header = b''
    while len(header) < 4:
        chunk = sock.recv(4 - len(header))
        if not chunk:
            return None
        header += chunk
    length = int.from_bytes(header, 'big')
    data = b''
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            return None
        data += chunk
    return data


def send_msg(sock, data):
    """
    Send data over TCP with a 4-byte big-endian length prefix.

    How it works:
        Accepts dict (→ JSON bytes), str (→ UTF-8 bytes), or raw bytes.
        Prepends the 4-byte length and calls sendall() which loops until all
        bytes have been handed to the kernel, even if the send buffer is full.

    Why it exists:
        The paired receiving side (recv_msg) expects this exact framing.
        sendall() is used rather than send() because a broken socket or full
        buffer could cause send() to write only a partial payload without
        raising an exception, silently corrupting the stream.
    """
    if isinstance(data, dict):
        data = json.dumps(data).encode()
    elif isinstance(data, str):
        data = data.encode()
    sock.sendall(len(data).to_bytes(4, 'big') + data)


def send_to(sock, msg_type: str, data: dict):
    """
    Send a typed chat-protocol message to one socket.

    How it works:
        Wraps the data dict in the standard chat envelope
        {"type": msg_type, "data": data} and delegates to send_msg.
        Silently discards any exception (broken pipe, closed socket) so
        that one bad recipient does not interrupt a broadcast loop.

    Why it exists:
        Every response and broadcast in the chat protocol has the same
        {"type": ..., "data": ...} structure. This function centralises
        that wrapping so callers can express intent ("send a RoomCreated
        message") rather than repeatedly constructing the envelope by hand.
        The silent exception handling is intentional: in broadcast scenarios
        a dead client's socket should not crash the server or prevent the
        message from reaching other clients.

    Args:
        sock     — destination socket.
        msg_type — the 'type' field (e.g. 'RoomCreated', 'IncomingMessage').
        data     — the 'data' dict payload.
    """
    try:
        send_msg(sock, {'type': msg_type, 'data': data})
    except Exception:
        pass


def send_error(sock, message: str):
    """
    Send an Error message to a client.

    How it works:
        Calls send_to with type='Error' and a data dict containing the error
        message string. This produces {"type": "Error", "data":
        {"error_message": message}} on the wire.

    Why it exists:
        Centralises error formatting so every code path that needs to reject
        a request produces identically structured Error messages that clients
        can parse uniformly.

    Args:
        sock    — the client socket to send the error to.
        message — human-readable description of what went wrong.
    """
    send_to(sock, 'Error', {'error_message': message})


# ── Database ──────────────────────────────────────────────────────────────────

class Database:
    """
    Persistent store for cumulative stats only.

    Schema
    ------
    stats — one row per counter key; survives server restarts.

    Rooms and users are never stored here; they live purely in UserManager's
    in-memory dicts and are lost on restart (sockets are gone anyway).

    All public methods are thread-safe via a single internal lock.
    check_same_thread=False lets worker threads share the connection while
    the lock serialises all access.
    """

    _STAT_KEYS = ('total_messages', 'total_files', 'total_users', 'total_rooms')

    def __init__(self, path: str):
        """
        Open (or create) the SQLite database at `path` and seed the stats table.

        How it works:
            sqlite3.connect() creates the file if it does not exist.
            check_same_thread=False allows the single connection object to be
            shared by multiple worker threads; correctness is maintained by the
            _lock that serialises every SQL operation.
            row_factory=sqlite3.Row makes fetchall() return Row objects that
            can be accessed by column name (row['key']) rather than by index.
            _init_schema() creates the table and seeds the stat keys (using
            INSERT OR IGNORE so existing data is preserved across restarts).

        Why it exists:
            Stats (total messages, files, users, rooms) accumulate across server
            restarts. Without a database they would reset to zero every time the
            server process exits. In-memory room/user state is intentionally *not*
            persisted because the sockets those rooms point to are destroyed when
            the process exits, making any stored references useless.

        Args:
            path — filesystem path to the SQLite file (created if absent).
        """
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._init_schema()
        print(f"[DB] Opened {path}")

    def _init_schema(self):
        """
        Create the stats table if it does not exist and seed all known keys.

        How it works:
            CREATE TABLE IF NOT EXISTS is idempotent — safe to call on every
            startup whether the DB is new or existing.
            INSERT OR IGNORE seeds each stat key with value=0 only if the key
            is not already present, preserving accumulated counts across restarts.
            commit() flushes both operations to disk before returning.

        Why it exists:
            Separating schema initialisation from __init__ makes the
            constructor readable and makes it easy to call _init_schema again
            in tests (e.g., after wiping the DB). Called once at startup from
            within the lock to avoid any race with concurrent worker threads
            that might try to read stats before the table exists.
        """
        with self._lock:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS stats (
                    key   TEXT PRIMARY KEY,
                    value INTEGER NOT NULL DEFAULT 0
                )
            """)
            for key in self._STAT_KEYS:
                self._conn.execute(
                    "INSERT OR IGNORE INTO stats (key, value) VALUES (?, 0)", (key,)
                )
            self._conn.commit()
        print(f"[DB] Stats restored: {self.get_stats()}")

    def increment_stat(self, *keys):
        """
        Atomically increment one or more stat counters by 1 each.

        How it works:
            Executes UPDATE stats SET value = value + 1 WHERE key = ? for each
            key inside the _lock. Using SQL arithmetic (value + 1) rather than
            a read-modify-write in Python is a single atomic operation in SQLite,
            avoiding races between concurrent threads. All increments for one call
            share a single commit() for efficiency.

        Why it exists:
            Called whenever a significant event occurs: a room is created, a user
            joins, a message or file is sent. Incrementing in the DB rather than
            memory means the totals are durable — a server crash between events
            loses at most the current call, not all accumulated history.

        Args:
            *keys — one or more stat key strings (must be in _STAT_KEYS).
        """
        with self._lock:
            for key in keys:
                self._conn.execute(
                    "UPDATE stats SET value = value + 1 WHERE key = ?", (key,)
                )
            self._conn.commit()

    def get_stats(self) -> dict:
        """
        Read all stat counters from the database and return them as a plain dict.

        How it works:
            Executes SELECT key, value FROM stats under the lock, then converts
            the list of Row objects to a {key: value} dict outside the lock.
            The dict is transient — it exists only long enough to be serialised
            and sent to the requesting client.

        Why it exists:
            Stats are always read from the DB, not from an in-memory cache. This
            guarantees that GetStats reflects the true persistent state even if
            the server was restarted between the last increment and this read.
            There is no in-memory cache to get out of sync.

        Returns:
            dict — e.g. {'total_messages': 42, 'total_files': 3, ...}
        """
        with self._lock:
            rows = self._conn.execute("SELECT key, value FROM stats").fetchall()
        return {row['key']: row['value'] for row in rows}


# ── Room ──────────────────────────────────────────────────────────────────────

class Room:
    """
    In-memory socket store for one active room.
    The DB holds the authoritative member list; this object holds the sockets
    needed to actually send messages to those members.

    clients: dict[socket, str]  — conn -> username
    """

    def __init__(self, room_code: str):
        """
        Initialise an empty room with the given UUID room code.

        How it works:
            Stores the room_code (used for display and JOIN validation) and an
            empty clients dict that maps socket → username. Both fields are set
            here; no DB interaction happens at construction time.

        Why it exists:
            Rooms are created dynamically when a user sends CreateRoom. Keeping
            the room purely in memory means no DB schema for rooms is needed and
            cleanup on disconnect is automatic (the object is garbage-collected
            once the last reference in UserManager._rooms is removed).

        Args:
            room_code — UUID string that uniquely identifies this room.
        """
        self.room_code = room_code
        self.clients: dict = {}  # conn -> username

    def add(self, conn, username: str):
        """
        Add a client socket → username mapping to this room.

        How it works:
            Simple dict assignment. The caller (UserManager) is responsible for
            holding the lock while calling this method so no additional
            synchronisation is needed inside Room itself.

        Why it exists:
            Centralises the mutation so UserManager does not need to know the
            internal structure of Room.

        Args:
            conn     — the client's TCP socket.
            username — display name chosen by the client.
        """
        self.clients[conn] = username

    def remove(self, conn):
        """
        Remove a client from the room, ignoring missing keys.

        How it works:
            dict.pop(conn, None) silently does nothing if conn is not in the
            dict, avoiding a KeyError on double-remove (e.g., leave + disconnect).

        Why it exists:
            Called from UserManager.leave_room which may itself be called both
            on a voluntary /leave and in the _handle_connection finally block.
            Using pop(..., None) makes it safe to call twice without checking.

        Args:
            conn — the client socket to remove.
        """
        self.clients.pop(conn, None)

    def get_username(self, conn) -> str | None:
        """
        Return the username for conn, or None if conn is not in this room.

        How it works:
            dict.get() with a default of None. The caller checks for None
            before using the returned name.

        Why it exists:
            UserManager needs to look up a sender's username when broadcasting
            IncomingMessage or IncomingFile, without raising an exception if the
            connection has already been removed.

        Args:
            conn — the client socket to look up.

        Returns:
            str — the username, or None.
        """
        return self.clients.get(conn)

    def others(self, exclude_conn=None) -> list:
        """
        Return a snapshot list of all connections except exclude_conn.

        How it works:
            List comprehension over self.clients — takes a snapshot of the
            current connections at the moment of the call. The snapshot is
            important: if we iterated over self.clients while sending (which
            happens outside the lock), a concurrent join or leave could modify
            the dict and raise RuntimeError.

        Why it exists:
            Used in two contexts:
            1. Broadcasting a message to everyone except the sender.
            2. Getting the list of existing members to notify when a new user
               joins (UserJoined broadcast).
            Passing exclude_conn=None returns all connections (used in leave_room
            to notify everyone when a member departs).

        Args:
            exclude_conn — socket to exclude (the sender), or None.

        Returns:
            list[socket] — snapshot of matching connections.
        """
        return [c for c in self.clients if c is not exclude_conn]

    def is_empty(self) -> bool:
        """
        Return True if no clients are currently in this room.

        How it works:
            len(self.clients) == 0.

        Why it exists:
            After a leave_room call, if the room is empty it is deleted from
            UserManager._rooms to prevent accumulating ghost rooms. This
            predicate makes that check readable.

        Returns:
            bool — True if the room has no members.
        """
        return len(self.clients) == 0


# ── UserManager ───────────────────────────────────────────────────────────────

class UserManager:
    """
    Manages rooms and users entirely in memory.

    The Database is only consulted for stats (increment and read).
    Rooms and users are never written to or read from the DB; they exist only
    for the lifetime of the server process.

    _lock serialises every join/leave/create so in-memory state stays consistent.
    Broadcasts happen outside the lock so a slow/broken client can't stall others.
    """

    def __init__(self, db: Database):
        """
        Initialise an empty manager backed by the given Database for stats.

        How it works:
            Sets up two dicts: _rooms maps room_code → Room, and _conn_room maps
            client socket → room_code. These two views allow O(1) lookup in both
            directions. A single threading.Lock() (_lock) protects both dicts.

        Why it exists:
            Having both _rooms and _conn_room avoids the need to iterate over all
            rooms to find which room a socket belongs to. This is the dominant
            lookup pattern (every send/receive needs to find the sender's room).

        Args:
            db — the Database instance used for stats increment/read only.
        """
        self.db = db
        self._rooms: dict[str, Room] = {}       # room_code -> Room
        self._conn_room: dict[object, str] = {} # conn -> room_code
        self._lock = threading.Lock()

    # ── Room lookup ───────────────────────────────────────────────────────────

    def get_room_of(self, conn) -> str | None:
        """
        Return the room_code of the room conn is in, or None.

        How it works:
            Simple dict lookup under the lock. The lock is needed because another
            thread might be executing leave_room concurrently and modifying
            _conn_room.

        Why it exists:
            Used as a precondition check in ChatMessageHandler: before sending a
            message or leaving a room, the handler verifies the client is in a
            room. Returning None means "not in a room" without raising an
            exception.

        Args:
            conn — the client socket to query.

        Returns:
            str — the room code, or None if not in any room.
        """
        with self._lock:
            return self._conn_room.get(conn)

    def get_username_of(self, conn) -> str | None:
        """
        Return the username of conn's current room, or None.

        How it works:
            Looks up the room_code from _conn_room, then looks up the Room from
            _rooms, then calls room.get_username(conn). Returns None if the
            connection is not in any room or the room no longer exists.
            All lookups happen under _lock for consistency.

        Why it exists:
            SendMessage and SendFile need to include the sender's username in the
            broadcast (IncomingMessage.from_username). The username is stored in
            the Room object, so we need to traverse conn → room_code → Room →
            username.

        Args:
            conn — the client socket.

        Returns:
            str — the username, or None.
        """
        with self._lock:
            rc = self._conn_room.get(conn)
            if rc is None:
                return None
            room = self._rooms.get(rc)
            return room.get_username(conn) if room else None

    # ── Create ────────────────────────────────────────────────────────────────

    def create_room(self, conn, username: str) -> tuple[str, list[str]]:
        """
        Create a new room, add the creator, return (room_code, member_list).
        Raises ValueError if conn is already in a room.

        How it works:
            Under the lock:
                1. Checks that conn is not already in _conn_room (can't be in
                   two rooms simultaneously).
                2. Generates a UUID room code. UUID4 is random and practically
                   collision-free — two simultaneous creates will get different
                   codes.
                3. Creates a Room, adds the creator, registers in both dicts.
                4. Takes a snapshot of the member list (just the creator at this
                   point) while still under the lock.

            Outside the lock:
                5. Increments total_rooms and total_users in the DB.

        Why the DB increment is outside the lock:
            DB I/O under the user lock would mean that a slow disk write could
            stall other threads trying to join or leave rooms. The lock guards
            in-memory state only; the DB has its own internal lock.

        Args:
            conn     — the client socket (the room creator).
            username — display name chosen by the client.

        Returns:
            (room_code: str, members: list[str]) — the UUID code and the
            initial member list (containing only the creator).

        Raises:
            ValueError — if conn is already in a room.
        """
        with self._lock:
            if conn in self._conn_room:
                raise ValueError('Already in a room — leave first')
            room_code = str(uuid.uuid4())
            room = Room(room_code)
            room.add(conn, username)
            self._rooms[room_code] = room
            self._conn_room[conn] = room_code
            members = list(room.clients.values())

        self.db.increment_stat('total_rooms', 'total_users')
        print(f"[CHAT] {username!r} created room {room_code}")
        return room_code, members

    # ── Join ──────────────────────────────────────────────────────────────────

    def join_room(self, conn, room_code: str, username: str) -> tuple[list[str], list]:
        """
        Add conn to an existing room.
        Returns (member_list, other_conns_to_notify).
        Raises ValueError on bad room code or duplicate username.

        How it works:
            Under the lock:
                1. Checks conn is not already in a room.
                2. Looks up the Room by room_code; raises ValueError if not found.
                3. Checks that the requested username is not already taken in this
                   room (compares against room.clients.values()).
                4. Takes a snapshot of `others` (existing members) before adding
                   the new user — this is the list that will receive UserJoined.
                5. Adds the new user and updates _conn_room.
                6. Takes a snapshot of the full member list (including the new
                   user) to return to the caller for the RoomJoined response.

            Outside the lock:
                7. Increments total_users in the DB.

        Why the snapshot of others is taken before adding:
            The caller (ChatMessageHandler._join_room) will send UserJoined to
            'others' and RoomJoined (with the full member list) to conn. If we
            added the new user before snapshotting others, the new user's socket
            would be in the list and would receive a spurious UserJoined about
            themselves.

        Args:
            conn      — the joining client's socket.
            room_code — the UUID of the room to join.
            username  — the display name the new user wants.

        Returns:
            (members: list[str], others: list[socket]) — full member list
            including the new user, and sockets of pre-existing members.

        Raises:
            ValueError — room does not exist, or username is taken.
        """
        with self._lock:
            if conn in self._conn_room:
                raise ValueError('Already in a room — leave first')
            room = self._rooms.get(room_code)
            if room is None:
                raise ValueError(f'Room {room_code!r} does not exist')
            if username in room.clients.values():
                raise ValueError(f'Username {username!r} is already taken in that room')
            others = room.others()
            room.add(conn, username)
            self._conn_room[conn] = room_code
            members = list(room.clients.values())

        self.db.increment_stat('total_users')
        print(f"[CHAT] {username!r} joined room {room_code}")
        return members, others

    # ── Leave ─────────────────────────────────────────────────────────────────

    def leave_room(self, conn, notify_self: bool = True):
        """
        Remove conn from its room. Broadcasts UserLeft to remaining members.
        notify_self=False skips writing to conn (used on disconnect).

        How it works:
            Under the lock:
                1. Looks up the room_code for conn; returns immediately if conn
                   is not in any room (idempotent).
                2. Removes conn from the Room and from _conn_room.
                3. Snapshots `others` (the remaining members).
                4. If the room is now empty, deletes it from _rooms to avoid
                   accumulating ghost rooms.

            Outside the lock:
                5. Sends UserLeft to all remaining members.
                6. If notify_self is True, sends RoomLeft to conn. This flag is
                   False during connection teardown (the socket is already dead,
                   so writing to it would raise an exception).

        Why the sends happen outside the lock:
            Sending to a slow or broken socket could block for a long time.
            Holding the lock during sends would block other threads from joining
            or leaving simultaneously. Taking a snapshot under the lock and then
            sending outside it is the standard pattern in this codebase.

        Args:
            conn        — the leaving client's socket.
            notify_self — if True, send RoomLeft to conn (voluntary leave);
                          if False, skip (called from the finally on disconnect).
        """
        with self._lock:
            room_code = self._conn_room.get(conn)
            if room_code is None:
                return
            room = self._rooms[room_code]
            username = room.get_username(conn)
            room.remove(conn)
            del self._conn_room[conn]
            others = room.others()
            if room.is_empty():
                del self._rooms[room_code]

        for other_conn in others:
            send_to(other_conn, 'UserLeft', {'username': username, 'room_code': room_code})

        if notify_self:
            send_to(conn, 'RoomLeft', {'room_code': room_code})

        print(f"[CHAT] {username!r} left room {room_code}")

    # ── Broadcast ─────────────────────────────────────────────────────────────

    def broadcast(self, conn, msg_type: str, data: dict):
        """
        Send a message to all room members except conn.

        How it works:
            Under the lock: looks up conn's room and calls room.others() for a
            snapshot of target sockets.
            Outside the lock: calls send_to() on each target. send_to() silently
            swallows exceptions, so a broken recipient does not abort the loop.

        Why it exists:
            Reused by SendMessage (IncomingMessage) and SendFile (IncomingFile).
            Because the snapshot is taken under the lock but the sends happen
            outside it, a slow client cannot delay faster ones, and a join/leave
            that happens during the broadcast does not see partially-updated
            state.

        Args:
            conn     — the sender's socket (excluded from the broadcast).
            msg_type — 'IncomingMessage' or 'IncomingFile'.
            data     — the message payload dict.
        """
        with self._lock:
            room_code = self._conn_room.get(conn)
            if room_code is None:
                return
            targets = self._rooms[room_code].others(exclude_conn=conn)

        for target in targets:
            send_to(target, msg_type, data)


# ── ChatMessageHandler ────────────────────────────────────────────────────────

class ChatMessageHandler:
    """
    Stateless dispatcher. Receives one message at a time from the connection
    loop and routes it to the appropriate UserManager operation.
    """

    def __init__(self, user_manager: UserManager):
        """
        Bind the handler to a UserManager instance.

        How it works:
            Stores a reference to user_manager as self.um. The handler is
            stateless — it holds no per-connection state itself; all state
            lives in UserManager and Room.

        Why it exists:
            Separating the dispatcher (which parses message types) from the
            business logic (UserManager) follows the single-responsibility
            principle and makes unit-testing each layer independently easier.

        Args:
            user_manager — the shared UserManager instance.
        """
        self.um = user_manager

    def handle(self, conn, msg_type: str, data: dict):
        """
        Route one incoming message to the correct private handler method.

        How it works:
            A series of if/elif branches maps the 'type' string to a private
            _method. Unknown types produce an Error response. This is intentionally
            a flat dispatcher (not a dict of callables) so the control flow is
            visible without indirection.

        Why it exists:
            Isolates the switch logic from the implementation of each handler.
            If a new message type is added to the protocol, only this method
            and one new _private_method need to change.

        Args:
            conn     — the client's socket (used to send responses).
            msg_type — the 'type' field from the parsed message.
            data     — the 'data' field from the parsed message.
        """
        if msg_type == 'CreateRoom':
            self._create_room(conn, data)
        elif msg_type == 'JoinRoom':
            self._join_room(conn, data)
        elif msg_type == 'LeaveRoom':
            self._leave_room(conn)
        elif msg_type == 'SendMessage':
            self._send_message(conn, data)
        elif msg_type == 'SendFile':
            self._send_file(conn, data)
        elif msg_type == 'GetStats':
            self._get_stats(conn)
        else:
            send_error(conn, f'Unknown message type: {msg_type!r}')

    def _create_room(self, conn, data: dict):
        """
        Validate and execute a CreateRoom request.

        How it works:
            Strips and validates the 'my_username' field — empty usernames are
            rejected immediately. Calls um.create_room() which does the actual
            state mutation and DB increment. On success sends RoomCreated with
            the new room_code and the initial member list (just the creator).
            On ValueError (already in a room) sends an Error.

        Why it exists:
            Validates at the boundary (input from the client) before touching
            any shared state. ValueError from create_room is a business-logic
            error (already in a room), not a bug, so it is converted to a
            client-facing Error message rather than letting it propagate.

        Args:
            conn — the client socket.
            data — {'my_username': str}.
        """
        username = data.get('my_username', '').strip()
        if not username:
            send_error(conn, 'my_username is required')
            return
        try:
            room_code, members = self.um.create_room(conn, username)
        except ValueError as e:
            send_error(conn, str(e))
            return
        send_to(conn, 'RoomCreated', {'room_code': room_code, 'users': members})

    def _join_room(self, conn, data: dict):
        """
        Validate and execute a JoinRoom request.

        How it works:
            Validates both 'room_code' and 'my_username' are non-empty. Calls
            um.join_room() which validates the room exists and the username is
            free. On success:
                - Sends UserJoined to all existing members.
                - Sends RoomJoined (with the full member list) to the joiner.
            On ValueError sends an Error to the joiner only.

        Why the notifications are split:
            Existing members need UserJoined (just the new username). The new
            member needs RoomJoined (the full member list, to populate their
            user list UI). These are two different message types with different
            data shapes.

        Args:
            conn — the joining client's socket.
            data — {'room_code': str, 'my_username': str}.
        """
        room_code = data.get('room_code', '').strip()
        username  = data.get('my_username', '').strip()
        if not room_code or not username:
            send_error(conn, 'room_code and my_username are required')
            return
        try:
            members, others = self.um.join_room(conn, room_code, username)
        except ValueError as e:
            send_error(conn, str(e))
            return
        # Notify existing members
        for other_conn in others:
            send_to(other_conn, 'UserJoined', {'username': username, 'room_code': room_code})
        send_to(conn, 'RoomJoined', {'room_code': room_code, 'users': members})

    def _leave_room(self, conn):
        """
        Execute a voluntary LeaveRoom request.

        How it works:
            Checks that conn is actually in a room first (otherwise sends an
            Error). Calls um.leave_room(notify_self=True) which sends UserLeft
            to the other members and RoomLeft to conn.

        Why it exists:
            Voluntary leave (client sent /leave) differs from involuntary
            disconnect (socket closed): voluntary leave notifies conn with
            RoomLeft so the client UI can react cleanly. Involuntary disconnect
            is handled in _handle_connection's finally block with
            notify_self=False.

        Args:
            conn — the leaving client's socket.
        """
        if self.um.get_room_of(conn) is None:
            send_error(conn, 'Not in a room')
            return
        self.um.leave_room(conn, notify_self=True)

    def _send_message(self, conn, data: dict):
        """
        Broadcast a chat message from conn to all other room members.

        How it works:
            Checks that conn is in a room. Looks up the sender's username.
            Broadcasts IncomingMessage with from_username and message to all
            other room members. Increments the total_messages counter. Sends
            Ack back to the sender.

        Why the Ack is required:
            In Tor mode the exit node's relay handler blocks until it receives
            a response from the server after every send. Without an Ack, the
            exit node would time out (select 5s) and the circuit would appear
            to have failed. The Ack is the minimal response that unblocks it.

        Args:
            conn — the sender's socket.
            data — {'message': str}.
        """
        if self.um.get_room_of(conn) is None:
            send_error(conn, 'Not in a room')
            return
        username = self.um.get_username_of(conn)
        message  = data.get('message', '')
        self.um.broadcast(conn, 'IncomingMessage', {'from_username': username, 'message': message})
        self.um.db.increment_stat('total_messages')
        send_to(conn, 'Ack', {})

    def _send_file(self, conn, data: dict):
        """
        Broadcast a file transfer from conn to all other room members.

        How it works:
            Checks room membership. Broadcasts IncomingFile with from_username,
            filename, and filedata (base64-encoded bytes as a string) to all
            other members. Increments total_files. Sends Ack.

        Why base64:
            Binary file data cannot be embedded directly in JSON (which is
            UTF-8 text). Base64 encoding converts arbitrary bytes to ASCII
            characters that are safe in JSON string values. The receiving client
            base64-decodes before writing the file to disk.

        Why the Ack is required:
            Same reason as _send_message: the exit node blocks waiting for a
            server response after every relay.

        Args:
            conn — the sender's socket.
            data — {'filename': str, 'filedata': str (base64)}.
        """
        if self.um.get_room_of(conn) is None:
            send_error(conn, 'Not in a room')
            return
        username = self.um.get_username_of(conn)
        self.um.broadcast(conn, 'IncomingFile', {
            'from_username': username,
            'filename':      data.get('filename', ''),
            'filedata':      data.get('filedata', ''),
        })
        self.um.db.increment_stat('total_files')
        send_to(conn, 'Ack', {})

    def _get_stats(self, conn):
        """
        Read cumulative stats from the DB and send them to conn.

        How it works:
            Calls db.get_stats() — a live DB read, not a cached value — and
            wraps the result dict in a Stats message. No room membership check
            is required: any connected client can query stats at any time.

        Why it reads from DB (not memory):
            Stats are the only information that must survive server restarts.
            Reading directly from the DB ensures the response reflects the true
            accumulated totals, including any that accumulated before the current
            server process started.

        Args:
            conn — the requesting client's socket.
        """
        # Stats are read directly from the DB — no in-memory cache
        send_to(conn, 'Stats', self.um.db.get_stats())


# ── ChatServer ────────────────────────────────────────────────────────────────

class ChatServer:
    def __init__(self, host: str, port: int, db_path: str):
        """
        Initialise the chat server with a Database and a ChatMessageHandler.

        How it works:
            Creates a Database at db_path (opens or creates the SQLite file).
            Wraps it in a UserManager, then wraps that in a ChatMessageHandler.
            Stores host and port for later use by start().

        Why it exists:
            Separates construction (wiring together the components) from the
            accept loop (start()). This makes it easy to instantiate the server
            in tests without immediately binding a port.

        Args:
            host    — IP address to bind (e.g. '0.0.0.0' or '127.0.0.1').
            port    — TCP port to listen on.
            db_path — filesystem path for the SQLite stats database.
        """
        self.host = host
        self.port = port
        db = Database(db_path)
        um = UserManager(db)
        self.handler = ChatMessageHandler(um)

    def start(self):
        """
        Bind the server socket, enter the accept loop, and dispatch threads.

        How it works:
            Creates a TCP socket with SO_REUSEADDR so rapid restarts don't have
            to wait for TIME_WAIT to expire. Sets a 1-second accept() timeout
            so the outer while-loop can check for KeyboardInterrupt.
            For each accepted connection, spawns a daemon thread running
            _handle_connection. Daemon threads are killed automatically when the
            main thread exits, so no explicit thread cleanup is needed.
            On KeyboardInterrupt, breaks the loop and closes the server socket.

        Why it exists:
            This is the long-running server entry point. It needs to be in a
            separate method (not __init__) so the server can be fully initialised
            before binding starts, and so tests can create a ChatServer without
            accidentally starting a live server.
        """
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.host, self.port))
        srv.listen(64)
        srv.settimeout(1.0)
        print(f"[CHAT] Listening on {self.host}:{self.port}")

        try:
            while True:
                try:
                    conn, addr = srv.accept()
                except socket.timeout:
                    continue
                t = threading.Thread(target=self._handle_connection, args=(conn, addr), daemon=True)
                t.start()
        except KeyboardInterrupt:
            print("\n[CHAT] Shutting down...")
        finally:
            srv.close()

    def _handle_connection(self, conn, addr):
        """
        Manage one client connection for its full lifetime.

        How it works:
            Enters a read loop: calls recv_msg() to get the next framed message,
            JSON-parses it, and calls self.handler.handle() with the message type
            and data. The loop continues until recv_msg() returns None (clean
            disconnect) or an exception is raised (network error).

            JSON decode errors are caught per-message and responded to with an
            Error message, rather than killing the entire connection — a
            malformed packet should not disconnect the client.

            The finally block calls leave_room(notify_self=False) to clean up
            any room membership when the socket closes, and closes the socket.
            notify_self=False is used because the socket is already dead (or
            about to be) — attempting to write to it would raise an exception.

        Why it exists:
            In a multi-threaded server, each connection must have its own thread
            blocking on recv_msg(), otherwise a slow client would stall all
            others. Daemon threads ensure they're killed on process exit without
            needing explicit join().

        Args:
            conn — the accepted client socket.
            addr — (host, port) of the client (only used for logging).
        """
        print(f"[CHAT] Connection from {addr}")
        try:
            while True:
                raw = recv_msg(conn)
                if raw is None:
                    break
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    send_error(conn, 'Invalid JSON')
                    continue
                self.handler.handle(conn, msg.get('type', ''), msg.get('data', {}))
        except Exception as e:
            print(f"[CHAT] Error from {addr}: {e}")
        finally:
            self.handler.um.leave_room(conn, notify_self=False)
            conn.close()
            print(f"[CHAT] Disconnected {addr}")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    """
    Parse CLI arguments and start the chat server.

    How it works:
        Accepts --host (default '0.0.0.0'), --port (default 8001), and --db
        (default Servers/chat_stats.db relative to this file's directory).
        Creates a ChatServer and calls start(), which runs until Ctrl+C.

    Why it exists:
        Separates argument parsing from server construction so ChatServer can
        be imported and used programmatically without invoking argparse.
    """
    parser = argparse.ArgumentParser(description='Onion Chat Server')
    parser.add_argument('--host', default='0.0.0.0', help='Bind address')
    parser.add_argument('--port', type=int, default=8001, help='Listen port')
    parser.add_argument(
        '--db',
        default=os.path.join(os.path.dirname(__file__), 'chat_stats.db'),
        help='SQLite database path (default: Servers/chat_stats.db)',
    )
    args = parser.parse_args()
    ChatServer(args.host, args.port, args.db).start()


if __name__ == '__main__':
    main()
