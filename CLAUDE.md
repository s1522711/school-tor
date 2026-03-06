# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## Project overview

A simplified Tor-like onion routing network implemented entirely in Python using raw TCP sockets. Messages travel through three relay nodes (entry → middle → exit) before reaching a destination server. Each hop has its own AES-128 session key; messages are triple-encrypted on the way out and triple-decrypted on the way back. The exit→server leg carries plaintext (no AES).

On top of the core network sits a multi-room chat server, a CLI chat client, and a full CustomTkinter GUI client. Both clients can connect directly or through the onion circuit.

---

## Repository layout

```
school-tor/
├── Servers/
│   ├── directory_server.py   # Node registry; nodes register here, clients query here
│   ├── node.py               # All three relay roles (entry/middle/exit) in one file
│   ├── client.py             # Echo client; builds circuit, sends messages to server.py
│   ├── server.py             # Plaintext echo destination server
│   ├── chat_server.py        # Multi-room chat server (OOP, SQLite stats)
│   └── chat_client.py        # CLI chat client (direct or --tor mode)
├── client/
│   ├── main.py               # GUI entry point; App class; screen switching
│   ├── network.py            # Wire helpers, TorSocket, circuit building, Connection
│   ├── home_screen.py        # Home screen: connection settings, stats, create/join
│   └── chat_screen.py        # Chat room screen: messages, user list, file send/receive
├── start.bat                 # Windows launcher
├── start.sh                  # Linux/macOS launcher
├── protocol.md               # Chat wire protocol specification
├── requirements.txt          # pycryptodome, customtkinter
└── venv/                     # Python virtual environment
```

---

## Setup and running

```bash
# Activate venv and install deps
venv\Scripts\activate          # Windows
source venv/bin/activate       # Unix
pip install -r requirements.txt

# Launch everything (one of each node type by default)
start.bat                      # Windows
./start.sh                     # Linux/macOS

# Options (combinable, any order)
start.bat --nodes 3            # 3 entry, 3 middle, 3 exit nodes
start.bat --debug              # hex-dump relay messages at each hop
start.bat --nodes 2 --debug

# Manual launch order (servers first, then clients)
python Servers/directory_server.py
python Servers/chat_server.py --port 8001
python Servers/server.py --port 9000
python Servers/node.py --type entry  --port 9001 [--debug]
python Servers/node.py --type middle --port 9002 [--debug]
python Servers/node.py --type exit   --port 9003 [--debug]
python Servers/client.py --dest-port 9000          # echo client (no chat UI)
python Servers/chat_client.py                      # CLI direct chat
python Servers/chat_client.py --tor                # CLI chat over Tor circuit
python client/main.py                              # GUI client
```

Port layout when using `--nodes N`:
- Directory server: 8000
- Chat server:      8001
- Destination echo: 9000
- Entry nodes:  9001 … 9000+N
- Middle nodes: 9101 … 9100+N
- Exit nodes:   9201 … 9200+N

The start scripts launch everything in order with 1-second gaps, then open `Tor-Client` (echo) and `Chat-Client-Tor` (CLI chat over Tor) as separate terminal windows. Press any key (bat) or Ctrl+C (sh) to stop all components.

---

## Wire protocol (shared by all components)

Every TCP message in the system uses the same length-prefixed framing:

```
┌─────────────────────┬──────────────────────────────┐
│  4-byte big-endian  │  payload (JSON or raw bytes)  │
│  length header      │                               │
└─────────────────────┴──────────────────────────────┘
```

`recv_msg` / `send_msg` are duplicated in every file (no shared module for servers; `client/network.py` is the shared module for the GUI). `send_msg` accepts `dict`, `str`, or `bytes`; dicts are JSON-serialised before framing. `recv_msg` returns raw bytes (no JSON parsing) or `None` on disconnect.

Control messages are JSON objects with a `"type"` field. The chat protocol wraps this further:
```json
{"type": "MessageType", "data": {...}}
```

---

## Component deep-dives

---

### `Servers/directory_server.py`

**Role:** Central node registry. Nodes register on startup; clients query once per session to build a circuit.

**State:** Module-level list `nodes` (protected by `nodes_lock`). Each entry:
```python
{'node_type': 'entry'|'middle'|'exit', 'host': str, 'port': int, 'public_key': str}
```

**Protocol — one request/response per connection:**

| Incoming `type` | Fields | Response |
|---|---|---|
| `REGISTER` | `node_type`, `host`, `port`, `public_key` (PEM) | `{"status": "ok"}` |
| `GET_NODES` | — | `{"nodes": [...]}` |

`REGISTER` upserts by `host:port` (allows node restarts without stale entries).

**Threading:** One daemon thread per connection; each thread handles exactly one request then closes.

**Soft shutdown:** `server.settimeout(1.0)` on the accept socket. `KeyboardInterrupt` is caught; `finally` closes the socket.

---

### `Servers/node.py`

**Role:** Relay node. A single file serves all three roles controlled by `--type entry|middle|exit`.

**Module-level globals** (set in `main()` before threads start):
- `rsa_key` — RSA-2048 private key (generated fresh each launch)
- `node_type` — `'entry'`, `'middle'`, or `'exit'`
- `debug` — bool; enables hex dumps via `dbg()`
- `circuits` — dict keyed by circuit_id (UUID str):
  ```python
  {
      'key':       bytes,          # AES-128 relay key for this hop
      'next_sock': socket | None,  # persistent TCP socket to next hop (entry/middle only)
      'dest_sock': socket | None,  # persistent TCP socket to dest server (exit only)
      'dest':      (host, port),   # destination address (exit only, for reference)
      'is_exit':   bool,
  }
  ```
- `circuits_lock` — `threading.Lock` protecting `circuits`

**Threading:** One daemon thread per incoming connection via `handle_connection`. The thread owns the connection for its full lifetime and processes all circuits established over it.

**Soft shutdown:** `server_sock.settimeout(1.0)`. `KeyboardInterrupt` caught; `finally` closes all `next_sock` and `dest_sock` from active circuits, then closes `server_sock`.

#### Circuit setup (`CIRCUIT_SETUP`)

Each node's payload uses **hybrid encryption**:
```json
{
    "encrypted_key":  "<base64 of RSA-OAEP(node_pub, setup_aes_key)>",
    "encrypted_data": "<base64 of AES-CBC(setup_aes_key, json_inner)>"
}
```
- `setup_aes_key`: 16-byte ephemeral key, RSA-OAEP encrypted so only the target node can recover it.
- `json_inner` contains:
  - All nodes: `"key"` — base64 of the 16-byte AES relay key (K1/K2/K3)
  - Entry/middle: `"next_host"`, `"next_port"`, `"forward_payload"` (nested payload for next hop)
  - Exit only: `"dest_host"`, `"dest_port"`

**Why hybrid?** RSA-OAEP (2048-bit) can encrypt at most ~214 bytes. Routing data with nested payloads far exceeds this. Only the 16-byte ephemeral key goes through RSA; everything else goes through AES.

**Entry/middle setup flow (`handle_circuit_setup`):**
1. Decrypt payload → relay key + next hop address + forward payload.
2. Open persistent TCP socket to next hop (`next_sock`).
3. Forward `CIRCUIT_SETUP` with the inner payload to next hop.
4. Block for `{"status": "ok"}` from next hop.
5. Store circuit in `circuits`; add to `local_circuits`.
6. Return `{"status": "ok"}` to previous hop.

**Exit setup flow:**
1. Decrypt payload → relay key + dest address.
2. Open **persistent** TCP socket to the destination server (`dest_sock`). This connection stays open for the circuit's lifetime so the server can push messages back.
3. Store circuit in `circuits`; add to `local_circuits`.
4. Return `{"status": "ok"}`.

#### Relay (`RELAY` / `RELAY_RESPONSE`)

**Forward path — each node decrypts one layer:**
```
client sends:    AES_K1( AES_K2( AES_K3(message) ) )
entry receives:  AES_K1(...)  → strips K1 → forwards AES_K2(AES_K3(message))
middle receives: AES_K2(...)  → strips K2 → forwards AES_K3(message)
exit receives:   AES_K3(...)  → strips K3 → sends raw message to dest_sock
```

**Backward path — each node re-encrypts one layer:**
```
dest sends raw response R
exit   sends: AES_K3(R)
middle sends: AES_K2( AES_K3(R) )
entry  sends: AES_K1( AES_K2( AES_K3(R) ) )
client decrypts: K1 → K2 → K3 → R
```

**Entry/middle relay handler (`handle_relay`):**
1. Base64-decode `data`, AES-decrypt with own key.
2. Forward stripped ciphertext to `next_sock` as a RELAY.
3. Block for RELAY_RESPONSE from next hop.
4. AES-encrypt the response data with own key.
5. Return RELAY_RESPONSE to previous hop.

**Exit relay handler:**

The exit node distinguishes two relay modes based on whether the decrypted payload is empty:

- **Real relay** (non-empty payload): sends to `dest_sock` via `send_msg`, then waits up to **5 seconds** for a response using `select.select`.
- **Poll relay** (empty payload): skips the send entirely, waits up to **0.5 seconds** for any server-pushed data using `select.select`.

In both cases, if data is available it reads one framed message and returns it AES-encrypted. If nothing arrives within the timeout, returns an AES-encrypted empty bytes (signals "no message pending" to the client).

This poll mechanism is what enables the Tor-mode chat client to receive push notifications from the chat server (other users' messages, join/leave events).

#### Connection teardown

`handle_connection` maintains a `local_circuits` set. When its loop exits (client disconnects or socket error), the `finally` block:
1. Closes `conn`.
2. For each circuit in `local_circuits`: pops from `circuits`, closes `next_sock` (if present), closes `dest_sock` (if present).

Closing `next_sock` causes the next node's `recv_msg` to return `None`, triggering its own `finally` block. The teardown cascades entry → middle → exit automatically.

---

### `Servers/client.py`

**Role:** Interactive echo client. Builds a 3-hop circuit to `server.py` and provides a REPL.

**Startup sequence:**
1. `GET_NODES` from directory server.
2. `pick_nodes` — groups by type, `random.choice` from each group (supports N nodes per type).
3. `build_circuit` — constructs nested payloads exit→middle→entry, connects to entry, sends `CIRCUIT_SETUP`, waits for `{"status": "ok"}`.
4. REPL: `input("you > ")` → `send_relay` → print response.

**`send_relay`:**
```python
data = AES_K3(message)   # innermost
data = AES_K2(data)
data = AES_K1(data)      # outermost
send RELAY → wait for RELAY_RESPONSE
data = AES_K1_decrypt(data)
data = AES_K2_decrypt(data)
data = AES_K3_decrypt(data)  # → server response
```

The circuit (K1, K2, K3, entry_sock, circuit_id) is held for the process lifetime; every message reuses the same circuit.

**Soft shutdown:** `KeyboardInterrupt` caught in REPL loop; `finally` closes `entry_sock`.

---

### `Servers/server.py`

**Role:** Plaintext echo destination at the end of the chain.

Receives unencrypted messages from the exit node. Each connection handles exactly one request: reads a message, prints it, echoes `"Echo: <text>"` back, closes. Uses the same length-prefixed framing; no AES.

**Soft shutdown:** `srv.settimeout(1.0)`. `KeyboardInterrupt` caught; `finally` closes socket.

---

### `Servers/chat_server.py`

**Role:** Multi-room chat server. Clients connect either directly or through their own Tor circuit (the server only ever sees the exit node's IP, not the real client's).

**Architecture — four classes:**

```
ChatServer
  └── ChatMessageHandler  (stateless dispatcher)
        └── UserManager   (all room/user logic, purely in-memory)
              └── Database (stats persistence only)
```

#### `Database`

Stores only cumulative stats that survive restarts. Rooms and users are **never** written to or read from the DB.

**Schema — single table:**
```sql
CREATE TABLE IF NOT EXISTS stats (
    key   TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
);
```
Keys seeded on startup: `total_messages`, `total_files`, `total_users`, `total_rooms`.

**Methods:**
- `increment_stat(*keys)` — atomically increments one or more counters in the DB.
- `get_stats() -> dict` — reads all counters from DB and returns them as a plain dict. The dict is transient (not cached); it exists only long enough to be sent to the requesting client.

**Thread safety:** Single `_lock` (threading.Lock) serialises all SQL operations. `check_same_thread=False` lets worker threads share the connection.

**Stats are read-from-DB, not from memory.** There is no in-memory stats cache. `GetStats` causes a DB read every time.

#### `Room`

In-memory socket store for one active room.

```python
clients: dict[socket, str]  # conn -> username
```

Methods: `add(conn, username)`, `remove(conn)`, `get_username(conn)`, `others(exclude_conn)` (snapshot list), `is_empty()`.

`Room` objects are created and destroyed dynamically. They hold no DB references.

#### `UserManager`

All room and user state lives here, purely in memory:
```python
_rooms:     dict[str, Room]    # room_code -> Room
_conn_room: dict[conn, str]    # conn -> room_code
_lock:      threading.Lock
```

**`create_room(conn, username)`:**
1. Under `_lock`: generate UUID room code, create `Room`, add user, register in `_conn_room`.
2. Outside `_lock`: `db.increment_stat('total_rooms', 'total_users')`.
3. Returns `(room_code, members_list)`.

**`join_room(conn, room_code, username)`:**
1. Under `_lock`: validate room exists and username is free (both checked against in-memory `_rooms`), snapshot `others`, add user.
2. Outside `_lock`: `db.increment_stat('total_users')`.
3. Returns `(members_list, others_conns)`.

**`leave_room(conn, notify_self)`:**
1. Under `_lock`: remove user, snapshot `others`, delete `Room` if empty.
2. Outside `_lock`: broadcast `UserLeft` to others; optionally send `RoomLeft` to leaving conn.

**`broadcast(conn, msg_type, data)`:** Snapshots targets under `_lock`, sends outside lock. Prevents a slow receiver from blocking others.

**Lock discipline:** `UserManager._lock` is the only lock in the system for room/user state. DB's internal `_lock` is separate and only acquired for SQL I/O. The two locks are never nested.

#### `ChatMessageHandler`

Stateless dispatcher. Routes one message at a time to `UserManager`:

| Client sends | Handler does | Sends back |
|---|---|---|
| `CreateRoom` | `um.create_room` | `RoomCreated` `{room_code, users}` |
| `JoinRoom` | `um.join_room` | `RoomJoined` `{room_code, users}` + `UserJoined` broadcast |
| `LeaveRoom` | `um.leave_room(notify_self=True)` | `RoomLeft` + `UserLeft` broadcast |
| `SendMessage` | `um.broadcast` + `db.increment_stat` | `Ack` (required for Tor mode) |
| `SendFile` | `um.broadcast` + `db.increment_stat` | `Ack` (required for Tor mode) |
| `GetStats` | `db.get_stats()` → transient dict | `Stats` |

**Why `Ack` for SendMessage/SendFile?** The exit node's relay handler uses `select()` to wait for a response after every send. Without `Ack`, the exit node would block indefinitely waiting for a response that never comes.

#### `ChatServer`

Accept loop with `srv.settimeout(1.0)` for `KeyboardInterrupt` responsiveness. One daemon thread per connection via `_handle_connection`. `finally` calls `um.leave_room(conn, notify_self=False)` on disconnect (socket already dead, so don't write to it).

**Port:** 8001 (default). Set with `--port`.

**DB path:** `Servers/chat_stats.db` (default). Set with `--db`.

---

### `Servers/chat_client.py`

**Role:** CLI chat client. Can connect directly or through a 3-hop onion circuit (`--tor`).

**Usage:**
```bash
python Servers/chat_client.py                        # direct
python Servers/chat_client.py --tor                  # Tor mode
python Servers/chat_client.py --tor --host 127.0.0.1 --port 8001
```

**Flags:**
- `--host` / `--port` — chat server address (default `127.0.0.1:8001`)
- `--tor` — route through onion circuit
- `--dir-host` / `--dir-port` — directory server (Tor mode only, default `127.0.0.1:8000`)

#### Direct mode

1. Connect TCP socket to chat server.
2. `setup()` — synchronous create/join room (before receiver thread starts).
3. Start background `receiver` thread — calls `recv_msg(sock)` in a loop, prints incoming messages.
4. `input_loop` — reads user commands, sends via `send_to(sock, ...)`.

**Commands:** `/leave`, `/stats`, `/file <path>`, `/quit`, or plain text to `SendMessage`.

#### Tor mode

**Circuit building:**
1. `get_nodes(dir_host, dir_port)` → list of registered nodes.
2. `pick_nodes(nodes)` → one of each type via `random.choice`.
3. `build_circuit(entry, middle, exit, host, port)` → builds nested payloads exit→middle→entry, connects to entry, sends `CIRCUIT_SETUP`, waits for `{"status": "ok"}`.
4. Returns `(circuit_id, K1, K2, K3, entry_sock)`.

**`TorSocket` — drop-in socket replacement:**

`TorSocket` implements `sendall(data)` and `recv(n)` so it can be passed directly to `send_msg` / `recv_msg` without any changes to `setup()` or the rest of the code.

```python
class TorSocket:
    _cid, _K1, _K2, _K3  # circuit identity and keys
    _entry                # real socket to entry node
    _buf                  # internal byte buffer for recv()
    _lock                 # threading.Lock — serialises sendall and poll
```

**`sendall(data)`:**
- Strips the 4-byte length frame that `send_msg` added (the exit node's `send_msg` re-adds it when delivering to the chat server).
- Acquires `_lock`.
- Triple-encrypts the payload: K3 (innermost) → K2 → K1 (outermost).
- Sends a `RELAY` message to the entry node.
- Reads the `RELAY_RESPONSE`, triple-decrypts: K1 → K2 → K3.
- If the decrypted response is non-empty, re-adds a 4-byte length frame and appends to `_buf`.

**`recv(n)`:** Returns `n` bytes from `_buf` (called by `recv_msg(tor_sock)` to read the buffered response).

**Why strip/re-add the frame?** `send_msg` frames data as `[4B len][json]`. If we sent the full frame through the relay, the exit node's `send_msg` would add *another* 4-byte prefix, causing double-framing. By stripping before encrypting and re-adding after decrypting, the framing is transparent to all callers.

**`poll() -> dict | None`:**
- Acquires `_lock`.
- Encrypts **empty bytes** (b'') in three layers and sends as a RELAY.
- The exit node sees an empty decrypted payload, skips the `send_msg` to dest, and uses `select(0.5s)` to check for any server-pushed data.
- Decrypts the response: if empty → returns `None`; if non-empty → parses and returns the JSON message dict.

**`tor_receiver` thread:**
- Calls `tor_sock.poll()` in a tight loop.
- Each call naturally blocks for up to 0.5 s (exit node's `select` timeout), so no `sleep()` needed.
- Prints received push messages (IncomingMessage, UserJoined, UserLeft) exactly like the direct-mode `receiver` thread.
- `_lock` inside `TorSocket` serialises poll and sendall, preventing race conditions between this thread and the main thread.

**`tor_input_loop`:**
- Sends commands exactly as direct mode via `send_to(tor_sock, ...)`.
- After every send, calls `recv_msg(tor_sock)` to consume the direct response (Ack/Stats/RoomLeft) from `_buf`.
- Discards `Ack` silently; displays everything else via `print_incoming`.

**Full Tor mode flow:**
1. Build circuit.
2. Create `TorSocket`.
3. `setup(tor_sock)` — synchronous create/join (sendall fills buf, recv_msg drains it).
4. Start `tor_receiver` daemon thread.
5. Run `tor_input_loop`.

---

## GUI client (`client/`)

The GUI client is a CustomTkinter desktop application. It connects to the same chat server as the CLI client and supports both direct and Tor mode. All four files live in `client/` and share imports via `sys.path.insert(0, os.path.dirname(__file__))`.

### Architecture overview

```
App (main.py)
  │  owns Connection, switches between screens
  ├── HomeScreen (home_screen.py)
  │     connection settings, live stats panel, create/join room
  └── ChatScreen (chat_screen.py)
        message history, user list, send bar, file attach/save
        │
        └── Connection (network.py)
              wraps raw socket or TorSocket
              persistent — survives screen transitions
```

**Key design principle: one persistent connection per session.** The same socket is used for stats fetches, room setup, and in-room messaging. The connection is never closed and re-opened just to fetch stats. When the user leaves a room, the receiver thread stops but the socket stays open; the home screen immediately reuses it for stats.

---

### `client/network.py`

Shared network layer for the GUI client. Contains all networking code — wire helpers, AES/RSA helpers, Tor circuit building, `TorSocket`, and `Connection`.

#### Wire helpers

`recv_msg(sock)`, `send_msg(sock, data)`, `send_to(sock, msg_type, data)` — identical to the server-side helpers. Duplicated here to keep `client/` self-contained.

#### AES helpers

`aes_encrypt(key, plaintext) -> bytes` — AES-128-CBC, random IV prepended.
`aes_decrypt(key, ciphertext) -> bytes` — strips IV from first 16 bytes.

#### Tor circuit functions

`get_nodes(dir_host, dir_port)` — queries directory server, returns node list.

`pick_nodes(nodes)` — groups by type, selects one entry/middle/exit via `random.choice`. Raises `RuntimeError` if any type is missing.

`make_setup_payload(pub_pem, inner)` — hybrid-encrypts `inner` dict for a single node:
1. Generate 16-byte `setup_key`.
2. RSA-OAEP encrypt `setup_key` with node's public key → `encrypted_key`.
3. AES-CBC encrypt JSON of `inner` with `setup_key` → `encrypted_data`.
4. Returns `{encrypted_key: b64, encrypted_data: b64}`.

`build_circuit(entry, middle, exit_node, dest_host, dest_port)` — constructs nested payloads (exit → middle → entry), connects TCP to entry node, sends `CIRCUIT_SETUP`, waits for `{"status": "ok"}`. Returns `(circuit_id, K1, K2, K3, entry_sock)`.

#### `TorSocket`

Drop-in socket replacement routing traffic through a 3-hop onion circuit. Identical to the `TorSocket` in `chat_client.py`. Key methods:

- `sendall(data)` — strips 4B frame, triple-encrypts K3→K2→K1, sends RELAY, reads RELAY_RESPONSE, triple-decrypts K1→K2→K3, buffers non-empty response in `_buf`.
- `recv(n)` — returns `n` bytes from `_buf`.
- `poll()` — sends empty relay (b''), exit node does `select(0.5s)` for pushed data, returns parsed JSON dict or `None`.
- `close()` — closes the entry socket.

All methods serialise on `_lock` so `sendall` (main thread) and `poll` (receiver thread) never race.

#### `Connection`

Wraps either a raw socket (direct mode) or a `TorSocket` (Tor mode). Provides a uniform API to the rest of the GUI:

```python
conn.send_to(msg_type, data)    # send one chat protocol message
conn.recv_one()                 # synchronous receive — use only when no receiver running
conn.start_receiver(on_message, on_disconnect)
conn.stop_receiver()            # stops receiver, socket stays open
conn.close()                    # stop_receiver() + close socket
conn.is_tor                     # bool property
```

**Persistent receiver threads:**

- `start_receiver` creates a fresh `threading.Event` (`_recv_stop`) and a daemon thread each call.
- `stop_receiver` sets `_recv_stop` and joins the thread (max 2 s). The socket is **not** closed.
- Direct receiver uses `select.select([sock], [], [], 0.5)` before each `recv_msg` call. This means `stop_receiver` takes effect within 0.5 s without needing to close the socket.
- Tor receiver loops on `poll()`, which already blocks for 0.5 s per call; `_recv_stop` is checked between polls.
- Both receivers filter out `Ack` messages — they are never delivered to `on_message`.

**Why stop without closing?** The home screen reuses the connection for stats after the user leaves a room. If `stop_receiver` closed the socket, a new connection (and in Tor mode, a new circuit) would be required every time the user navigates back to the home screen.

#### Convenience functions

`connect_direct(host, port) -> Connection` — creates TCP socket, wraps in `Connection(is_tor=False)`.

`connect_tor(dir_host, dir_port, dest_host, dest_port) -> Connection` — queries directory, picks nodes, builds circuit, wraps in `Connection(is_tor=True)`.

---

### `client/main.py`

**Role:** Application entry point. Creates the `ctk.CTk` window, holds the `Connection`, and switches between the two screens.

```python
class App(ctk.CTk):
    _conn:  Connection | None   # the one persistent connection for this session
    _frame: CTkFrame | None     # the currently displayed screen

    def show_home(conn=None)    # destroy current frame, show HomeScreen
    def show_chat(conn, username, room_code, members)
    def _set_conn(conn)         # called by HomeScreen when it establishes a new connection
    def _on_close()             # WM_DELETE_WINDOW handler — closes conn, destroys window
```

`show_home(conn=None)` accepts the connection passed back from `ChatScreen` on leave. If `conn` is provided, it updates `_conn` so the home screen can reuse it. If `None`, the home screen starts disconnected.

Screen transitions:
- Home → Chat: `HomeScreen` calls `on_enter_room(conn, username, room_code, members)` → `App.show_chat`.
- Chat → Home: `ChatScreen` calls `on_leave(conn)` → `App.show_home(conn=conn)`.

Theme: `ctk.set_appearance_mode("dark")`, `ctk.set_default_color_theme("blue")`. Window minimum size: 800×520.

---

### `client/home_screen.py`

**Role:** Home screen. Displays connection settings, live server statistics, and create/join room forms.

#### Layout

```
┌──────────────────────────────────────────────────────────────────────┐
│  Onion Chat  Server:[host]  Port:[port]  [x]Tor  [Connect]  ● status │  ← top bar
├────────────────────────┬─────────────────────────────────────────────┤
│  Server Statistics     │  Create Room                                 │
│  ─────────────────     │  Username: [___________]                     │
│  Messages      42      │  [Create Room]                               │
│  Files Sent     3      ├─────────────────────────────────────────────┤
│  Total Users   15      │  Join Room                                   │
│  Total Rooms    2      │  Username: [___________]                     │
│  ─────────────────     │  Room Code: [__________________________]      │
│  [Refresh Now]         │  [Join Room]                                 │
└────────────────────────┴─────────────────────────────────────────────┘
│  status bar                                                           │
└──────────────────────────────────────────────────────────────────────┘
```

#### Connection settings (top bar)

- **Server / Port** — host and port of the chat server (defaults: `127.0.0.1:8001`).
- **Route via Tor** checkbox — when ticked, reveals `Dir Host` and `Dir Port` fields for the directory server.
- **Connect** button — calls `_manual_connect()`.
- **Status indicator** — shows `Connected (Direct)` or `Connected (Tor)` in green, or `Disconnected` / error text in red.

Changing settings while connected shows a "Settings changed — reconnect to apply" hint. Clicking Connect again closes the old connection on a worker thread and opens a fresh one.

#### Stats panel

Stats are fetched over the **existing persistent connection** — no open/close per fetch.

```python
conn.send_to('GetStats', {})
resp = conn.recv_one()          # synchronous; safe because receiver is not running
```

- `_fetch_stats()` runs on a worker thread to avoid blocking the GUI.
- Auto-refreshes every 5 seconds via `self.after(_STATS_INTERVAL_MS, ...)`.
- `_schedule_stats_refresh(delay)` / `_cancel_stats_refresh()` manage the `after` handle.
- `destroy()` always cancels the pending `after` to prevent callbacks on a dead widget.
- `winfo_exists()` is checked in worker callbacks for the same reason.

**Stats are never fetched synchronously on the receiver thread** — the receiver is stopped before `recv_one()` is ever called (either it was never started, or `stop_receiver()` was called on leave).

#### Create / Join room

Both operations use the existing `self._conn`:
1. Cancel stats auto-refresh.
2. Disable Create/Join buttons.
3. Worker thread: `send_to(CreateRoom|JoinRoom, ...)` → `recv_one()` for response.
4. On success: call `on_enter_room(conn, username, room_code, members)`.
5. On error: show error in status bar, re-enable buttons, resume stats refresh.

#### Connection lifecycle on this screen

- `initial_conn=None` (first launch): status bar shows "Fill in connection settings and click Connect." Room buttons disabled.
- `initial_conn=<existing>` (returning from chat room): `_mark_connected()` sets the status indicator, stats refresh starts immediately at delay=0.
- `_manual_connect()`: closes old conn on background thread, creates new conn, calls `_on_connect_success(conn)` which calls `on_connected(conn)` (notifies App), marks connected, enables buttons, starts stats refresh.

---

### `client/chat_screen.py`

**Role:** Chat room screen. Displays rolling message history, a live user list, a send bar, and file attach/save.

#### Layout

```
┌──────────────────────────────────────────────────────────────────────┐
│  Room: <room_code>  [Copy Code]                        [Leave Room]  │  ← top bar
├────────────────────────────────────────┬─────────────────────────────┤
│  Messages (scrollable CTkTextbox)      │  Users                      │
│                                        │  ──────────────────         │
│  you: hello                            │    alice                    │
│  alice: hey!                           │    bob                      │
│  *** bob joined the room ***           │    carol                    │
│  bob: hi                               │                             │
│  alice sent: report.pdf (~42 KB) [Save]│                             │
│  *** carol left the room ***           │                             │
│                                        │                             │
├────────────────────────────────────────┴─────────────────────────────┤
│  [Attach File]  [message entry…]                            [Send]   │  ← input bar
└──────────────────────────────────────────────────────────────────────┘
```

#### Message box

Uses `CTkTextbox` (underlying `tk.Text`) in `state="disabled"` to prevent user editing. Text tags applied directly to `_textbox` for coloured output:

| Tag | Colour | Used for |
|---|---|---|
| `username` | Blue `#4a9eff` bold | Other users' names |
| `self_tag` | Green `#5cb85c` bold | Your own messages |
| `system` | Grey `#888888` italic | Join/leave/system events |
| `file_tag` | Amber `#f0a500` italic | File transfer notices |
| `error` | Red `#e05252` | Error messages |

`_append(parts)` enables the textbox, inserts all tagged/untagged parts, disables, scrolls to end.

#### User list

`CTkScrollableFrame` on the right. `_user_labels: dict[str, CTkLabel]` maps username → label widget.
- `_add_user_label(username)` — creates and packs a label.
- `_remove_user_label(username)` — destroys the label and removes from dict.
Both are called from `_handle_message` on `UserJoined` / `UserLeft`.

#### Incoming message handler

`_handle_message(msg_type, data)` runs on the main thread (scheduled via `self.after(0, ...)`):

| `msg_type` | Action |
|---|---|
| `IncomingMessage` | `_append_chat(from_username, message)` |
| `IncomingFile` | `_append_file_notice(...)` with inline Save button |
| `UserJoined` | Add to user list, append system message |
| `UserLeft` | Remove from user list, append system message |
| `RoomLeft` | Append system message, call `_cleanup_and_leave()` |
| `Stats` | Append formatted stats as system message |
| `Error` | Append error message |

#### File sending

`_pick_file()` opens `tkinter.filedialog.askopenfilename`. File is read, base64-encoded, and sent via `conn.send_to('SendFile', {filename, filedata})` on a worker thread. Size displayed in KB.

#### File receiving and saving

`_append_file_notice` embeds a `tkinter.Button` (not CTkButton — embedded in `tk.Text` via `window_create`) directly in the message flow:

```
  alice sent: report.pdf  (~42 KB)  [Save]
```

Clicking Save calls `_save_file(filename, filedata)` which opens `asksaveasfilename` pre-filled with the original filename, then base64-decodes and writes to disk.

The `filedata` string is captured in the button's `command` lambda at creation time and held for the lifetime of the widget.

#### Send message

`_send_message()` reads the entry, clears it, appends the message locally with `self_tag` (immediate feedback), then sends on a worker thread. In Tor mode, `send_to` blocks for the full circuit round-trip (the lock in `TorSocket` serialises it with the poll loop), so doing it on a worker thread prevents GUI freezing.

#### Leave room

`_do_leave()`:
1. Sets `_leaving = True` (prevents `_handle_disconnect` from showing an error).
2. Disables Leave button.
3. Worker thread: `send_to('LeaveRoom', {})`, then `conn.stop_receiver()`.
4. Calls `on_leave(conn)` on the main thread — App receives the still-open connection and passes it to the new HomeScreen.

`_cleanup_and_leave()` (server-initiated leave): same flow without the `send_to`.

**`_leaving` flag:** Without it, `stop_receiver()` or `conn.close()` causes the receiver thread to get `None` / `ConnectionError` and call `on_disconnect`, which would try to update widgets on the home screen (already shown) or display a spurious "Disconnected" error.

---

## AES details

- Mode: CBC with a fresh random 16-byte IV per encryption call.
- IV is prepended to the ciphertext: `ciphertext = iv + AES_CBC_encrypt(key, iv, padded_plaintext)`.
- Padding: PKCS7 via `Crypto.Util.Padding.pad/unpad`, block size 16.
- Key size: 128-bit (16 bytes) for all relay keys and ephemeral setup keys.
- Empty plaintext (`b''`) is valid: PKCS7 pads it to 16 bytes, AES encrypts to 16 bytes of ciphertext. After decryption and unpadding the result is `b''`. This is used by the poll mechanism.

---

## RSA details

- Key size: 2048-bit, generated fresh per node process with `RSA.generate(2048)`.
- Padding: PKCS1-OAEP (`Crypto.Cipher.PKCS1_OAEP`).
- Only used to encrypt the 16-byte ephemeral setup key per hop. Never used for relay data.
- Public key distributed as PEM string via the directory server at registration time.
- Max plaintext for RSA-OAEP (2048-bit): ~214 bytes — well above 16 bytes, well below routing payloads.

---

## Tor mode chat — end-to-end data flow

### Sending a message (GUI client)

```
ChatScreen (main thread)
  │  _send_message() → worker thread
  │    conn.send_to('SendMessage', {message})
  │      → send_msg(tor_sock, {...})
  │          → tor_sock.sendall([4B][json])
  │              acquires _lock
  │              strips 4B, encrypts K3→K2→K1
  │              sends RELAY to entry node
  │
entry node (handle_relay)
  │  decrypts K1, forwards RELAY to middle
  │
middle node (handle_relay)
  │  decrypts K2, forwards RELAY to exit
  │
exit node (handle_relay)
  │  decrypts K3 → raw JSON bytes
  │  send_msg(dest_sock, raw_json) → [4B][json] to chat server
  │  select(dest_sock, timeout=5.0)
  │  recv_msg(dest_sock) → Ack bytes
  │  encrypts K3, returns RELAY_RESPONSE
  │
middle → re-encrypts K2, returns RELAY_RESPONSE
entry  → re-encrypts K1, returns RELAY_RESPONSE
  │
tor_sock.sendall reads RELAY_RESPONSE
  decrypts K1→K2→K3 → Ack JSON bytes
  buffers in _buf (never read in GUI mode)
  releases _lock
```

### Receiving a push notification (GUI client)

```
Connection._tor_receiver thread
  │  tor_sock.poll()
  │    acquires _lock
  │    encrypts b'' K3→K2→K1
  │    sends RELAY to entry
  │
entry → decrypts K1 → forwards to middle
middle → decrypts K2 → forwards to exit
  │
exit node (handle_relay)
  │  decrypts K3 → b'' (empty, poll)
  │  does NOT send to dest_sock
  │  select(dest_sock, timeout=0.5)
  │    if data ready: recv_msg(dest_sock) → pushed message bytes
  │    else: raw_response = b''
  │  encrypts response K3, returns RELAY_RESPONSE
  │
middle → re-encrypts K2, returns RELAY_RESPONSE
entry  → re-encrypts K1, returns RELAY_RESPONSE
  │
tor_sock.poll reads RELAY_RESPONSE
  releases _lock
  decrypts K1→K2→K3
  if empty → returns None (loop continues)
  else     → json.loads → returns message dict
  │
Connection._tor_receiver: on_message(msg_type, data)
  │
ChatScreen: self.after(0, lambda: _handle_message(msg_type, data))
  updates message box and/or user list on main thread
```

### Fetching stats (GUI client — home screen)

```
HomeScreen worker thread
  │  conn.send_to('GetStats', {})
  │    → send_msg(sock, {...})   [direct: raw socket]
  │    → tor_sock.sendall(...)   [Tor: full circuit relay, response in _buf]
  │
  │  conn.recv_one()
  │    → recv_msg(sock)   [direct: blocks on socket]
  │    → recv_msg(tor_sock)  [Tor: reads from _buf where sendall put the Stats response]
  │
  │  resp['data'] → {total_messages, total_files, ...}
  │
self.after(0, lambda: _apply_stats(stats))
  updates StringVar labels on main thread
  schedules next refresh via self.after(5000, ...)
```

**Why `recv_one()` is safe here:** The receiver thread is not running while on the home screen. `stop_receiver()` was called when leaving the chat room, so no competing reads on the socket.

---

## What each node knows

| Node | Knows |
|---|---|
| Entry | Client's IP, middle's address, K1, forward payload (opaque blob) |
| Middle | Entry's address, exit's address, K2, forward payload (opaque blob) |
| Exit | Middle's address, server's address, K3, plaintext message |
| Server / Chat server | Exit's IP, plaintext message |
| Directory | All node addresses and public keys; nothing about circuits or traffic |

The chat server sees only the exit node's IP, never the real client's IP.

---

## Start scripts

### `start.bat` (Windows)

- `setlocal EnableDelayedExpansion` for delayed variable expansion inside `for /L` loops.
- Parses `--debug` and `--nodes N` in any order with a `:parse_args` goto loop.
- Port arithmetic uses `set /a`; port variables referenced with `!VAR!` (delayed expansion).
- Each component opens in a named `cmd /k` window so it stays open for inspection.
- Cleanup uses `taskkill /FI "WINDOWTITLE eq ..."` for each named window.
- Windows launched: `Dir-Server`, `Chat-Server`, `Dest-Server`, `Entry-Node-N`, `Middle-Node-N`, `Exit-Node-N`, `Tor-Client`, `Chat-Client-Tor`.

### `start.sh` (Linux/macOS)

- `trap cleanup EXIT INT TERM` ensures cleanup on Ctrl+C, kill, or normal exit.
- `cleanup()` uses `pkill -f "python Servers/..."` for each component.
- `launch()` detects terminal emulator: `gnome-terminal` → `xterm` → macOS `osascript`.
- PIDs of terminal processes collected in `TERM_PIDS` array and killed in cleanup.
- Same `--debug` / `--nodes N` parsing via `case` in a `while [[ $# -gt 0 ]]` loop.
- Same port layout as the bat file.
- Main process blocks with `while true; do sleep 1; done` after launching everything.
