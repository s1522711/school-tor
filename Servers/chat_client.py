"""
CLI Chat Client

Connects to the chat server directly or through an onion circuit (--tor).

Usage:
    python Servers/chat_client.py
    python Servers/chat_client.py --host 127.0.0.1 --port 8001
    python Servers/chat_client.py --tor
    python Servers/chat_client.py --tor --host 127.0.0.1 --port 8001

Commands (once in a room):
    /leave          leave the current room
    /stats          show server statistics
    /file <path>    send a file to everyone in the room
    /quit           disconnect and exit

Tor mode notes:
    Builds a 3-hop onion circuit before connecting to the chat server.
    The circuit is synchronous (one response per send), so push notifications
    from other users (new messages, joins, leaves) are not received.
"""

import socket
import json
import threading
import argparse
import base64
import os
import sys
import uuid
import random

from Crypto.PublicKey import RSA
from Crypto.Cipher import AES, PKCS1_OAEP
from Crypto.Random import get_random_bytes
from Crypto.Util.Padding import pad, unpad


# ── Wire helpers ──────────────────────────────────────────────────────────────

def recv_msg(sock):
    """
    Read one complete length-prefixed message from a TCP socket (or TorSocket).

    How it works:
        Reads a 4-byte big-endian length header, then reads that many payload
        bytes. Both reads loop because a single recv() call can return fewer
        bytes than requested on a TCP stream. For TorSocket, recv() drains from
        an internal buffer (_buf) rather than a real socket, so the loop still
        terminates correctly.

    Why it exists:
        The same framing protocol is used by every component in the system.
        Passing a TorSocket to this function works transparently because
        TorSocket.recv() consumes bytes from its internal buffer exactly like
        a real socket would — the chat protocol layer never has to know whether
        it is talking to a real socket or a Tor circuit.

    Returns:
        bytes — the raw payload, or None if the socket closed / circuit broke.
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
    Send data over TCP (or TorSocket) with a 4-byte big-endian length prefix.

    How it works:
        Serialises dicts to JSON bytes, encodes strs to UTF-8, then prepends
        the 4-byte length and calls sendall(). For a TorSocket, sendall()
        strips the 4-byte frame, triple-encrypts the payload, sends it as a
        RELAY, reads the RELAY_RESPONSE, and buffers the decrypted response.
        The caller then reads that response with recv_msg(tor_sock).

    Why it exists:
        By accepting either a real socket or a TorSocket, this function (and
        send_to which calls it) lets all the chat protocol code above the
        TorSocket layer remain unchanged whether Tor mode is on or off.
    """
    if isinstance(data, dict):
        data = json.dumps(data).encode()
    elif isinstance(data, str):
        data = data.encode()
    sock.sendall(len(data).to_bytes(4, 'big') + data)


def send_to(sock, msg_type: str, data: dict):
    """
    Send a typed chat-protocol message {"type": ..., "data": ...} to sock.

    How it works:
        Wraps msg_type and data in the standard chat envelope and delegates to
        send_msg. No exception handling — the caller is responsible for
        catching errors in its own try/except.

    Why it exists:
        Avoids repeating the {"type": ..., "data": ...} envelope construction
        at every call site. Cleaner than send_msg(sock, {'type': ..., ...}).

    Args:
        sock     — destination (real socket or TorSocket).
        msg_type — e.g. 'SendMessage', 'CreateRoom'.
        data     — the message payload dict.
    """
    send_msg(sock, {'type': msg_type, 'data': data})


# ── Tor circuit ───────────────────────────────────────────────────────────────

def aes_encrypt(key: bytes, plaintext: bytes) -> bytes:
    """
    AES-128-CBC encrypt and prepend a fresh random 16-byte IV.

    How it works:
        Generates a random IV, pads the plaintext to a 16-byte multiple with
        PKCS7, encrypts using AES-CBC, and returns iv + ciphertext. A new
        random IV for every call means identical plaintexts produce different
        ciphertexts, preventing statistical analysis across multiple messages.

    Why it exists:
        Used in TorSocket.sendall() and TorSocket.poll() to add each AES layer
        before sending a RELAY message through the circuit.

    Args:
        key       — 16-byte AES-128 key (K1, K2, or K3).
        plaintext — bytes to encrypt (may be empty b'' for poll).

    Returns:
        bytes — iv (16 B) + ciphertext (multiple of 16 B).
    """
    iv = get_random_bytes(16)
    cipher = AES.new(key, AES.MODE_CBC, iv)
    return iv + cipher.encrypt(pad(plaintext, AES.block_size))


def aes_decrypt(key: bytes, ciphertext: bytes) -> bytes:
    """
    AES-128-CBC decrypt a blob produced by aes_encrypt.

    How it works:
        Splits at byte 16 to recover the IV, reconstructs the same AES-CBC
        cipher, decrypts, then removes PKCS7 padding to restore the plaintext.

    Why it exists:
        Used in TorSocket.sendall() and TorSocket.poll() to peel the three AES
        layers off a RELAY_RESPONSE. The order is K1 → K2 → K3 (outermost
        first), the reverse of the K3 → K2 → K1 wrapping order used on send.

    Args:
        key        — 16-byte AES-128 key.
        ciphertext — iv + ciphertext as produced by aes_encrypt.

    Returns:
        bytes — the original plaintext.
    """
    iv, ct = ciphertext[:16], ciphertext[16:]
    cipher = AES.new(key, AES.MODE_CBC, iv)
    return unpad(cipher.decrypt(ct), AES.block_size)


def get_nodes(dir_host: str, dir_port: int) -> list:
    """
    Query the directory server for the list of registered relay nodes.

    How it works:
        Opens a short-lived TCP connection to the directory server, sends a
        GET_NODES request, reads and parses the JSON response, closes the
        connection, and returns the node list. Each node dict contains:
        {node_type, host, port, public_key (PEM)}.

    Why it exists:
        The directory server is the only bootstrap point where a client can
        discover which relay nodes are live and obtain their RSA public keys.
        Without those keys, the client cannot construct the hybrid-encrypted
        CIRCUIT_SETUP payloads.

    Returns:
        list[dict] — all registered nodes.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect((dir_host, dir_port))
    send_msg(s, {'type': 'GET_NODES'})
    resp = json.loads(recv_msg(s))
    s.close()
    return resp['nodes']


def pick_nodes(nodes: list) -> tuple:
    """
    Randomly select one entry, one middle, and one exit node.

    How it works:
        Groups all nodes by 'node_type', then calls random.choice() on each
        group independently. If any type is missing, raises RuntimeError so
        the caller gets a clear message rather than a silent KeyError.

    Why it exists:
        Randomising node selection makes traffic correlation harder — an
        adversary watching some nodes cannot predict the full path of any
        circuit. It also distributes load when multiple nodes of each type
        are registered (--nodes N in the start scripts).

    Returns:
        tuple — (entry_node, middle_node, exit_node).

    Raises:
        RuntimeError — if any node type is missing from the directory.
    """
    by_type = {}
    for n in nodes:
        by_type.setdefault(n['node_type'], []).append(n)
    missing = [t for t in ('entry', 'middle', 'exit') if not by_type.get(t)]
    if missing:
        raise RuntimeError(f"Directory missing node type(s): {missing}")
    return (random.choice(by_type['entry']),
            random.choice(by_type['middle']),
            random.choice(by_type['exit']))


def make_setup_payload(pub_pem: str, inner: dict) -> dict:
    """
    Hybrid-encrypt a routing dict for one relay node.

    How it works:
        1. Generates a random 16-byte setup_key (ephemeral AES key).
        2. RSA-OAEP encrypts setup_key with the node's public key (pub_pem).
           Only the node with the matching private key can recover setup_key.
        3. AES-CBC encrypts the JSON serialisation of inner with setup_key.
        4. Returns both ciphertexts as base64 strings in a dict.

        This hybrid approach is necessary because RSA-2048-OAEP can encrypt at
        most ~214 bytes, but the inner dict (especially when it contains a
        nested forward_payload) can be thousands of bytes.

    Why it exists:
        Called three times in build_circuit (once per hop, innermost first) to
        produce the nested CIRCUIT_SETUP payload. Each call encrypts for a
        different node using that node's public key, so only the intended node
        can read its own routing instructions.

    Args:
        pub_pem — PEM-encoded RSA-2048 public key of the target node.
        inner   — dict of routing instructions for this node.

    Returns:
        dict — {'encrypted_key': base64, 'encrypted_data': base64}.
    """
    setup_key = get_random_bytes(16)
    pub = RSA.import_key(pub_pem)
    enc_key  = PKCS1_OAEP.new(pub).encrypt(setup_key)
    enc_data = aes_encrypt(setup_key, json.dumps(inner).encode())
    return {
        'encrypted_key':  base64.b64encode(enc_key).decode(),
        'encrypted_data': base64.b64encode(enc_data).decode(),
    }


def build_circuit(entry, middle, exit_node, dest_host: str, dest_port: int):
    """
    Build a 3-hop Tor circuit to dest_host:dest_port and return circuit state.

    How it works:
        1. Generates a UUID circuit_id and three random 16-byte relay keys
           (K1 for entry, K2 for middle, K3 for exit).
        2. Constructs nested hybrid-encrypted payloads from the inside out:
               exit_payload   — K3 + destination address, encrypted for exit.
               middle_payload — K2 + exit's address + exit_payload, for middle.
               entry_payload  — K1 + middle's address + middle_payload, for entry.
           Each node can only read its own layer; the nested payloads are opaque.
        3. Connects to the entry node and sends CIRCUIT_SETUP with entry_payload.
           The entry node decrypts its layer, connects to middle, sends
           middle_payload. Middle connects to exit and sends exit_payload.
           The exit node connects to dest_host:dest_port and replies 'ok'.
           The 'ok' bubbles back through middle and entry to this client.
        4. If 'ok' is received, the circuit is live. Returns all state needed
           for TorSocket to send relay messages.

    Why it exists:
        The circuit must be built before any chat messages can be sent in Tor
        mode. After this call, the client has a single TCP socket to entry and
        three AES keys; that is all TorSocket needs to encrypt/decrypt messages.

    Returns:
        (circuit_id: str, K1: bytes, K2: bytes, K3: bytes, entry_sock: socket)

    Raises:
        RuntimeError — if the circuit setup handshake fails.
    """
    circuit_id = str(uuid.uuid4())
    K1 = get_random_bytes(16)
    K2 = get_random_bytes(16)
    K3 = get_random_bytes(16)

    exit_payload = make_setup_payload(exit_node['public_key'], {
        'key': base64.b64encode(K3).decode(),
        'dest_host': dest_host, 'dest_port': dest_port,
    })
    middle_payload = make_setup_payload(middle['public_key'], {
        'key': base64.b64encode(K2).decode(),
        'next_host': exit_node['host'], 'next_port': exit_node['port'],
        'forward_payload': exit_payload,
    })
    entry_payload = make_setup_payload(entry['public_key'], {
        'key': base64.b64encode(K1).decode(),
        'next_host': middle['host'], 'next_port': middle['port'],
        'forward_payload': middle_payload,
    })

    entry_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    entry_sock.connect((entry['host'], entry['port']))
    send_msg(entry_sock, {
        'type': 'CIRCUIT_SETUP',
        'circuit_id': circuit_id,
        'payload': entry_payload,
    })
    resp = json.loads(recv_msg(entry_sock))
    if resp.get('status') != 'ok':
        entry_sock.close()
        raise RuntimeError(f"Circuit setup failed: {resp}")

    print(f"  Route: you -> {entry['host']}:{entry['port']}"
          f" -> {middle['host']}:{middle['port']}"
          f" -> {exit_node['host']}:{exit_node['port']}"
          f" -> {dest_host}:{dest_port}")
    return circuit_id, K1, K2, K3, entry_sock


class TorSocket:
    """
    Drop-in socket replacement that routes traffic through a 3-hop onion circuit.

    send_msg() strips the 4-byte length frame before encrypting (the exit node's
    send_msg re-adds it when delivering to the chat server). After sending the RELAY,
    it reads the RELAY_RESPONSE, decrypts it, and re-adds the frame into an internal
    buffer so recv_msg(self) works normally.

    poll() sends an empty relay (no payload sent to dest). The exit node uses a short
    select() timeout and returns any server-pushed message that arrived in the meantime,
    or empty bytes if nothing was pending. This lets a background thread receive
    push notifications (IncomingMessage, UserJoined, etc.) while the main thread sends.

    A lock serialises all circuit access so both threads can share one circuit safely.
    """

    def __init__(self, circuit_id, K1, K2, K3, entry_sock):
        """
        Initialise the TorSocket with the circuit credentials from build_circuit.

        How it works:
            Stores the circuit_id, the three relay keys (K1/K2/K3), and the
            real TCP socket to the entry node (_entry). Initialises _buf as
            empty bytes — this is the internal receive buffer that sendall()
            writes to and recv() drains from. Creates a threading.Lock() to
            prevent concurrent calls to sendall() and poll() from interleaving
            their RELAY/RELAY_RESPONSE exchanges on the same circuit.

        Why it exists:
            All state needed for the TorSocket to operate is provided at
            construction time by build_circuit. After __init__, the TorSocket
            is ready to use as a drop-in replacement for a real socket.

        Args:
            circuit_id — UUID string matching what the relay nodes registered.
            K1, K2, K3 — 16-byte AES keys for entry, middle, exit layers.
            entry_sock  — the real TCP socket connected to the entry node.
        """
        self._cid   = circuit_id
        self._K1, self._K2, self._K3 = K1, K2, K3
        self._entry = entry_sock
        self._buf   = b''
        self._lock  = threading.Lock()

    def sendall(self, data: bytes):
        """
        Send one framed message through the Tor circuit, buffer the response.

        How it works:
            1. Strips the 4-byte length prefix that send_msg added — only the
               raw JSON payload travels the circuit. The exit node's send_msg
               re-adds the framing when delivering to the chat server, so the
               server receives a properly framed message.
            2. Under _lock (to prevent concurrent sendall/poll calls):
               a. Triple-encrypts the payload: K3 (innermost) → K2 → K1 (outermost).
               b. Sends a RELAY message to the entry node with the encrypted blob.
               c. Reads the RELAY_RESPONSE (blocks until the response arrives).
               d. Triple-decrypts the response data: K1 → K2 → K3.
               e. If the decrypted response is non-empty, re-adds the 4-byte
                  frame and appends to _buf so recv_msg(self) can read it later.

        Why the frame is stripped and re-added:
            send_msg(sock, data) adds a 4-byte frame. If we sent the full framed
            bytes through the circuit, the exit node's send_msg would add *another*
            4-byte frame to the already-framed data, causing double-framing. By
            stripping before encrypting and re-adding after decrypting, the
            framing is transparent to all callers of send_msg/recv_msg.

        Why it buffers the response in _buf:
            After sendall() the caller typically calls recv_msg(tor_sock) to
            read the server's response (Ack, RoomCreated, etc.). recv_msg calls
            recv() which drains _buf. This means the full send→receive round
            trip through the circuit happens inside sendall(), and recv() just
            reads back the buffered result.

        Args:
            data — the raw bytes that send_msg produced (4-byte frame + JSON).
        """
        # Strip the 4-byte frame added by send_msg — only raw JSON travels the
        # circuit. The exit node's send_msg re-adds framing for the chat server.
        payload = data[4:]
        with self._lock:
            enc = aes_encrypt(self._K3, payload)
            enc = aes_encrypt(self._K2, enc)
            enc = aes_encrypt(self._K1, enc)
            send_msg(self._entry, {
                'type': 'RELAY',
                'circuit_id': self._cid,
                'data': base64.b64encode(enc).decode(),
            })
            raw = recv_msg(self._entry)
            if raw is None:
                return
            resp = json.loads(raw)
            if 'error' in resp:
                raise RuntimeError(f"Circuit error: {resp['error']}")
            dec = base64.b64decode(resp['data'])
            dec = aes_decrypt(self._K1, dec)
            dec = aes_decrypt(self._K2, dec)
            dec = aes_decrypt(self._K3, dec)
            # Only buffer non-empty responses; empty means no message pending.
            if dec:
                self._buf += len(dec).to_bytes(4, 'big') + dec

    def recv(self, n: int) -> bytes:
        """
        Return up to n bytes from the internal buffer.

        How it works:
            Slices the first n bytes from _buf and advances _buf past them.
            If fewer than n bytes are available, returns whatever is in _buf
            (recv_msg loops until it has read 4 + length bytes, so it calls
            recv repeatedly until the buffer is large enough).

        Why it exists:
            recv_msg(tor_sock) calls tor_sock.recv(n) in a loop to drain the
            buffered response that sendall() wrote. This method provides the
            same interface as a real socket's recv() so recv_msg works without
            any modification.

        Args:
            n — maximum number of bytes to return.

        Returns:
            bytes — up to n bytes from _buf.
        """
        chunk = self._buf[:n]
        self._buf = self._buf[n:]
        return chunk

    def poll(self) -> dict | None:
        """
        Send an empty relay to check for any server-pushed messages.
        The exit node waits up to 0.5 s for data on the persistent dest socket,
        then returns whatever arrived (or empty if nothing).
        Returns a parsed message dict, or None if nothing was pending.
        Raises ConnectionError if the circuit is broken.

        How it works:
            Under _lock:
            1. Encrypts empty bytes (b'') in three layers — K3 → K2 → K1.
               PKCS7 pads b'' to 16 bytes before encrypting, so the ciphertext
               is 32 bytes (IV + one block). It is still valid AES-CBC output.
            2. Sends the encrypted empty payload as a RELAY message.
            3. The exit node decrypts K3, sees an empty plaintext, and skips
               send_msg to dest_sock. Instead it calls select.select(0.5s) on
               dest_sock to check whether the chat server has pushed anything.
            4. Exit re-encrypts whatever it found (or b'') and sends RELAY_RESPONSE.
            5. Client decrypts K1 → K2 → K3. If the result is empty, returns None
               (no messages pending). If non-empty, JSON-parses and returns it.

        Why this exists — the poll mechanism:
            A Tor circuit is fundamentally request/response — the client sends a
            RELAY and the exit node delivers it. The server cannot spontaneously
            push data back through the circuit. To receive push notifications
            (UserJoined, IncomingMessage, UserLeft from other users), the
            receiver thread continuously polls: each call blocks for up to 0.5 s
            inside the exit node's select(), giving ~2 polls/second with zero CPU
            spin. The _lock ensures that poll() and sendall() never overlap on the
            same circuit, which would corrupt the RELAY/RELAY_RESPONSE pairing.

        Returns:
            dict — the parsed pushed message, or None.

        Raises:
            ConnectionError — if the circuit has been broken.
        """
        with self._lock:
            enc = aes_encrypt(self._K3, b'')
            enc = aes_encrypt(self._K2, enc)
            enc = aes_encrypt(self._K1, enc)
            send_msg(self._entry, {
                'type': 'RELAY',
                'circuit_id': self._cid,
                'data': base64.b64encode(enc).decode(),
            })
            raw = recv_msg(self._entry)
            if raw is None:
                raise ConnectionError("Circuit disconnected")
            resp = json.loads(raw)
            if 'error' in resp:
                raise ConnectionError(f"Circuit error: {resp['error']}")
            dec = base64.b64decode(resp['data'])
            dec = aes_decrypt(self._K1, dec)
            dec = aes_decrypt(self._K2, dec)
            dec = aes_decrypt(self._K3, dec)
        if not dec:
            return None
        try:
            return json.loads(dec)
        except json.JSONDecodeError:
            return None

    def close(self):
        """
        Close the underlying entry-node socket.

        How it works:
            Calls self._entry.close() which triggers TCP connection teardown
            to the entry node. The entry node's handle_connection finally block
            then closes next_sock (to middle), cascading to exit and dest_sock.

        Why it exists:
            Provides the same close() interface as a real socket so callers can
            call conn.close() without knowing whether conn is a real socket or a
            TorSocket.
        """
        self._entry.close()


# ── Incoming message printer ──────────────────────────────────────────────────

def print_incoming(msg_type: str, data: dict):
    """
    Pretty-print one server-pushed message to stdout.

    How it works:
        A series of if/elif branches format each known message type as a
        human-readable string and prints it with a leading '\r' to overwrite
        any partial "you > " prompt the user may have typed. Unknown types
        are printed as raw JSON.

    Why it exists:
        Centralises display formatting for all incoming message types so that
        both receiver() (direct mode) and tor_receiver() (Tor mode) call this
        single function rather than duplicating the formatting logic.

    Args:
        msg_type — e.g. 'IncomingMessage', 'UserJoined'.
        data     — the message's data dict.
    """
    if msg_type == 'IncomingMessage':
        print(f"\r  {data['from_username']}: {data['message']}")
    elif msg_type == 'IncomingFile':
        print(f"\r  {data['from_username']} sent file: {data['filename']!r}  "
              f"({len(data.get('filedata', '')) * 3 // 4} bytes approx)")
    elif msg_type == 'UserJoined':
        print(f"\r  *** {data['username']} joined the room ***")
    elif msg_type == 'UserLeft':
        print(f"\r  *** {data['username']} left the room ***")
    elif msg_type == 'Stats':
        print(f"\r  [Stats] messages={data['total_messages']}  "
              f"files={data['total_files']}  "
              f"users={data['total_users']}  "
              f"rooms={data['total_rooms']}")
    elif msg_type == 'RoomLeft':
        print(f"\r  [Left room {data['room_code']}]")
    elif msg_type == 'Error':
        print(f"\r  [Error] {data['error_message']}")
    elif msg_type == 'Ack':
        pass  # Ack is silent — it just unblocks the exit node's relay handler
    else:
        print(f"\r  [Server] {msg_type}: {data}")


# ── Background receiver ───────────────────────────────────────────────────────

def receiver(sock, stop_event: threading.Event):
    """
    Continuously read and display messages from the server (direct mode).

    How it works:
        Loops calling recv_msg(sock) on the real socket. Blocks until a
        complete message arrives. When recv_msg returns None (server closed the
        connection), prints a disconnection notice and sets stop_event so the
        input_loop in the main thread exits too.
        JSON-decodes each message and calls print_incoming() to display it.
        Re-prints the "you > " prompt after each message so the cursor stays
        at a sensible position.

    Why it exists:
        In direct mode, the chat server pushes messages (IncomingMessage,
        UserJoined, etc.) at any time. Without a background thread continuously
        reading the socket, those messages would only be seen after the user
        sends something. Running this in a daemon thread means it is killed
        automatically when the process exits.

    Args:
        sock       — real TCP socket to the chat server.
        stop_event — set by this thread on disconnect, or by the main thread
                     on /quit, to signal mutual shutdown.
    """
    while not stop_event.is_set():
        raw = recv_msg(sock)
        if raw is None:
            if not stop_event.is_set():
                print("\n[!] Server disconnected.")
                stop_event.set()
            break
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue
        print_incoming(msg.get('type', ''), msg.get('data', {}))
        # Reprint the prompt so the cursor stays in a sensible place
        print("you > ", end='', flush=True)


# ── Tor background receiver ───────────────────────────────────────────────────

def tor_receiver(tor_sock: TorSocket, stop_event: threading.Event):
    """
    Poll the circuit for server-pushed messages in Tor mode.

    How it works:
        Loops calling tor_sock.poll(), which sends an empty relay and blocks
        for up to 0.5 s waiting for the exit node to return any pushed message.
        If poll() returns None, nothing was pending — loop immediately (the
        0.5 s timeout in the exit node already provides the necessary pacing,
        so no sleep() is needed here). If a message dict is returned, calls
        print_incoming() to display it.

        ConnectionError from poll() means the circuit broke — prints a notice
        and sets stop_event. Other exceptions exit the loop silently.

        The _lock inside TorSocket ensures this thread's poll() calls and the
        main thread's sendall() calls never interleave on the same circuit.

    Why it exists:
        The poll mechanism is the only way to receive server-pushed messages in
        Tor mode (because the circuit is request/response). This function is the
        consumer of that mechanism: it runs in a daemon thread, continuously
        asking the exit node "anything new?" so pushed messages appear promptly
        (~2/s) even while the user has not typed anything.

    Args:
        tor_sock   — the TorSocket wrapping the 3-hop circuit.
        stop_event — set on circuit failure or /quit to stop the loop.
    """
    while not stop_event.is_set():
        try:
            msg = tor_sock.poll()
        except ConnectionError:
            if not stop_event.is_set():
                print("\n[!] Circuit disconnected.")
                stop_event.set()
            break
        except Exception:
            break
        if msg is not None:
            msg_type = msg.get('type', '')
            if msg_type not in ('Ack',):
                print_incoming(msg_type, msg.get('data', {}))
                print("you > ", end='', flush=True)


# ── Setup (synchronous — before receiver starts) ──────────────────────────────

def setup(sock) -> tuple[str, str]:
    """
    Interactively create or join a room before the receiver thread starts.
    Returns (username, room_code).
    Handles the initial request/response synchronously so the receiver
    thread does not need to be running yet.

    How it works:
        Prompts the user to choose create (c) or join (j), then their username.
        For create: sends CreateRoom, reads the RoomCreated response, extracts
        and displays the room_code and initial member list.
        For join: additionally prompts for the room_code, sends JoinRoom, reads
        the RoomJoined response with the full member list.

        Both paths call recv_msg() synchronously — no background receiver is
        running yet. This is safe because setup() runs before start_receiver()
        is called in main(), so there is only one thread reading the socket.

        If the server sends an Error (e.g. room not found, username taken),
        the function prints the error and exits the process (sys.exit(1)).

    Why setup is synchronous:
        Starting the receiver thread before the room is established would cause
        it to immediately try to read messages from a socket that is mid-request,
        creating a race between the receiver thread reading the RoomCreated
        response and setup() trying to read it. By running setup first, we ensure
        the room state is fully established before the receiver competes.

    Returns:
        (username: str, room_code: str)
    """
    while True:
        choice = input("Create or join a room? [c/j]: ").strip().lower()
        if choice in ('c', 'j'):
            break
        print("  Please enter 'c' to create or 'j' to join.")

    username = ''
    while not username:
        username = input("Username: ").strip()

    if choice == 'c':
        send_to(sock, 'CreateRoom', {'my_username': username})
        raw = recv_msg(sock)
        if raw is None:
            print("[!] Server disconnected during setup.")
            sys.exit(1)
        resp = json.loads(raw)
        if resp['type'] == 'Error':
            print(f"[Error] {resp['data']['error_message']}")
            sys.exit(1)
        room_code = resp['data']['room_code']
        members   = resp['data']['users']
        print(f"  Room created: {room_code}")
        print(f"  Members: {', '.join(members)}")
        return username, room_code

    else:
        room_code = ''
        while not room_code:
            room_code = input("Room code: ").strip()
        send_to(sock, 'JoinRoom', {'room_code': room_code, 'my_username': username})
        raw = recv_msg(sock)
        if raw is None:
            print("[!] Server disconnected during setup.")
            sys.exit(1)
        resp = json.loads(raw)
        if resp['type'] == 'Error':
            print(f"[Error] {resp['data']['error_message']}")
            sys.exit(1)
        members = resp['data']['users']
        print(f"  Joined room: {room_code}")
        print(f"  Members: {', '.join(members)}")
        return username, room_code


# ── Main input loop ───────────────────────────────────────────────────────────

def input_loop(sock, stop_event: threading.Event):
    """
    Read commands from stdin and send them to the server (direct mode).

    How it works:
        Loops reading lines from stdin. Dispatches each line to the appropriate
        send_to() call:
            /quit   — breaks the loop (closes the session).
            /leave  — sends LeaveRoom; the server sends RoomLeft back.
            /stats  — sends GetStats; the server responds with Stats.
            /file p — reads the file at path p, base64-encodes it, sends
                      SendFile. Validates the path before reading.
            plain   — sends SendMessage with the typed text.
        Breaks on EOFError (stdin closed) or KeyboardInterrupt.
        Checks stop_event each iteration so the loop exits if the background
        receiver detected a disconnect.

    Why it exists:
        In direct mode, send_to() on the real socket is non-blocking from the
        point of view of message flow (the receiver thread handles incoming
        messages independently). This loop only needs to send; the receiver
        thread handles all incoming traffic.

    Args:
        sock       — real TCP socket (or TorSocket, but see tor_input_loop).
        stop_event — set by the receiver thread on disconnect, or by /quit.
    """
    print("  Type a message and press Enter.")
    print("  Commands: /leave  /stats  /file <path>  /quit\n")

    while not stop_event.is_set():
        try:
            line = input("you > ").strip()
        except (EOFError, KeyboardInterrupt):
            break

        if not line:
            continue

        if line == '/quit':
            break

        elif line == '/leave':
            send_to(sock, 'LeaveRoom', {})

        elif line == '/stats':
            send_to(sock, 'GetStats', {})

        elif line.startswith('/file '):
            path = line[6:].strip()
            if not os.path.isfile(path):
                print(f"  [!] File not found: {path!r}")
                continue
            try:
                with open(path, 'rb') as f:
                    filedata = base64.b64encode(f.read()).decode()
                filename = os.path.basename(path)
                send_to(sock, 'SendFile', {'filename': filename, 'filedata': filedata})
                print(f"  [Sent file {filename!r}]")
            except OSError as e:
                print(f"  [!] Could not read file: {e}")

        elif line.startswith('/'):
            print(f"  Unknown command: {line!r}")

        else:
            send_to(sock, 'SendMessage', {'message': line})


# ── Tor input loop ────────────────────────────────────────────────────────────

def tor_input_loop(tor_sock: TorSocket, stop_event: threading.Event):
    """
    Read commands from stdin and send them through the circuit (Tor mode).

    How it works:
        Identical command parsing to input_loop. The key difference is what
        happens after each send:
            - send_to(tor_sock, ...) calls tor_sock.sendall(), which does the
              full circuit round-trip (encrypt → RELAY → wait RELAY_RESPONSE
              → decrypt → buffer) synchronously under _lock.
            - After sending, reads the one direct response (Ack, Stats,
              RoomLeft, etc.) from the buffer with recv_msg(tor_sock).
            - Ack is silently discarded; other message types are printed via
              print_incoming().
        Push notifications (from other users) are delivered by the
        tor_receiver background thread via separate poll() calls.

    Why a separate function from input_loop:
        In Tor mode, every send produces a direct response in _buf (put there
        by sendall()). The input loop must read that response immediately after
        each send to keep the buffer from accumulating stale responses.
        In direct mode, the background receiver reads all responses — the send
        loop does not need to read at all. Having two separate loops avoids
        entangling these two very different patterns with a flag.

    Args:
        tor_sock   — TorSocket wrapping the 3-hop circuit.
        stop_event — set by the receiver thread on circuit failure or /quit.
    """
    print("  Type a message and press Enter.")
    print("  Commands: /leave  /stats  /file <path>  /quit\n")

    while not stop_event.is_set():
        try:
            line = input("you > ").strip()
        except (EOFError, KeyboardInterrupt):
            break

        if not line:
            continue

        if line == '/quit':
            break

        sent = True

        if line == '/leave':
            send_to(tor_sock, 'LeaveRoom', {})

        elif line == '/stats':
            send_to(tor_sock, 'GetStats', {})

        elif line.startswith('/file '):
            path = line[6:].strip()
            if not os.path.isfile(path):
                print(f"  [!] File not found: {path!r}")
                sent = False
            else:
                try:
                    with open(path, 'rb') as f:
                        filedata = base64.b64encode(f.read()).decode()
                    filename = os.path.basename(path)
                    send_to(tor_sock, 'SendFile', {'filename': filename, 'filedata': filedata})
                    print(f"  [Sent file {filename!r}]")
                except OSError as e:
                    print(f"  [!] Could not read file: {e}")
                    sent = False

        elif line.startswith('/'):
            print(f"  Unknown command: {line!r}")
            sent = False

        else:
            send_to(tor_sock, 'SendMessage', {'message': line})

        if not sent:
            continue

        # Read the one response every command receives through the circuit
        raw = recv_msg(tor_sock)
        if raw is None:
            print("\n[!] Circuit disconnected.")
            break
        msg = json.loads(raw)
        msg_type = msg.get('type', '')
        if msg_type != 'Ack':
            print_incoming(msg_type, msg.get('data', {}))


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    """
    Parse arguments, connect (direct or Tor), set up a room, and run the loop.

    How it works:
        In direct mode:
            Connects a plain TCP socket to the chat server. Uses it as conn.
            Starts a receiver() daemon thread for push notifications.
            Runs input_loop() on the main thread.

        In Tor mode:
            Queries the directory server for nodes.
            Picks one of each type at random.
            Calls build_circuit() to negotiate keys and establish the 3-hop path.
            Wraps the result in TorSocket as conn.
            Starts tor_receiver() daemon thread for push notifications via poll.
            Runs tor_input_loop() on the main thread.

        In both modes:
            Calls setup() synchronously (before the receiver thread starts) to
            create or join a room.
            Uses a shared stop_event to signal shutdown between threads.
            The finally block sets stop_event (so the receiver thread exits) and
            closes conn (triggering TCP teardown or circuit teardown).

    Why it exists:
        Entry point for the entire CLI client. All branching between direct and
        Tor mode happens here, so the rest of the code (setup, input loops,
        receiver threads) can be written in terms of conn (socket or TorSocket)
        without caring which mode is active.
    """
    parser = argparse.ArgumentParser(description='CLI Chat Client')
    parser.add_argument('--host', default='127.0.0.1', help='Chat server host')
    parser.add_argument('--port', type=int, default=8001, help='Chat server port')
    parser.add_argument('--tor', action='store_true', help='Route through onion circuit')
    parser.add_argument('--dir-host', default='127.0.0.1', help='Directory server host (Tor mode)')
    parser.add_argument('--dir-port', type=int, default=8000, help='Directory server port (Tor mode)')
    args = parser.parse_args()

    if args.tor:
        print("[Tor] Querying directory server...")
        try:
            nodes = get_nodes(args.dir_host, args.dir_port)
        except Exception as e:
            print(f"[!] Could not reach directory server: {e}")
            sys.exit(1)
        print(f"[Tor] Found {len(nodes)} node(s): {[n['node_type'] for n in nodes]}")

        try:
            entry, middle, exit_node = pick_nodes(nodes)
        except RuntimeError as e:
            print(f"[!] {e}")
            sys.exit(1)

        print("[Tor] Building circuit...")
        try:
            circuit_id, K1, K2, K3, entry_sock = build_circuit(
                entry, middle, exit_node, args.host, args.port
            )
        except Exception as e:
            print(f"[!] Circuit setup failed: {e}")
            sys.exit(1)
        print("[Tor] Circuit ready.\n")

        conn = TorSocket(circuit_id, K1, K2, K3, entry_sock)
        loop_fn = tor_input_loop
    else:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.connect((args.host, args.port))
        except ConnectionRefusedError:
            print(f"[!] Could not connect to chat server at {args.host}:{args.port}")
            sys.exit(1)
        print(f"Connected to chat server at {args.host}:{args.port}\n")
        conn = sock
        loop_fn = None  # use normal input_loop with receiver thread

    stop_event = threading.Event()
    try:
        username, room_code = setup(conn)

        if loop_fn:
            recv_thread = threading.Thread(
                target=tor_receiver, args=(conn, stop_event), daemon=True
            )
            recv_thread.start()
            loop_fn(conn, stop_event)
        else:
            recv_thread = threading.Thread(target=receiver, args=(conn, stop_event), daemon=True)
            recv_thread.start()
            input_loop(conn, stop_event)
    except KeyboardInterrupt:
        print("\n[!] Interrupted.")
    finally:
        stop_event.set()
        try:
            conn.close()
        except Exception:
            pass
        print("Disconnected.")


if __name__ == '__main__':
    main()
