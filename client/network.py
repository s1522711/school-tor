"""
Network layer for the GUI chat client.

Provides wire helpers, Tor circuit building, TorSocket, Connection,
and convenience connect/fetch functions.
"""

import select
import socket
import json
import threading
import base64
import uuid
import random

from Crypto.PublicKey import RSA
from Crypto.Cipher import AES, PKCS1_OAEP
from Crypto.Random import get_random_bytes
from Crypto.Util.Padding import pad, unpad


# ── Wire helpers ──────────────────────────────────────────────────────────────

def recv_msg(sock):
    """
    Read one complete length-prefixed message from a socket or TorSocket.

    How it works:
        Reads a 4-byte big-endian header to learn the payload length, then
        reads exactly that many payload bytes. Both reads loop because a single
        recv() call on a TCP stream (or TorSocket._buf drain) can return fewer
        bytes than requested. Returns None on EOF or circuit disconnect.

    Why it exists:
        Used by Connection.recv_one() and the direct-mode receiver. Accepting
        either a real socket or a TorSocket makes the caller agnostic to the
        underlying transport — TorSocket.recv() drains _buf just like a real
        socket would return bytes from the kernel buffer.

    Returns:
        dict (parsed) — actually returns raw bytes; callers parse JSON.
        None          — on disconnect / circuit closed.
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
    Send data with a 4-byte big-endian length prefix over sock.

    How it works:
        Serialises dict → JSON bytes, str → UTF-8 bytes. Prepends 4-byte
        big-endian length and calls sendall(). For a TorSocket, sendall()
        triggers the full circuit round-trip (encrypt → RELAY → decrypt →
        buffer) transparently.

    Why it exists:
        Pairs with recv_msg to implement the system-wide framing protocol.
        Using sendall() (not send()) prevents partial writes when the OS send
        buffer is momentarily full.
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
        Wraps msg_type and data in the standard chat envelope and calls
        send_msg. Works transparently for both real sockets and TorSocket.

    Why it exists:
        Avoids duplicating the envelope construction at every call site.
        Called by Connection.send_to() and directly in convenience functions.

    Args:
        sock     — destination socket or TorSocket.
        msg_type — e.g. 'CreateRoom', 'SendMessage'.
        data     — the payload dict.
    """
    send_msg(sock, {'type': msg_type, 'data': data})


# ── AES helpers ───────────────────────────────────────────────────────────────

def aes_encrypt(key: bytes, plaintext: bytes) -> bytes:
    """
    AES-128-CBC encrypt and prepend a fresh random 16-byte IV.

    How it works:
        Generates a cryptographically random IV, pads plaintext to a 16-byte
        multiple with PKCS7, encrypts with AES-CBC, and returns iv + ciphertext.
        A new IV per call means identical plaintexts produce different ciphertexts,
        preventing analysis across repeated relay messages.

    Why it exists:
        Called in TorSocket.sendall() and TorSocket.poll() to apply each AES
        layer before sending a RELAY. Also used in make_setup_payload to encrypt
        the routing dict with the ephemeral setup_key.

    Args:
        key       — 16-byte AES-128 key (K1, K2, K3, or setup_key).
        plaintext — bytes to encrypt (may be empty b'' for poll).

    Returns:
        bytes — iv (16 B) + ciphertext (multiple of 16 B).
    """
    iv = get_random_bytes(16)
    cipher = AES.new(key, AES.MODE_CBC, iv)
    return iv + cipher.encrypt(pad(plaintext, AES.block_size))


def aes_decrypt(key: bytes, ciphertext: bytes) -> bytes:
    """
    AES-128-CBC decrypt, reading the IV from the first 16 bytes.

    How it works:
        Splits the input at byte 16 (iv vs ciphertext), reconstructs the
        AES-CBC cipher, decrypts, and removes PKCS7 padding.

    Why it exists:
        Inverse of aes_encrypt. Called in TorSocket to peel the three AES
        layers off a RELAY_RESPONSE in order K1 → K2 → K3 (outermost first),
        the reverse of the K3 → K2 → K1 wrapping order used when sending.

    Args:
        key        — 16-byte AES-128 key.
        ciphertext — iv + ciphertext as produced by aes_encrypt.

    Returns:
        bytes — original plaintext.
    """
    iv, ct = ciphertext[:16], ciphertext[16:]
    cipher = AES.new(key, AES.MODE_CBC, iv)
    return unpad(cipher.decrypt(ct), AES.block_size)


# ── Tor circuit ───────────────────────────────────────────────────────────────

def get_nodes(dir_host: str, dir_port: int) -> list:
    """
    Query the directory server for the list of registered relay nodes.

    How it works:
        Opens a short-lived TCP connection to the directory server, sends
        GET_NODES, parses the JSON response, closes the socket, and returns
        the list. Each node dict contains: {node_type, host, port, public_key}.

    Why it exists:
        The directory server is the only bootstrap point that knows which
        relay nodes are currently online and their RSA public keys. Without
        the keys, make_setup_payload cannot encrypt the CIRCUIT_SETUP payload
        for each node.

    Returns:
        list[dict] — all registered relay nodes.
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
        Groups the flat node list by 'node_type', then calls random.choice()
        on each group. Raises RuntimeError if any type is absent so the caller
        receives a clear error rather than a KeyError.

    Why it exists:
        Randomising selection makes traffic analysis harder — a network-level
        adversary watching some nodes cannot predict which circuit will be used.

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
    return (
        random.choice(by_type['entry']),
        random.choice(by_type['middle']),
        random.choice(by_type['exit']),
    )


def make_setup_payload(pub_pem: str, inner: dict) -> dict:
    """
    Hybrid-encrypt a routing dict for one relay node.

    How it works:
        1. Generates a random 16-byte setup_key (ephemeral AES key).
        2. RSA-OAEP encrypts setup_key with pub_pem (the node's public key).
           Only the node with the matching RSA private key can recover it.
        3. AES-CBC encrypts JSON(inner) with setup_key.
        4. Returns both ciphertexts as base64 strings.

        RSA-2048-OAEP can encrypt at most ~214 bytes, but inner may be many
        kilobytes (it embeds the next node's payload). The hybrid approach
        solves this: only the 16-byte setup_key goes through RSA.

    Why it exists:
        Called three times in build_circuit (once per hop, exit → middle → entry)
        to produce the nested onion payload. The nesting is what gives each node
        just enough information (its own key + the next hop address + the opaque
        blob for the next node) without revealing anything else.

    Args:
        pub_pem — PEM-encoded RSA-2048 public key of the target node.
        inner   — dict of routing instructions for this node.

    Returns:
        dict — {'encrypted_key': base64, 'encrypted_data': base64}.
    """
    setup_key = get_random_bytes(16)
    pub = RSA.import_key(pub_pem)
    enc_key = PKCS1_OAEP.new(pub).encrypt(setup_key)
    enc_data = aes_encrypt(setup_key, json.dumps(inner).encode())
    return {
        'encrypted_key':  base64.b64encode(enc_key).decode(),
        'encrypted_data': base64.b64encode(enc_data).decode(),
    }


def build_circuit(entry, middle, exit_node, dest_host: str, dest_port: int):
    """
    Establish a 3-hop Tor circuit and return the circuit credentials.

    How it works:
        1. Generates a UUID circuit_id and three 16-byte relay keys (K1, K2, K3).
        2. Builds nested hybrid-encrypted payloads from the inside out:
               exit_payload   — K3 + dest_host:dest_port, for exit node only.
               middle_payload — K2 + exit addr + exit_payload, for middle only.
               entry_payload  — K1 + middle addr + middle_payload, for entry only.
        3. Connects TCP to the entry node and sends CIRCUIT_SETUP.
           Entry decrypts its layer, forwards middle_payload to middle, middle
           forwards exit_payload to exit. Exit connects to dest_host:dest_port
           and sends 'ok' back. The 'ok' bubbles entry → client.
        4. Returns the five values TorSocket needs to operate.

    Why it exists:
        After this function, the caller has a single TCP socket to the entry
        node and three relay keys. Every subsequent RELAY message is sent and
        received through that one socket using those keys. The circuit persists
        for the session lifetime; no per-message handshake is needed.

    Returns:
        (circuit_id: str, K1: bytes, K2: bytes, K3: bytes, entry_sock: socket)

    Raises:
        RuntimeError — if the handshake with the entry node fails.
    """
    circuit_id = str(uuid.uuid4())
    K1 = get_random_bytes(16)
    K2 = get_random_bytes(16)
    K3 = get_random_bytes(16)

    exit_payload = make_setup_payload(exit_node['public_key'], {
        'key': base64.b64encode(K3).decode(),
        'dest_host': dest_host,
        'dest_port': dest_port,
    })
    middle_payload = make_setup_payload(middle['public_key'], {
        'key': base64.b64encode(K2).decode(),
        'next_host': exit_node['host'],
        'next_port': exit_node['port'],
        'forward_payload': exit_payload,
    })
    entry_payload = make_setup_payload(entry['public_key'], {
        'key': base64.b64encode(K1).decode(),
        'next_host': middle['host'],
        'next_port': middle['port'],
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
    return circuit_id, K1, K2, K3, entry_sock


# ── TorSocket ─────────────────────────────────────────────────────────────────

class TorSocket:
    """
    Drop-in socket replacement routing traffic through a 3-hop onion circuit.

    sendall() strips the 4-byte frame added by send_msg, triple-encrypts the
    payload, sends a RELAY message, reads the RELAY_RESPONSE, and decrypts the
    response. Any non-empty response is buffered so recv_msg(self) works.

    poll() sends an empty relay (no payload delivered to the chat server).
    The exit node's select() waits up to 0.5 s for pushed data, so the polling
    rate is ~2/s with no extra sleep needed.

    A lock serialises sendall and poll so both threads share one circuit safely.
    """

    def __init__(self, circuit_id, K1, K2, K3, entry_sock):
        """
        Store circuit credentials from build_circuit.

        How it works:
            Saves all five values returned by build_circuit. Initialises _buf
            as empty bytes (the receive buffer) and _lock as a threading.Lock.
            The lock prevents concurrent sendall (main thread) and poll (receiver
            thread) calls from interleaving their RELAY/RELAY_RESPONSE exchanges
            on the same entry_sock.

        Why it exists:
            Construction is separate from build_circuit so Connection can own
            the TorSocket without knowing its internals. After __init__ the
            object is ready to use as a drop-in socket replacement.

        Args:
            circuit_id — UUID string matching what the relay nodes registered.
            K1, K2, K3 — 16-byte AES relay keys for entry, middle, exit.
            entry_sock  — open TCP socket to the entry node.
        """
        self._cid = circuit_id
        self._K1, self._K2, self._K3 = K1, K2, K3
        self._entry = entry_sock
        self._buf   = b''
        self._lock  = threading.Lock()

    def sendall(self, data: bytes):
        """
        Send one framed message through the circuit and buffer the response.

        How it works:
            1. Strips the 4-byte length prefix (data[4:]) — send_msg adds the
               frame, but only the raw JSON payload should travel the circuit.
               The exit node's send_msg re-adds framing for the chat server.
            2. Under _lock:
               a. Triple-encrypts: K3 → K2 → K1 (innermost to outermost).
               b. Sends RELAY to entry node.
               c. Reads RELAY_RESPONSE (blocks until round-trip completes).
               d. Triple-decrypts: K1 → K2 → K3.
               e. If response is non-empty, prepends 4-byte frame and appends
                  to _buf so recv_msg(self) can drain it.

        Why the frame is stripped then re-added:
            Without stripping, the exit node's send_msg would add a *second*
            4-byte frame to the already-framed data, causing the chat server's
            recv_msg to misread the message length. Stripping before encrypting
            and re-adding after decrypting makes the framing transparent.

        Args:
            data — the framed bytes that send_msg produced (4B header + JSON).
        """
        payload = data[4:]  # strip frame; exit node's send_msg re-adds it
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
            if dec:
                self._buf += len(dec).to_bytes(4, 'big') + dec

    def recv(self, n: int) -> bytes:
        """
        Return up to n bytes from the internal receive buffer.

        How it works:
            Slices the first n bytes from _buf and advances _buf past them.
            If fewer than n bytes are in _buf, returns whatever is available.
            recv_msg() loops calling recv() until it has read all 4 + length
            bytes, so partial returns are handled correctly.

        Why it exists:
            recv_msg(tor_sock) calls tor_sock.recv(n) exactly as it would call
            socket.recv(n) on a real socket. This method makes TorSocket a
            drop-in replacement so recv_msg needs no Tor-specific code.

        Args:
            n — maximum bytes to return.

        Returns:
            bytes — up to n bytes from _buf (may be fewer if _buf is small).
        """
        chunk = self._buf[:n]
        self._buf = self._buf[n:]
        return chunk

    def poll(self) -> dict | None:
        """
        Send an empty relay to check for server-pushed messages.
        Returns a parsed message dict or None if nothing was pending.
        Raises ConnectionError if the circuit is broken.

        How it works:
            Under _lock:
            1. Triple-encrypts empty bytes (b''): K3 → K2 → K1. PKCS7 pads
               b'' to 16 bytes before encryption, so the result is a valid
               AES-CBC ciphertext (32 bytes: IV + one block).
            2. Sends RELAY with the encrypted empty payload.
            3. The exit node decrypts, sees empty plaintext, skips send_msg
               to dest_sock, and instead calls select.select(dest_sock, 0.5s).
               If data is ready within 0.5 s, it reads one message and returns
               it encrypted. Otherwise returns empty bytes.
            4. Client decrypts K1 → K2 → K3. If empty → returns None.
               If non-empty → JSON-parses and returns the message dict.

        Why the poll mechanism exists:
            A Tor circuit is inherently request/response. The server cannot
            push messages spontaneously through the circuit. To receive
            IncomingMessage, UserJoined, etc. in real time, the receiver thread
            calls poll() in a tight loop. The exit node's 0.5s select() provides
            natural pacing (~2 polls/second) without any sleep() needed here.
            The _lock ensures poll() and sendall() never overlap on entry_sock.

        Returns:
            dict — a parsed pushed message, or None if nothing pending.

        Raises:
            ConnectionError — if the entry socket closed (circuit dead).
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
        Close the underlying entry-node TCP socket.

        How it works:
            Calls self._entry.close() which triggers TCP FIN and causes the
            entry node's recv_msg to return None, triggering the cascade teardown
            (entry closes next_sock → middle closes next_sock → exit closes
            dest_sock).

        Why it exists:
            Provides the socket.close() interface so Connection.close() can call
            self._sock.close() without knowing whether _sock is a real socket or
            a TorSocket.
        """
        self._entry.close()


# ── Connection ────────────────────────────────────────────────────────────────

class Connection:
    """
    Wraps a raw socket (direct mode) or TorSocket (Tor mode).

    A single persistent socket is used for the entire session — stats,
    room setup, and in-room messaging all share the same connection.

    start_receiver(on_message, on_disconnect)
        Launch a background thread delivering pushed messages. The direct-mode
        receiver uses select() with a 0.5 s timeout so it can be cleanly
        interrupted without closing the socket.

    stop_receiver()
        Signal the receiver thread to stop and wait for it to exit (up to 2 s).
        The socket stays open; send_to / recv_one can be used again afterwards.

    close()
        Stop the receiver and close the underlying socket.
    """

    def __init__(self, sock, is_tor: bool = False):
        """
        Wrap sock in a Connection.

        How it works:
            Stores the socket (real or TorSocket) and the is_tor flag. Creates
            a threading.Event and a thread slot, both initially inactive.

        Why it exists:
            HomeScreen and ChatScreen interact with Connection rather than with
            a raw socket or TorSocket. This indirection means neither screen
            needs to know which transport is active — they both call send_to(),
            recv_one(), start_receiver(), etc. on the same API.

        Args:
            sock   — real socket or TorSocket.
            is_tor — True if sock is a TorSocket (used by UI for the status label).
        """
        self._sock        = sock
        self._is_tor      = is_tor
        self._recv_stop   = threading.Event()
        self._recv_thread: threading.Thread | None = None

    @property
    def is_tor(self) -> bool:
        """
        True if this connection routes traffic through a Tor circuit.

        Why it exists:
            HomeScreen displays "Connected (Tor)" or "Connected (Direct)" in
            the status bar. Without this property, the UI would have to
            inspect the type of _sock directly (breaking encapsulation).
        """
        return self._is_tor

    def send_to(self, msg_type: str, data: dict):
        """
        Send one typed chat-protocol message.

        How it works:
            Delegates to the module-level send_to() with self._sock. Callers
            do not need to know whether the underlying socket is a real socket
            or a TorSocket.

        Why it exists:
            Single call site for sending from ChatScreen and HomeScreen. No
            exception handling here — the caller (always a worker thread in the
            GUI) wraps the call in try/except.

        Args:
            msg_type — e.g. 'SendMessage', 'LeaveRoom'.
            data     — the payload dict.
        """
        send_to(self._sock, msg_type, data)

    def recv_one(self) -> dict | None:
        """
        Synchronously receive and JSON-parse one message.

        How it works:
            Calls recv_msg(self._sock) (which blocks until a complete message
            arrives) and JSON-parses the result. Returns None on disconnect or
            JSON decode error.

        Why it exists:
            Used by HomeScreen._fetch_stats() and the create/join workers, which
            need to read exactly one response synchronously. These callers always
            call recv_one() when no receiver thread is running (either before
            start_receiver(), or after stop_receiver()), so there is no
            concurrent read on the socket.

        Returns:
            dict — the parsed message, or None.
        """
        raw = recv_msg(self._sock)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def start_receiver(self, on_message, on_disconnect):
        """
        Start a background daemon thread to deliver incoming messages.

        How it works:
            Creates a fresh threading.Event (_recv_stop) so stop_receiver()
            works correctly even if called multiple times. Chooses the right
            receiver implementation (_tor_receiver or _direct_receiver) based
            on is_tor. Starts a daemon thread — daemon threads are killed
            automatically when the main thread exits, so no cleanup is needed
            on process exit.

        Why a fresh Event each call:
            If stop_receiver() was called previously (e.g. when leaving a room),
            the old event is already set. Creating a new event resets the stop
            condition so the new thread starts running immediately.

        Args:
            on_message    — callback(msg_type: str, data: dict) called on the
                            main thread (via self.after(0, ...)) for each message.
            on_disconnect — callback() called when the socket or circuit closes
                            unexpectedly.
        """
        self._recv_stop = threading.Event()
        target = self._tor_receiver if self._is_tor else self._direct_receiver
        self._recv_thread = threading.Thread(
            target=target, args=(on_message, on_disconnect), daemon=True)
        self._recv_thread.start()

    def stop_receiver(self):
        """
        Signal the receiver thread to stop and wait up to 2 seconds.

        How it works:
            Sets _recv_stop (the thread checks this flag on each iteration).
            Calls thread.join(timeout=2.0) to wait for a clean exit. The
            timeout prevents the GUI from freezing indefinitely if the thread
            is stuck in a slow poll. Does NOT close the socket — the caller
            may reuse the Connection for further sends/receives.

        Why the socket stays open:
            HomeScreen.destroy() calls stop_receiver() when switching to
            ChatScreen, but the same Connection is passed to ChatScreen and
            reused there. Closing the socket here would invalidate the
            Connection for all future use.
        """
        self._recv_stop.set()
        if self._recv_thread and self._recv_thread.is_alive():
            self._recv_thread.join(timeout=2.0)
        self._recv_thread = None

    def _direct_receiver(self, on_message, on_disconnect):
        """
        Background receiver for direct-mode connections.

        How it works:
            Loops until _recv_stop is set. Uses select.select() with a 0.5 s
            timeout before each recv_msg call. The select() serves two purposes:
            1. Non-blocking check: if no data is ready, the loop re-checks
               _recv_stop without blocking on recv_msg.
            2. Fast response to stop_receiver(): within 0.5 s of _recv_stop
               being set, this thread exits cleanly — without closing the socket.

            On recv_msg returning None (server disconnected): calls on_disconnect
            only if _recv_stop is not already set (i.e., the disconnect was
            unexpected, not triggered by stop_receiver). This prevents a spurious
            "Disconnected" error when the user intentionally leaves a room.

            Filters out Ack messages — they are only meaningful at the send layer
            and should not be delivered to the UI.

        Why select() instead of closing the socket:
            The alternative to select() would be to close the socket when
            stop_receiver() is called, which would unblock recv_msg immediately.
            But closing the socket destroys the connection, which HomeScreen needs
            to keep alive for stats queries after the user leaves the chat room.

        Args:
            on_message    — UI callback for non-Ack messages.
            on_disconnect — UI callback when the server closes the connection.
        """
        while not self._recv_stop.is_set():
            try:
                readable, _, _ = select.select([self._sock], [], [], 0.5)
            except Exception:
                break
            if not readable:
                continue
            raw = recv_msg(self._sock)
            if raw is None:
                if not self._recv_stop.is_set():
                    on_disconnect()
                break
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            msg_type = msg.get('type', '')
            if msg_type != 'Ack':
                on_message(msg_type, msg.get('data', {}))

    def _tor_receiver(self, on_message, on_disconnect):
        """
        Background receiver for Tor-mode connections.

        How it works:
            Loops until _recv_stop is set, calling self._sock.poll() on each
            iteration. poll() encrypts an empty payload, sends it as RELAY,
            and blocks up to 0.5 s in the exit node's select() for any pushed
            message. This 0.5 s timeout provides natural pacing (~2 polls/s)
            without any sleep() needed in this function.

            ConnectionError from poll() means the circuit closed — calls
            on_disconnect() if the stop wasn't intentional. Other exceptions
            (e.g. JSON errors inside poll) exit the loop silently.

            Filters out Ack messages (same as _direct_receiver).

        Why a separate method from _direct_receiver:
            Tor mode uses poll() instead of select + recv_msg because the
            entire circuit round-trip (encrypt → RELAY → wait → decrypt) must
            happen atomically under _lock. There is no way to use select() on
            a TorSocket because the data lives in _buf, not on a file descriptor.

        Args:
            on_message    — UI callback for non-Ack messages.
            on_disconnect — UI callback on circuit failure.
        """
        while not self._recv_stop.is_set():
            try:
                msg = self._sock.poll()
            except ConnectionError:
                if not self._recv_stop.is_set():
                    on_disconnect()
                break
            except Exception:
                break
            if msg is not None:
                msg_type = msg.get('type', '')
                if msg_type != 'Ack':
                    on_message(msg_type, msg.get('data', {}))

    def close(self):
        """
        Stop the receiver thread and close the underlying socket.

        How it works:
            Calls stop_receiver() first (sets the stop event and joins the
            thread), then calls self._sock.close(). For a real socket, close()
            triggers TCP teardown. For a TorSocket, close() closes the entry
            node socket, which cascades through the relay chain.

        Why it exists:
            Called by App._on_close() when the window is closed, and by
            HomeScreen._manual_connect() when the user clicks "Connect" again.
            Guarantees that neither the thread nor the socket lingers after
            the caller no longer needs the connection.
        """
        self.stop_receiver()
        try:
            self._sock.close()
        except Exception:
            pass


# ── Convenience functions ─────────────────────────────────────────────────────

def connect_direct(host: str, port: int) -> Connection:
    """
    Open a direct TCP connection to the chat server and wrap it in Connection.

    How it works:
        Creates a standard AF_INET/SOCK_STREAM socket, calls connect(), and
        wraps the socket in Connection(is_tor=False).

    Why it exists:
        Provides a one-liner API for HomeScreen._manual_connect() so it doesn't
        have to construct the socket and Connection separately.

    Args:
        host — chat server IP.
        port — chat server port.

    Returns:
        Connection — ready for send_to / start_receiver.

    Raises:
        ConnectionRefusedError / OSError — if the connection fails.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect((host, port))
    return Connection(s, is_tor=False)


def connect_tor(dir_host: str, dir_port: int, dest_host: str, dest_port: int) -> Connection:
    """
    Build a Tor circuit to the chat server and wrap it in Connection.

    How it works:
        1. get_nodes() queries the directory server.
        2. pick_nodes() selects one of each type randomly.
        3. build_circuit() negotiates keys and establishes the 3-hop path.
        4. Wraps the resulting TorSocket in Connection(is_tor=True).

    Why it exists:
        Provides a one-liner API matching connect_direct() so HomeScreen can
        switch between modes with a single if/else:
            conn = connect_tor(...) if use_tor else connect_direct(...)

    Args:
        dir_host  — directory server IP.
        dir_port  — directory server port.
        dest_host — chat server IP (the Tor circuit destination).
        dest_port — chat server port.

    Returns:
        Connection — backed by TorSocket, ready for use.

    Raises:
        RuntimeError — if any node type is missing or circuit setup fails.
    """
    nodes = get_nodes(dir_host, dir_port)
    entry, middle, exit_node = pick_nodes(nodes)
    circuit_id, K1, K2, K3, entry_sock = build_circuit(
        entry, middle, exit_node, dest_host, dest_port
    )
    tor_sock = TorSocket(circuit_id, K1, K2, K3, entry_sock)
    return Connection(tor_sock, is_tor=True)
