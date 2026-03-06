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


# ── Tor circuit ───────────────────────────────────────────────────────────────

def aes_encrypt(key: bytes, plaintext: bytes) -> bytes:
    iv = get_random_bytes(16)
    cipher = AES.new(key, AES.MODE_CBC, iv)
    return iv + cipher.encrypt(pad(plaintext, AES.block_size))


def aes_decrypt(key: bytes, ciphertext: bytes) -> bytes:
    iv, ct = ciphertext[:16], ciphertext[16:]
    cipher = AES.new(key, AES.MODE_CBC, iv)
    return unpad(cipher.decrypt(ct), AES.block_size)


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
    return (random.choice(by_type['entry']),
            random.choice(by_type['middle']),
            random.choice(by_type['exit']))


def make_setup_payload(pub_pem: str, inner: dict) -> dict:
    setup_key = get_random_bytes(16)
    pub = RSA.import_key(pub_pem)
    enc_key  = PKCS1_OAEP.new(pub).encrypt(setup_key)
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
        self._cid   = circuit_id
        self._K1, self._K2, self._K3 = K1, K2, K3
        self._entry = entry_sock
        self._buf   = b''
        self._lock  = threading.Lock()

    def sendall(self, data: bytes):
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


# ── Incoming message printer ──────────────────────────────────────────────────

def print_incoming(msg_type: str, data: dict):
    """Pretty-print a server-pushed message."""
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
        pass
    else:
        print(f"\r  [Server] {msg_type}: {data}")


# ── Background receiver ───────────────────────────────────────────────────────

def receiver(sock, stop_event: threading.Event):
    """Continuously reads server messages and prints them."""
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
    Polls the circuit for server-pushed messages (UserJoined, IncomingMessage, etc.).
    Each poll() call blocks for up to 0.5 s inside the exit node's select(), so the
    polling rate is roughly 2/s with no extra sleep needed.
    The lock inside TorSocket serialises this thread with the main send thread.
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
    Interactively create or join a room.
    Returns (username, room_code).
    Handles the initial request/response synchronously so the receiver
    thread does not need to be running yet.
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
    Input loop for Tor mode. Each send goes through the circuit and reads the
    direct response (Ack / Stats / RoomLeft) from the buffer. Push notifications
    from other users are delivered by the tor_receiver background thread.
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
