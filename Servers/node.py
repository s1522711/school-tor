"""
Onion Router Node
Handles CIRCUIT_SETUP and RELAY messages for a single hop in the circuit.

Usage:
    python node.py --type entry  --port 9001
    python node.py --type middle --port 9002
    python node.py --type exit   --port 9003

Optional:
    --host      bind address (default 127.0.0.1)
    --dir-host  directory server host (default 127.0.0.1)
    --dir-port  directory server port (default 8000)

Circuit setup payload uses hybrid encryption:
  - A random 16-byte setup key is RSA-OAEP encrypted with this node's public key.
  - The actual routing data (circuit AES key + next hop info) is AES-CBC encrypted
    with that setup key.

This prevents RSA size limits from being hit, since only 16 bytes go through RSA.

Forward relay:  each node decrypts one AES layer.
Backward relay: each node adds one AES layer (re-encrypts).
Exit -> server traffic is raw (length-prefixed but no AES).
"""

import socket
import json
import threading
import base64
import argparse
import select

from Crypto.PublicKey import RSA
from Crypto.Cipher import AES, PKCS1_OAEP
from Crypto.Random import get_random_bytes
from Crypto.Util.Padding import pad, unpad


# ── Socket helpers ────────────────────────────────────────────────────────────

def recv_msg(sock):
    """Receive a length-prefixed message (4-byte big-endian header)."""
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
    """Send a length-prefixed message."""
    if isinstance(data, dict):
        data = json.dumps(data).encode()
    elif isinstance(data, str):
        data = data.encode()
    sock.sendall(len(data).to_bytes(4, 'big') + data)


# ── AES helpers ───────────────────────────────────────────────────────────────

def aes_encrypt(key: bytes, plaintext: bytes) -> bytes:
    """AES-128-CBC encrypt; prepends random IV."""
    iv = get_random_bytes(16)
    cipher = AES.new(key, AES.MODE_CBC, iv)
    return iv + cipher.encrypt(pad(plaintext, AES.block_size))


def aes_decrypt(key: bytes, ciphertext: bytes) -> bytes:
    """AES-128-CBC decrypt; reads IV from first 16 bytes."""
    iv, ct = ciphertext[:16], ciphertext[16:]
    cipher = AES.new(key, AES.MODE_CBC, iv)
    return unpad(cipher.decrypt(ct), AES.block_size)


# ── Per-node state ────────────────────────────────────────────────────────────

# circuits[circuit_id] = {
#   'key':      bytes,           # this node's AES relay key
#   'next_sock': socket | None,  # persistent connection to next hop (None for exit)
#   'dest':     (host, port) | None,  # only for exit nodes
#   'is_exit':  bool,
# }
circuits: dict = {}
circuits_lock = threading.Lock()

# Set by main() before any threads start.
rsa_key = None
node_type = None
debug = False


def dbg(label: str, data: bytes):
    if not debug:
        return
    preview = data[:32].hex()
    suffix = '...' if len(data) > 32 else ''
    print(f"  [DBG] {label:28s} {len(data):4d}B  {preview}{suffix}")


# ── Decrypt setup payload ─────────────────────────────────────────────────────

def decrypt_setup_payload(payload: dict) -> dict:
    """
    payload = {
        'encrypted_key':  base64(RSA_OAEP(pub, setup_aes_key)),
        'encrypted_data': base64(AES_CBC(setup_aes_key, json_inner)),
    }
    Returns the decoded inner dict.
    """
    setup_key = PKCS1_OAEP.new(rsa_key).decrypt(
        base64.b64decode(payload['encrypted_key'])
    )
    raw = aes_decrypt(setup_key, base64.b64decode(payload['encrypted_data']))
    return json.loads(raw.decode())


# ── Message handlers ──────────────────────────────────────────────────────────

def handle_circuit_setup(conn, msg, local_circuits: set):
    circuit_id = msg['circuit_id']

    try:
        inner = decrypt_setup_payload(msg['payload'])
    except Exception as e:
        send_msg(conn, {'status': 'error', 'msg': f'Setup decrypt failed: {e}'})
        return

    relay_key = base64.b64decode(inner['key'])

    if 'dest_host' in inner:
        # ── Exit node ──
        dest = (inner['dest_host'], inner['dest_port'])
        try:
            dest_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            dest_sock.connect(dest)
        except Exception as e:
            send_msg(conn, {'status': 'error', 'msg': f'Cannot reach destination: {e}'})
            return
        with circuits_lock:
            circuits[circuit_id] = {
                'key': relay_key,
                'next_sock': None,
                'dest_sock': dest_sock,
                'dest': dest,
                'is_exit': True,
            }
        local_circuits.add(circuit_id)
        print(f"[{node_type.upper()}] Circuit {circuit_id[:8]}  dest={dest[0]}:{dest[1]}")
        send_msg(conn, {'status': 'ok'})

    else:
        # ── Entry or middle node ──
        next_host = inner['next_host']
        next_port = inner['next_port']
        forward_payload = inner['forward_payload']  # already a dict for next hop

        try:
            next_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            next_sock.connect((next_host, next_port))
        except Exception as e:
            send_msg(conn, {'status': 'error', 'msg': f'Cannot reach next hop: {e}'})
            return

        # Forward setup (the nested payload is already encrypted for the next node)
        send_msg(next_sock, {
            'type': 'CIRCUIT_SETUP',
            'circuit_id': circuit_id,
            'payload': forward_payload,
        })

        resp_raw = recv_msg(next_sock)
        if resp_raw is None:
            send_msg(conn, {'status': 'error', 'msg': 'Next hop disconnected during setup'})
            next_sock.close()
            return

        resp = json.loads(resp_raw)
        if resp.get('status') != 'ok':
            send_msg(conn, resp)
            next_sock.close()
            return

        with circuits_lock:
            circuits[circuit_id] = {
                'key': relay_key,
                'next_sock': next_sock,
                'dest': None,
                'is_exit': False,
            }
        local_circuits.add(circuit_id)
        print(f"[{node_type.upper()}] Circuit {circuit_id[:8]}  next={next_host}:{next_port}")
        send_msg(conn, {'status': 'ok'})


def handle_relay(conn, msg):
    circuit_id = msg['circuit_id']

    with circuits_lock:
        circuit = circuits.get(circuit_id)

    if not circuit:
        send_msg(conn, {'type': 'RELAY_RESPONSE', 'circuit_id': circuit_id,
                        'error': 'unknown circuit'})
        return

    relay_key = circuit['key']

    encrypted_blob = base64.b64decode(msg['data'])
    dbg("recv  (still encrypted)", encrypted_blob)

    # Peel one onion layer
    decrypted = aes_decrypt(relay_key, encrypted_blob)
    dbg("after peel             ", decrypted)

    if circuit['is_exit']:
        # ── Exit: deliver via persistent destination connection ──
        # Empty decrypted = poll (check for pushed messages without sending).
        # Non-empty = real message; send to dest then wait for response.
        dest_sock = circuit['dest_sock']

        if decrypted:
            dbg("plaintext to server    ", decrypted)
            if debug:
                print(f"  [DBG] plaintext text:              {decrypted.decode(errors='replace')!r}")

        try:
            if decrypted:
                send_msg(dest_sock, decrypted)
            # Longer timeout for real messages (waiting for Ack); short for polls.
            timeout = 5.0 if decrypted else 0.5
            readable, _, _ = select.select([dest_sock], [], [], timeout)
            if readable:
                raw_response = recv_msg(dest_sock)
                if raw_response is None:
                    raw_response = b''
            else:
                raw_response = b''
        except Exception as e:
            raw_response = f'DELIVERY ERROR: {e}'.encode()

        if raw_response:
            dbg("server response (raw)  ", raw_response)
            print(f"[{node_type.upper()}] Delivered  circuit={circuit_id[:8]}")

        # Wrap response in one AES layer for the backward trip
        resp_enc = aes_encrypt(relay_key, raw_response)
        dbg("response re-encrypted  ", resp_enc)
        send_msg(conn, {
            'type': 'RELAY_RESPONSE',
            'circuit_id': circuit_id,
            'data': base64.b64encode(resp_enc).decode(),
        })

    else:
        # ── Entry / middle: forward with one layer removed ──
        next_sock = circuit['next_sock']
        send_msg(next_sock, {
            'type': 'RELAY',
            'circuit_id': circuit_id,
            'data': base64.b64encode(decrypted).decode(),
        })

        resp_raw = recv_msg(next_sock)
        if resp_raw is None:
            send_msg(conn, {'type': 'RELAY_RESPONSE', 'circuit_id': circuit_id,
                            'error': 'next hop disconnected'})
            return

        resp = json.loads(resp_raw)
        if 'error' in resp:
            send_msg(conn, resp)
            return

        # Add our layer back for the backward trip
        inner_data = base64.b64decode(resp['data'])
        dbg("response from next hop ", inner_data)
        re_enc = aes_encrypt(relay_key, inner_data)
        dbg("response re-encrypted  ", re_enc)

        print(f"[{node_type.upper()}] Relayed    circuit={circuit_id[:8]}")
        send_msg(conn, {
            'type': 'RELAY_RESPONSE',
            'circuit_id': circuit_id,
            'data': base64.b64encode(re_enc).decode(),
        })


# ── Connection loop ───────────────────────────────────────────────────────────

def handle_connection(conn, addr):
    print(f"[{node_type.upper()}] Connection from {addr}")
    local_circuits: set = set()
    try:
        while True:
            raw = recv_msg(conn)
            if raw is None:
                break
            msg = json.loads(raw)
            t = msg.get('type')

            if t == 'CIRCUIT_SETUP':
                handle_circuit_setup(conn, msg, local_circuits)
            elif t == 'RELAY':
                handle_relay(conn, msg)
            else:
                print(f"[{node_type.upper()}] Unknown message type: {t!r}")
    except Exception as e:
        print(f"[{node_type.upper()}] Connection error from {addr}: {e}")
    finally:
        conn.close()
        # Close every downstream socket opened by this connection.
        # Closing next_sock causes the next node's recv_msg to return None,
        # which triggers its own finally block, cascading all the way to exit.
        for cid in local_circuits:
            with circuits_lock:
                circuit = circuits.pop(cid, None)
            if circuit:
                if circuit['next_sock']:
                    try:
                        circuit['next_sock'].close()
                    except Exception:
                        pass
                if circuit.get('dest_sock'):
                    try:
                        circuit['dest_sock'].close()
                    except Exception:
                        pass
        if local_circuits:
            print(f"[{node_type.upper()}] Torn down {len(local_circuits)} circuit(s) from {addr}")


# ── Directory registration ────────────────────────────────────────────────────

def register_with_directory(dir_host, dir_port, n_type, host, port, pub_pem):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect((dir_host, dir_port))
    send_msg(s, {
        'type': 'REGISTER',
        'node_type': n_type,
        'host': host,
        'port': port,
        'public_key': pub_pem,
    })
    resp = json.loads(recv_msg(s))
    s.close()
    if resp.get('status') == 'ok':
        print(f"[{n_type.upper()}] Registered with directory at {dir_host}:{dir_port}")
    else:
        raise RuntimeError(f"Registration failed: {resp}")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    global rsa_key, node_type, debug

    parser = argparse.ArgumentParser(description='Onion Router Node')
    parser.add_argument('--type', required=True, choices=['entry', 'middle', 'exit'],
                        help='Role of this node in the circuit')
    parser.add_argument('--host', default='127.0.0.1', help='Bind address')
    parser.add_argument('--port', type=int, required=True, help='Listen port')
    parser.add_argument('--dir-host', default='127.0.0.1', help='Directory server host')
    parser.add_argument('--dir-port', type=int, default=8000, help='Directory server port')
    parser.add_argument('--debug', action='store_true',
                        help='Print hex previews of messages at each relay step')
    args = parser.parse_args()

    node_type = args.type
    debug = args.debug
    if debug:
        print(f"[{node_type.upper()}] Debug mode ON")

    print(f"[{node_type.upper()}] Generating RSA-2048 key pair...")
    rsa_key = RSA.generate(2048)
    pub_pem = rsa_key.publickey().export_key().decode()

    register_with_directory(args.dir_host, args.dir_port,
                            node_type, args.host, args.port, pub_pem)

    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_sock.bind((args.host, args.port))
    server_sock.listen(20)
    server_sock.settimeout(1.0)
    print(f"[{node_type.upper()}] Listening on {args.host}:{args.port}")

    try:
        while True:
            try:
                conn, addr = server_sock.accept()
            except socket.timeout:
                continue
            t = threading.Thread(target=handle_connection, args=(conn, addr), daemon=True)
            t.start()
    except KeyboardInterrupt:
        print(f"\n[{node_type.upper()}] Shutting down...")
    finally:
        with circuits_lock:
            for circuit in circuits.values():
                if circuit['next_sock']:
                    try:
                        circuit['next_sock'].close()
                    except Exception:
                        pass
                if circuit.get('dest_sock'):
                    try:
                        circuit['dest_sock'].close()
                    except Exception:
                        pass
        server_sock.close()


if __name__ == '__main__':
    main()
