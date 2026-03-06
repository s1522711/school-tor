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
    send_msg(sock, {'type': msg_type, 'data': data})


# ── AES helpers ───────────────────────────────────────────────────────────────

def aes_encrypt(key: bytes, plaintext: bytes) -> bytes:
    iv = get_random_bytes(16)
    cipher = AES.new(key, AES.MODE_CBC, iv)
    return iv + cipher.encrypt(pad(plaintext, AES.block_size))


def aes_decrypt(key: bytes, ciphertext: bytes) -> bytes:
    iv, ct = ciphertext[:16], ciphertext[16:]
    cipher = AES.new(key, AES.MODE_CBC, iv)
    return unpad(cipher.decrypt(ct), AES.block_size)


# ── Tor circuit ───────────────────────────────────────────────────────────────

def get_nodes(dir_host: str, dir_port: int) -> list:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect((dir_host, dir_port))
    send_msg(s, {'type': 'GET_NODES'})
    resp = json.loads(recv_msg(s))
    s.close()
    return resp['nodes']


def pick_nodes(nodes: list) -> tuple:
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
    setup_key = get_random_bytes(16)
    pub = RSA.import_key(pub_pem)
    enc_key = PKCS1_OAEP.new(pub).encrypt(setup_key)
    enc_data = aes_encrypt(setup_key, json.dumps(inner).encode())
    return {
        'encrypted_key':  base64.b64encode(enc_key).decode(),
        'encrypted_data': base64.b64encode(enc_data).decode(),
    }


def build_circuit(entry, middle, exit_node, dest_host: str, dest_port: int):
    """Returns (circuit_id, K1, K2, K3, entry_sock)."""
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
        self._cid = circuit_id
        self._K1, self._K2, self._K3 = K1, K2, K3
        self._entry = entry_sock
        self._buf   = b''
        self._lock  = threading.Lock()

    def sendall(self, data: bytes):
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
        chunk = self._buf[:n]
        self._buf = self._buf[n:]
        return chunk

    def poll(self) -> dict | None:
        """
        Send an empty relay to check for server-pushed messages.
        Returns a parsed message dict or None if nothing was pending.
        Raises ConnectionError if the circuit is broken.
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
        self._sock        = sock
        self._is_tor      = is_tor
        self._recv_stop   = threading.Event()
        self._recv_thread: threading.Thread | None = None

    @property
    def is_tor(self) -> bool:
        return self._is_tor

    def send_to(self, msg_type: str, data: dict):
        send_to(self._sock, msg_type, data)

    def recv_one(self) -> dict | None:
        """Synchronous receive — only safe when no receiver thread is running."""
        raw = recv_msg(self._sock)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def start_receiver(self, on_message, on_disconnect):
        """Start the background receiver. Creates a fresh stop-event each call."""
        self._recv_stop = threading.Event()
        target = self._tor_receiver if self._is_tor else self._direct_receiver
        self._recv_thread = threading.Thread(
            target=target, args=(on_message, on_disconnect), daemon=True)
        self._recv_thread.start()

    def stop_receiver(self):
        """
        Stop the background receiver thread without closing the socket.
        Blocks until the thread exits (max 2 s).
        """
        self._recv_stop.set()
        if self._recv_thread and self._recv_thread.is_alive():
            self._recv_thread.join(timeout=2.0)
        self._recv_thread = None

    def _direct_receiver(self, on_message, on_disconnect):
        """
        Uses select() with a 0.5 s timeout so stop_receiver() takes effect
        quickly without having to close the socket.
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
        self.stop_receiver()
        try:
            self._sock.close()
        except Exception:
            pass


# ── Convenience functions ─────────────────────────────────────────────────────

def connect_direct(host: str, port: int) -> Connection:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect((host, port))
    return Connection(s, is_tor=False)


def connect_tor(dir_host: str, dir_port: int, dest_host: str, dest_port: int) -> Connection:
    nodes = get_nodes(dir_host, dir_port)
    entry, middle, exit_node = pick_nodes(nodes)
    circuit_id, K1, K2, K3, entry_sock = build_circuit(
        entry, middle, exit_node, dest_host, dest_port
    )
    tor_sock = TorSocket(circuit_id, K1, K2, K3, entry_sock)
    return Connection(tor_sock, is_tor=True)
