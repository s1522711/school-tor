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
    UserManager         — all room/user logic, purely in-memory (no DB reads/writes)
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
    if isinstance(data, dict):
        data = json.dumps(data).encode()
    elif isinstance(data, str):
        data = data.encode()
    sock.sendall(len(data).to_bytes(4, 'big') + data)


def send_to(sock, msg_type: str, data: dict):
    try:
        send_msg(sock, {'type': msg_type, 'data': data})
    except Exception:
        pass


def send_error(sock, message: str):
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
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._init_schema()
        print(f"[DB] Opened {path}")

    def _init_schema(self):
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
        with self._lock:
            for key in keys:
                self._conn.execute(
                    "UPDATE stats SET value = value + 1 WHERE key = ?", (key,)
                )
            self._conn.commit()

    def get_stats(self) -> dict:
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
        self.room_code = room_code
        self.clients: dict = {}  # conn -> username

    def add(self, conn, username: str):
        self.clients[conn] = username

    def remove(self, conn):
        self.clients.pop(conn, None)

    def get_username(self, conn) -> str | None:
        return self.clients.get(conn)

    def others(self, exclude_conn=None) -> list:
        """Snapshot of all connections except exclude_conn."""
        return [c for c in self.clients if c is not exclude_conn]

    def is_empty(self) -> bool:
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
        self.db = db
        self._rooms: dict[str, Room] = {}       # room_code -> Room
        self._conn_room: dict[object, str] = {} # conn -> room_code
        self._lock = threading.Lock()

    # ── Room lookup ───────────────────────────────────────────────────────────

    def get_room_of(self, conn) -> str | None:
        with self._lock:
            return self._conn_room.get(conn)

    def get_username_of(self, conn) -> str | None:
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
        """Send to all room members except conn."""
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
        self.um = user_manager

    def handle(self, conn, msg_type: str, data: dict):
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
        if self.um.get_room_of(conn) is None:
            send_error(conn, 'Not in a room')
            return
        self.um.leave_room(conn, notify_self=True)

    def _send_message(self, conn, data: dict):
        if self.um.get_room_of(conn) is None:
            send_error(conn, 'Not in a room')
            return
        username = self.um.get_username_of(conn)
        message  = data.get('message', '')
        self.um.broadcast(conn, 'IncomingMessage', {'from_username': username, 'message': message})
        self.um.db.increment_stat('total_messages')
        send_to(conn, 'Ack', {})

    def _send_file(self, conn, data: dict):
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
        # Stats are read directly from the DB — no in-memory cache
        send_to(conn, 'Stats', self.um.db.get_stats())


# ── ChatServer ────────────────────────────────────────────────────────────────

class ChatServer:
    def __init__(self, host: str, port: int, db_path: str):
        self.host = host
        self.port = port
        db = Database(db_path)
        um = UserManager(db)
        self.handler = ChatMessageHandler(um)

    def start(self):
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
