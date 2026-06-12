# Protocol Reference

This document describes every wire protocol used in `school-tor`, with concrete
JSON examples. There are four layers:

1. **Wire framing** — shared by every TCP connection in the system.
2. **Directory protocol** — node registration & discovery (`directory_server.py`).
3. **Tor protocol** — circuit setup and onion relay (`node.py`, `client/network.py`, `chat_client.py`).
4. **Chat protocol** — the application-level messages carried *inside* the Tor circuit (or sent directly) between a client and `chat_server.py`.

A fifth, trivial protocol (the plaintext echo server, `Servers/server.py` /
`Servers/client.py`) is documented at the end for completeness.

---

## 1. Wire framing

Every TCP message in the system (except the AES-encrypted onion blobs, which
are framed *inside* a JSON field) uses the same length-prefixed framing:

```
┌─────────────────────┬──────────────────────────────┐
│  4-byte big-endian   │  payload (JSON or raw bytes)  │
│  length header       │                               │
└─────────────────────┴──────────────────────────────┘
```

- `recv_msg(sock)` reads the 4-byte header, then reads exactly that many
  payload bytes (looping as needed), returning raw `bytes` or `None` on EOF.
- `send_msg(sock, data)` accepts `dict` (JSON-encoded), `str` (UTF-8), or
  `bytes`, and prepends the 4-byte length before `sendall()`.
- `send_to(sock, msg_type, data)` wraps `data` in the standard envelope:

```json
{"type": "MessageType", "data": {...}}
```

This `recv_msg` / `send_msg` / `send_to` trio is duplicated in every server
file and in `client/network.py`.

---

## 2. Directory server protocol

**Endpoint:** `directory_server.py`, default `0.0.0.0:8000`.
**Pattern:** one request → one response → connection closed.

### 2.1 `REGISTER` (node → directory)

Sent by every relay node on startup.

```json
{
    "type": "REGISTER",
    "node_type": "entry",
    "host": "127.0.0.1",
    "port": 9001,
    "public_key": "-----BEGIN PUBLIC KEY-----\nMIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8A...\n-----END PUBLIC KEY-----\n"
}
```

| Field | Type | Notes |
|---|---|---|
| `node_type` | `"entry" \| "middle" \| "exit"` | role of this node |
| `host` | str | IP clients/other nodes should connect to |
| `port` | int | listen port |
| `public_key` | str (PEM) | RSA-2048 public key, used by clients for hybrid encryption during `CIRCUIT_SETUP` |

**Response:**

```json
{"status": "ok"}
```

The directory upserts by `(host, port)` — re-registering (e.g. after a
restart) replaces the old entry and resets `fail_count` to `0`.

### 2.2 `GET_NODES` (client → directory)

```json
{"type": "GET_NODES"}
```

**Response** — nodes grouped by type, `fail_count` stripped:

```json
{
    "nodes": {
        "entry": [
            {"node_type": "entry", "host": "127.0.0.1", "port": 9001, "public_key": "-----BEGIN PUBLIC KEY-----\n...\n-----END PUBLIC KEY-----\n"}
        ],
        "middle": [
            {"node_type": "middle", "host": "127.0.0.1", "port": 9002, "public_key": "-----BEGIN PUBLIC KEY-----\n...\n-----END PUBLIC KEY-----\n"}
        ],
        "exit": [
            {"node_type": "exit", "host": "127.0.0.1", "port": 9003, "public_key": "-----BEGIN PUBLIC KEY-----\n...\n-----END PUBLIC KEY-----\n"}
        ]
    }
}
```

Any of the three lists may contain zero or more entries (e.g. with
`--nodes 3` there are 3 entries per type). `pick_nodes()` calls
`random.choice()` on each list and raises `RuntimeError` if any list is empty.

### 2.3 `PING` (directory health checker → node)

Sent by `directory_server.py`'s background `health_check_loop` every
`_CHECK_INTERVAL` (10s) to each registered node, on a short-lived connection.

```json
{"type": "PING"}
```

**Response:**

```json
{"status": "pong"}
```

A node that fails 3 consecutive checks (`_MAX_FAILURES`) is removed from the
registry.

---

## 3. Tor protocol

All Tor control messages use the same `[4-byte length][JSON]` framing as
everything else.

| `type` | Direction | Purpose |
|---|---|---|
| `CIRCUIT_SETUP` | client → entry → middle → exit | Establish one hop of the circuit (recursive cascade) |
| `RELAY` | client → entry → middle → exit | Forward one onion-encrypted payload |
| `RELAY_RESPONSE` | exit → middle → entry → client | Return a response back through the circuit |

### 3.1 Cryptographic building blocks

- **AES-128-CBC** (`aes_encrypt` / `aes_decrypt`): fresh random 16-byte IV per
  call, PKCS7 padding. Output = `iv (16B) || ciphertext`. Empty plaintext
  (`b''`) pads to a 16-byte block, so `aes_encrypt(key, b'')` is always 32
  bytes — this is what makes "poll" relays indistinguishable from real ones.
- **RSA-2048-OAEP** (`PKCS1_OAEP`): used *only* to encrypt the ephemeral
  16-byte `setup_key` during `CIRCUIT_SETUP`. Max plaintext ~214 bytes, far
  above the 16-byte key it carries.
- **Hybrid envelope** (`make_setup_payload`):

```json
{
    "encrypted_key":  "<base64 — RSA-OAEP(node_pub_key, setup_key)>",
    "encrypted_data": "<base64 — AES-CBC(setup_key, JSON(inner))>"
}
```

`setup_key` is a fresh random 16 bytes generated once per hop, used once, and
discarded.

### 3.2 `CIRCUIT_SETUP`

**Wire message (client → entry):**

```json
{
    "type": "CIRCUIT_SETUP",
    "circuit_id": "8f14e45f-ceea-4abc-8000-000000000001",
    "payload": {
        "encrypted_key":  "Qb3F...== (base64 RSA-OAEP ciphertext, 256B)",
        "encrypted_data": "x7Lm...== (base64 AES-CBC ciphertext)"
    }
}
```

`payload.encrypted_data`, once RSA+AES-decrypted by the entry node, yields the
`inner` dict. Its shape depends on the node's role:

**`inner` for entry / middle nodes:**

```json
{
    "key": "MTIzNDU2Nzg5MGFiY2RlZg==",
    "next_host": "127.0.0.1",
    "next_port": 9002,
    "forward_payload": {
        "encrypted_key": "...",
        "encrypted_data": "..."
    }
}
```

| Field | Meaning |
|---|---|
| `key` | base64 of the 16-byte AES relay key for *this* hop (K1 for entry, K2 for middle) |
| `next_host` / `next_port` | address of the next hop |
| `forward_payload` | opaque hybrid-encrypted payload for the next node (this node cannot read it) |

**`inner` for the exit node:**

```json
{
    "key": "ZmVkY2JhMDk4NzY1NDMyMQ==",
    "dest_host": "127.0.0.1",
    "dest_port": 8001
}
```

| Field | Meaning |
|---|---|
| `key` | base64 of the 16-byte AES relay key K3 |
| `dest_host` / `dest_port` | the final destination (e.g. the chat server) |

**Response — success (each node to previous hop):**

```json
{"status": "ok"}
```

**Response — failure:**

```json
{"status": "error", "msg": "Cannot reach next hop: [Errno 61] Connection refused"}
```

#### Setup cascade

The client builds the three payloads from the inside out
(exit → middle → entry), then sends `CIRCUIT_SETUP` to the entry node only:

```
client --CIRCUIT_SETUP(entry_payload)--> entry
                                            │ decrypts its layer (gets K1, middle addr, middle_payload)
                                            │ opens TCP to middle, generates a *fresh* next_circuit_id
                                            ▼
                                          middle  <--CIRCUIT_SETUP(middle_payload, next_circuit_id)--
                                            │ decrypts its layer (gets K2, exit addr, exit_payload)
                                            │ opens TCP to exit, generates another fresh next_circuit_id
                                            ▼
                                           exit   <--CIRCUIT_SETUP(exit_payload, next_circuit_id)--
                                            │ decrypts its layer (gets K3, dest addr)
                                            │ opens persistent TCP to dest_host:dest_port
                                            │
                                           exit  --{"status":"ok"}--> middle --{"status":"ok"}--> entry --{"status":"ok"}--> client
```

**Per-hop circuit IDs:** the `circuit_id` on the client→entry link is
different from the one entry generates for entry→middle, which differs again
from middle→exit. An observer watching two links cannot correlate them by
`circuit_id`.

### 3.3 `RELAY`

Carries one onion-encrypted application payload. The client triple-encrypts
the plaintext with K3 innermost, K1 outermost.

**Wire message (client → entry, and forwarded hop-to-hop):**

```json
{
    "type": "RELAY",
    "circuit_id": "8f14e45f-ceea-4abc-8000-000000000001",
    "data": "qD9f3kP2... (base64 — AES-CBC ciphertext, iv||ct)"
}
```

**Onion layer format** — each layer is the raw output of `aes_encrypt`:

```
[ IV (16 bytes) ][ ciphertext (multiple of 16 bytes) ]
```

**Client constructs `data`** for a real chat-protocol message (here,
`{"type": "GetStats", "data": {}}`, framed and stripped of its own 4-byte
prefix first):

```
plaintext = b'{"type": "GetStats", "data": {}}'
data = AES_K1( AES_K2( AES_K3( plaintext ) ) )
```

**Each hop's behaviour:**

| Hop | Action |
|---|---|
| Entry | `aes_decrypt(K1, data)` → forwards as new `RELAY` (own fresh `next_circuit_id`) to middle; blocks for `RELAY_RESPONSE` |
| Middle | `aes_decrypt(K2, data)` → forwards as new `RELAY` to exit; blocks for `RELAY_RESPONSE` |
| Exit | `aes_decrypt(K3, data)` → plaintext. If non-empty → "real relay" (deliver to dest). If empty (`b''`) → "poll" (see §3.5) |

**Exit "real relay" behaviour:**

1. `send_msg(dest_sock, plaintext)` — delivers the raw chat-protocol frame to
   the chat server (re-framed with its own 4-byte length prefix).
2. `select.select([dest_sock], [], [], 5.0)` — waits up to 5s for the chat
   server's response (e.g. an `Ack`).
3. Reads one message if ready, else `raw_response = b''`.

### 3.4 `RELAY_RESPONSE`

Carries the response back through the circuit, each hop adding (not
removing) one AES layer.

**Wire message — success:**

```json
{
    "type": "RELAY_RESPONSE",
    "circuit_id": "8f14e45f-ceea-4abc-8000-000000000001",
    "data": "fT8wQ1z... (base64 — AES-CBC ciphertext)"
}
```

**Wire message — error** (e.g. unknown circuit, next hop disconnected):

```json
{
    "type": "RELAY_RESPONSE",
    "circuit_id": "8f14e45f-ceea-4abc-8000-000000000001",
    "error": "next hop disconnected"
}
```

**Backward encryption stack** (each hop *adds* a layer with its own key):

```
exit   sends: AES_K3( raw_response )
middle sends: AES_K2( AES_K3( raw_response ) )
entry  sends: AES_K1( AES_K2( AES_K3( raw_response ) ) )
client decrypts:  K1 → K2 → K3  →  raw_response
```

`raw_response` is the chat server's complete framed reply (4-byte length +
JSON), or `b''` if nothing was pending (poll with no data, or real relay that
timed out).

### 3.5 Poll mechanism (push notifications)

A Tor circuit is strictly request/response, so the server cannot push
messages spontaneously. The client's receiver thread fakes a continuous
"anything new?" channel by sending **empty relays**:

```
TorSocket.poll():
    data = AES_K1( AES_K2( AES_K3( b'' ) ) )     # 32 bytes: iv(16) + one padded block(16)
    send RELAY{circuit_id, data}
```

The exit node decrypts down to K3 and gets `b''`. It recognises this as a poll:

- Skips `send_msg(dest_sock, ...)` entirely.
- `select.select([dest_sock], [], [], 0.5)` — short timeout, just checks for
  server-pushed data (e.g. another user's `IncomingMessage`).
- Re-encrypts whatever it found (or `b''`) and returns `RELAY_RESPONSE`.

The client decrypts K1→K2→K3:

- Empty → `poll()` returns `None` (nothing pending); loop immediately polls
  again — the exit node's 0.5s `select()` provides natural pacing (~2/s).
- Non-empty → `json.loads(...)` → returns the message dict, e.g.
  `{"type": "IncomingMessage", "data": {"from_username": "bob", "message": "hi"}}`.

A `threading.Lock` in `TorSocket` serialises `sendall()` (main thread) and
`poll()` (receiver thread) so a real relay and a poll relay never interleave
on the same circuit.

### 3.6 Full worked example

Sending `{"type": "SendMessage", "data": {"message": "hello"}}`:

```
1. plaintext = b'{"type": "SendMessage", "data": {"message": "hello"}}'

2. Client encrypts:
   enc = AES_K3(plaintext)        # iv3 || ct3
   enc = AES_K2(enc)               # iv2 || ct2
   enc = AES_K1(enc)               # iv1 || ct1

3. Client -> Entry:
   {"type": "RELAY", "circuit_id": "<client-entry-id>", "data": base64(enc)}

4. Entry:  dec = AES_K1_decrypt(enc)              # = iv2||ct2 (still encrypted w/ K2,K3)
   Entry -> Middle:
   {"type": "RELAY", "circuit_id": "<entry-middle-id>", "data": base64(dec)}

5. Middle: dec = AES_K2_decrypt(dec)              # = iv3||ct3
   Middle -> Exit:
   {"type": "RELAY", "circuit_id": "<middle-exit-id>", "data": base64(dec)}

6. Exit:   plaintext = AES_K3_decrypt(dec)        # = b'{"type":"SendMessage",...}'
   send_msg(dest_sock, plaintext)   # chat server receives [4B len][json]
   select(dest_sock, 5.0) -> ready
   raw_response = recv_msg(dest_sock)  # = b'{"type":"Ack","data":{}}' framed

7. Exit -> Middle:
   {"type": "RELAY_RESPONSE", "circuit_id": "<middle-exit-id>",
    "data": base64(AES_K3(raw_response))}

8. Middle -> Entry:
   {"type": "RELAY_RESPONSE", "circuit_id": "<entry-middle-id>",
    "data": base64(AES_K2(AES_K3(raw_response)))}

9. Entry -> Client:
   {"type": "RELAY_RESPONSE", "circuit_id": "<client-entry-id>",
    "data": base64(AES_K1(AES_K2(AES_K3(raw_response))))}

10. Client decrypts K1 -> K2 -> K3 -> raw_response
    = [4B len][{"type":"Ack","data":{}}]
    -> buffered in TorSocket._buf, read by recv_msg(tor_sock)
```

---

## 4. Chat protocol

**Endpoint:** `chat_server.py`, default `0.0.0.0:8001`.
**Framing:** `[4-byte big-endian length][JSON body]`.
**Body shape:** `{"type": "MessageType", "data": {...}}`.

In Tor mode, this exact frame is what travels as the plaintext payload inside
`RELAY` (see §3.6) — the chat server is unaware whether it's talking to a
direct client or an exit node.

### 4.1 Client → Server

| `type` | `data` | Example |
|---|---|---|
| `CreateRoom` | `{"my_username": str}` | `{"type": "CreateRoom", "data": {"my_username": "alice"}}` |
| `JoinRoom` | `{"room_code": str, "my_username": str}` | `{"type": "JoinRoom", "data": {"room_code": "8f14e45f-ceea-4abc-8000-000000000001", "my_username": "bob"}}` |
| `LeaveRoom` | `{}` | `{"type": "LeaveRoom", "data": {}}` |
| `SendMessage` | `{"message": str}` | `{"type": "SendMessage", "data": {"message": "hello everyone"}}` |
| `SendFile` | `{"filename": str, "filedata": str (base64)}` | `{"type": "SendFile", "data": {"filename": "report.pdf", "filedata": "JVBERi0xLjQK..."}}` |
| `GetStats` | `{}` | `{"type": "GetStats", "data": {}}` |

Notes:
- `my_username` and `room_code` are `.strip()`ped server-side; empty values
  produce an `Error`.
- A connection may belong to at most one room at a time. `CreateRoom` /
  `JoinRoom` while already in a room → `Error`.
- `SendMessage` / `SendFile` while *not* in a room → `Error`.

### 4.2 Server → Client

| `type` | `data` | Notes |
|---|---|---|
| `RoomCreated` | `{"room_code": str, "users": [str, ...]}` | sent to the creator; `users` contains only the creator |
| `RoomJoined` | `{"room_code": str, "users": [str, ...]}` | sent to the joiner; `users` is the **full** member list including the joiner |
| `UserJoined` | `{"username": str, "room_code": str}` | broadcast to pre-existing members when someone joins |
| `RoomLeft` | `{"room_code": str}` | sent to a client after a voluntary `LeaveRoom` |
| `UserLeft` | `{"username": str, "room_code": str}` | broadcast to remaining members when someone leaves (voluntary or disconnect) |
| `IncomingMessage` | `{"from_username": str, "message": str}` | broadcast to all other room members on `SendMessage` |
| `IncomingFile` | `{"from_username": str, "filename": str, "filedata": str (base64)}` | broadcast to all other room members on `SendFile` |
| `Stats` | `{"total_messages": int, "total_files": int, "total_users": int, "total_rooms": int}` | response to `GetStats`; read live from SQLite |
| `Ack` | `{}` | sent back to the sender after `SendMessage` / `SendFile` — required so the exit node's `select()` doesn't time out in Tor mode |
| `Error` | `{"error_message": str}` | sent on any validation/business-logic failure |

### 4.3 Examples — full exchanges

**Create a room:**

```json
// client -> server
{"type": "CreateRoom", "data": {"my_username": "alice"}}

// server -> client (alice)
{"type": "RoomCreated", "data": {"room_code": "8f14e45f-ceea-4abc-8000-000000000001", "users": ["alice"]}}
```

**Another user joins:**

```json
// client -> server
{"type": "JoinRoom", "data": {"room_code": "8f14e45f-ceea-4abc-8000-000000000001", "my_username": "bob"}}

// server -> alice (existing member)
{"type": "UserJoined", "data": {"username": "bob", "room_code": "8f14e45f-ceea-4abc-8000-000000000001"}}

// server -> bob (the joiner)
{"type": "RoomJoined", "data": {"room_code": "8f14e45f-ceea-4abc-8000-000000000001", "users": ["alice", "bob"]}}
```

**Sending a chat message:**

```json
// bob -> server
{"type": "SendMessage", "data": {"message": "hey alice!"}}

// server -> alice (broadcast)
{"type": "IncomingMessage", "data": {"from_username": "bob", "message": "hey alice!"}}

// server -> bob (Ack)
{"type": "Ack", "data": {}}
```

**Sending a file:**

```json
// alice -> server
{"type": "SendFile", "data": {"filename": "notes.txt", "filedata": "SGVsbG8gd29ybGQh"}}

// server -> bob (broadcast)
{"type": "IncomingFile", "data": {"from_username": "alice", "filename": "notes.txt", "filedata": "SGVsbG8gd29ybGQh"}}

// server -> alice (Ack)
{"type": "Ack", "data": {}}
```

**Leaving a room:**

```json
// bob -> server
{"type": "LeaveRoom", "data": {}}

// server -> alice (remaining member)
{"type": "UserLeft", "data": {"username": "bob", "room_code": "8f14e45f-ceea-4abc-8000-000000000001"}}

// server -> bob
{"type": "RoomLeft", "data": {"room_code": "8f14e45f-ceea-4abc-8000-000000000001"}}
```
(The room is deleted server-side once its last member leaves.)

**Stats:**

```json
// client -> server
{"type": "GetStats", "data": {}}

// server -> client
{"type": "Stats", "data": {"total_messages": 42, "total_files": 3, "total_users": 7, "total_rooms": 2}}
```

**Error cases:**

```json
// joining a non-existent room
{"type": "JoinRoom", "data": {"room_code": "does-not-exist", "my_username": "carol"}}
-> {"type": "Error", "data": {"error_message": "Room 'does-not-exist' does not exist"}}

// username already taken in target room
-> {"type": "Error", "data": {"error_message": "Username 'bob' is already taken in that room"}}

// sending a message while not in a room
{"type": "SendMessage", "data": {"message": "hi"}}
-> {"type": "Error", "data": {"error_message": "Not in a room"}}

// malformed JSON frame
-> {"type": "Error", "data": {"error_message": "Invalid JSON"}}

// unknown message type
{"type": "Bogus", "data": {}}
-> {"type": "Error", "data": {"error_message": "Unknown message type: 'Bogus'"}}
```

---

## 5. Echo / destination protocol (`Servers/client.py` ↔ `Servers/server.py`)

Used only for low-level circuit testing — no AES, no JSON envelope, just raw
length-prefixed bytes.

```
client (3-hop circuit) -> exit -> server.py
```

**Request** (whatever bytes the user typed, UTF-8 encoded, framed):

```
[4-byte length]["hello from the other side"]
```

**Response** — `server.py` echoes back with an `"Echo: "` prefix:

```
[4-byte length]["Echo: hello from the other side"]
```

This raw bytes blob is exactly the `raw_response` re-encrypted at each hop in
§3.4 — the echo server has no concept of JSON `type`/`data` envelopes.
