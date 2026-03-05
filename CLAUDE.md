# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

A simplified Tor-like onion routing network implemented entirely in Python using raw TCP sockets. Messages travel through three relay nodes (entry → middle → exit) before reaching a destination server. Each hop has its own AES-128 session key; messages are triple-encrypted on the way out and triple-decrypted on the way back. The exit→server leg carries plaintext (no AES).

## Repository layout

```
school-tor/
├── Servers/
│   ├── directory_server.py   # Node registry; clients query this to build circuits
│   ├── node.py               # All three relay roles (entry/middle/exit) in one file
│   ├── client.py             # Interactive client; builds circuit, sends messages
│   └── server.py             # Plaintext destination server (echo)
├── start.bat                 # Windows launcher
├── start.sh                  # Linux/macOS launcher
├── requirements.txt          # pycryptodome
└── venv/                     # Python virtual environment
```

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

# Manual launch order (directory first, then server, then nodes, then client)
python Servers/directory_server.py
python Servers/server.py --port 9000
python Servers/node.py --type entry  --port 9001 [--debug]
python Servers/node.py --type middle --port 9002 [--debug]
python Servers/node.py --type exit   --port 9003 [--debug]
python Servers/client.py --dest-port 9000
```

Port layout when using `--nodes N`:
- Entry nodes:  9001 … 9000+N
- Middle nodes: 9101 … 9100+N
- Exit nodes:   9201 … 9200+N

## Wire protocol (shared by all components)

Every TCP message in the system uses the same length-prefixed framing:

```
┌─────────────────────┬──────────────────────────────┐
│  4-byte big-endian  │  payload (JSON or raw bytes)  │
│  length header      │                               │
└─────────────────────┴──────────────────────────────┘
```

`recv_msg` / `send_msg` are duplicated in every file (no shared module). `send_msg` accepts `dict`, `str`, or `bytes`; dicts are JSON-serialised before framing.

Control messages are JSON objects with a `"type"` field. Raw bytes (exit→server and back) use the same framing but carry no JSON envelope.

---

## Component deep-dives

### `Servers/directory_server.py`

**Role:** Central registry. Nodes register on startup; the client queries once per session to get the node list.

**State:** A single module-level list `nodes` protected by `nodes_lock` (threading.Lock). Each entry is:
```python
{'node_type': 'entry'|'middle'|'exit', 'host': str, 'port': int, 'public_key': str}
```

**Protocol — two message types, one request/response per connection:**

| Incoming `type` | Expected fields | Response |
|---|---|---|
| `REGISTER` | `node_type`, `host`, `port`, `public_key` (PEM string) | `{"status": "ok"}` |
| `GET_NODES` | _(none)_ | `{"nodes": [...]}` |

On `REGISTER`, any existing entry for the same `host:port` is replaced (supports node restart without stale entries).

**Threading:** Each accepted connection gets its own daemon thread; the thread handles exactly one request then closes the socket.

---

### `Servers/node.py`

**Role:** Relay node. A single file serves all three roles; the `--type entry|middle|exit` argument controls behaviour.

**Module-level globals** (set in `main()` before any threads start):
- `rsa_key` — the node's RSA-2048 private key (generated fresh on each launch)
- `node_type` — `'entry'`, `'middle'`, or `'exit'`
- `debug` — bool; enables per-relay hex dumps
- `circuits` — dict keyed by `circuit_id` (UUID string); each value is a circuit record:
  ```python
  {
      'key':      bytes,           # AES-128 relay key for this hop
      'next_sock': socket | None,  # persistent TCP socket to next hop; None for exit
      'dest':     (host, port),    # only set on exit nodes
      'is_exit':  bool,
  }
  ```
- `circuits_lock` — threading.Lock protecting `circuits`

**Threading model:** `server_sock.accept()` loop spawns one daemon thread per incoming connection via `handle_connection`. Each thread owns its connection for its full lifetime and handles all messages for all circuits established over that connection.

#### Circuit setup (`CIRCUIT_SETUP`)

The entry node receives CIRCUIT_SETUP from the client. Middle and exit nodes receive it forwarded from the previous hop.

**Payload structure — hybrid encryption:**

Each node's payload is a dict:
```json
{
    "encrypted_key":  "<base64 of RSA-OAEP(node_pub, setup_aes_key)>",
    "encrypted_data": "<base64 of AES-CBC(setup_aes_key, json_inner)>"
}
```
- `setup_aes_key` is a 16-byte ephemeral key generated by the client for this payload only; it is RSA-OAEP encrypted so only the target node can recover it.
- `json_inner` (AES-encrypted) contains the actual routing data:
  - All nodes: `"key"` — base64 of the 16-byte AES relay key for this hop (K1/K2/K3).
  - Non-exit nodes: `"next_host"`, `"next_port"`, `"forward_payload"` (nested payload dict for the next hop, already encrypted for that node).
  - Exit node only: `"dest_host"`, `"dest_port"` — the final destination.

**Why hybrid?** RSA-OAEP with 2048-bit keys can only encrypt ~214 bytes. The routing data (especially `forward_payload` which embeds nested payloads) far exceeds this limit. Encrypting only the 16-byte ephemeral key via RSA and the rest via AES sidesteps the limit entirely.

**Setup flow for entry/middle nodes (`handle_circuit_setup`):**
1. Decrypt `payload` → recover relay key + next hop address + forwarded payload.
2. Open a persistent TCP connection to next hop (`next_sock`).
3. Forward `{"type": "CIRCUIT_SETUP", "circuit_id": ..., "payload": forward_payload}` to next hop.
4. Block until next hop returns `{"status": "ok"}`.
5. Store `circuit_id → {key, next_sock, is_exit: False}` in `circuits`.
6. Register `circuit_id` in `local_circuits` (the set owned by this thread).
7. Return `{"status": "ok"}` to the previous hop.

**Setup flow for exit nodes:** Same decrypt step, but stores `dest` instead of `next_sock` and sets `is_exit: True`. No outgoing connection is opened during setup — the exit connects to the destination server fresh on each relay.

#### Relay (`RELAY` / `RELAY_RESPONSE`)

**Forward path — each node decrypts one layer:**

```
client sends:   AES_K1( AES_K2( AES_K3(message) ) )
entry receives: AES_K1(...) → strips K1 → forwards AES_K2(AES_K3(message)) to middle
middle receives: AES_K2(...) → strips K2 → forwards AES_K3(message) to exit
exit receives:  AES_K3(...) → strips K3 → sends raw message to server
```

The `data` field of a RELAY message is base64-encoded ciphertext. Each node base64-decodes it, AES-decrypts with its relay key, then base64-encodes the result before forwarding.

**Backward path — each node adds one layer:**

```
server sends raw response R
exit  sends: AES_K3(R)
middle sends: AES_K2( AES_K3(R) )
entry sends:  AES_K1( AES_K2( AES_K3(R) ) )
client decrypts: strip K1 → strip K2 → strip K3 → R
```

The relay handler (`handle_relay`) for non-exit nodes:
1. Receive RELAY from previous hop.
2. AES-decrypt with own key.
3. Forward to `next_sock` (the persistent socket established during CIRCUIT_SETUP).
4. Block waiting for RELAY_RESPONSE from next hop.
5. AES-encrypt the response data with own key (adding the backward layer).
6. Return RELAY_RESPONSE to previous hop.

The relay handler for exit nodes:
1. AES-decrypt the final layer → plaintext message.
2. Open a fresh TCP connection to `dest`.
3. `send_msg(dest_sock, plaintext)` / `recv_msg(dest_sock)`.
4. AES-encrypt the server's raw response.
5. Return RELAY_RESPONSE to middle.

**Debug mode (`--debug`):** The `dbg()` helper prints label + byte count + first 32 bytes as hex for: received ciphertext, post-peel bytes, plaintext (exit only), server response, and re-encrypted response.

#### Connection teardown

`handle_connection` maintains a `local_circuits` set. When the loop exits (client disconnects or socket error), the `finally` block:
1. Closes `conn` (incoming socket).
2. For each circuit in `local_circuits`: pops the circuit from `circuits`, closes `next_sock` if present.

Closing `next_sock` causes the next node's `recv_msg` to return `None`, breaking its loop and triggering its own `finally` block. This cascade propagates all the way from entry → middle → exit automatically.

---

### `Servers/client.py`

**Role:** Builds a circuit and provides an interactive message REPL.

**Startup sequence:**
1. `GET_NODES` from directory server → list of registered nodes.
2. `pick_nodes` — groups nodes by type, then calls `random.choice` on each group independently. Supports any number of nodes per type.
3. `build_circuit` — constructs nested payloads from inside out (exit → middle → entry), connects to entry, sends `CIRCUIT_SETUP`, waits for `{"status": "ok"}`.
4. REPL: `input("you > ")` → `send_relay` → print response.

**Payload construction (`make_setup_payload`):**
```python
setup_key = get_random_bytes(16)
enc_key  = RSA_OAEP(node_pub).encrypt(setup_key)     # 16 bytes → 256 bytes
enc_data = AES_CBC(setup_key, json.dumps(inner))
return {'encrypted_key': b64(enc_key), 'encrypted_data': b64(enc_data)}
```
Called once per hop, starting with exit, so the exit payload is embedded inside the middle payload, which is embedded inside the entry payload.

**`send_relay`:**
```python
data = AES_K3(message)   # exit layer  (innermost)
data = AES_K2(data)      # middle layer
data = AES_K1(data)      # entry layer (outermost)
send RELAY → wait for RELAY_RESPONSE
data = AES_K1_decrypt(data)
data = AES_K2_decrypt(data)
data = AES_K3_decrypt(data)  # → original server response
```

The circuit (K1, K2, K3, entry_sock, circuit_id) is held in memory for the process lifetime; each user message reuses the same circuit.

---

### `Servers/server.py`

**Role:** Plaintext destination — sits at the end of the chain, receives unencrypted messages from the exit node.

Uses the same length-prefixed framing as everything else; no AES. Each connection handles exactly one request: reads a message, prints it, echoes `"Echo: <text>"` back, closes.

---

## AES details

- Mode: CBC with a random 16-byte IV generated per encryption call.
- IV is prepended to the ciphertext: `ciphertext = iv + AES_CBC_encrypt(key, iv, padded_plaintext)`.
- Padding: PKCS7 (via `Crypto.Util.Padding.pad/unpad`), block size 16.
- Key size: 128-bit (16 bytes) for all relay keys and ephemeral setup keys.

## RSA details

- Key size: 2048-bit, generated fresh per node process with `RSA.generate(2048)`.
- Padding: PKCS1-OAEP (via `Crypto.Cipher.PKCS1_OAEP`).
- Only used to encrypt the 16-byte ephemeral setup key; never used for relay data.
- Public key distributed as PEM string via the directory server.

## What each node knows

| Node | Knows |
|---|---|
| Entry | Client's address, middle's address, K1, forward payload (opaque to entry) |
| Middle | Entry's address, exit's address, K2, forward payload (opaque to middle) |
| Exit | Middle's address, server's address, K3, plaintext message |
| Server | Exit's address, plaintext message |
| Directory | All node addresses and public keys; nothing about circuits or traffic |
