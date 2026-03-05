"""
Onion Routing Client

1. Fetches the node list from the directory server.
2. Selects one entry, one middle, and one exit node.
3. Builds a circuit:  client -> entry -> middle -> exit -> server
4. Sends messages interactively; each message is triple-encrypted (one layer per hop).
   The exit -> server leg is raw (no AES).

Usage:
    python client.py --dest-host 127.0.0.1 --dest-port 9000

Optional:
    --dir-host  (default 127.0.0.1)
    --dir-port  (default 8000)

Encryption order (wrapping):
    payload = AES_K3(message)          # exit layer (innermost)
    payload = AES_K2(payload)          # middle layer
    payload = AES_K1(payload)          # entry layer (outermost)

Decryption order (unwrapping response):
    data = AES_K1_decrypt(data)        # entry layer
    data = AES_K2_decrypt(data)        # middle layer
    data = AES_K3_decrypt(data)        # exit layer
"""

import socket
import json
import base64
import uuid
import argparse
import random

from Crypto.PublicKey import RSA
from Crypto.Cipher import AES, PKCS1_OAEP
from Crypto.Random import get_random_bytes
from Crypto.Util.Padding import pad, unpad


# ── Socket helpers ────────────────────────────────────────────────────────────

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


# ── AES helpers ───────────────────────────────────────────────────────────────

def aes_encrypt(key: bytes, plaintext: bytes) -> bytes:
    iv = get_random_bytes(16)
    cipher = AES.new(key, AES.MODE_CBC, iv)
    return iv + cipher.encrypt(pad(plaintext, AES.block_size))


def aes_decrypt(key: bytes, ciphertext: bytes) -> bytes:
    iv, ct = ciphertext[:16], ciphertext[16:]
    cipher = AES.new(key, AES.MODE_CBC, iv)
    return unpad(cipher.decrypt(ct), AES.block_size)


# ── Directory ─────────────────────────────────────────────────────────────────

def get_nodes(dir_host, dir_port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect((dir_host, dir_port))
    send_msg(s, {'type': 'GET_NODES'})
    resp = json.loads(recv_msg(s))
    s.close()
    return resp['nodes']


def pick_nodes(nodes):
    by_type = {}
    for n in nodes:
        by_type.setdefault(n['node_type'], []).append(n)
    missing = [t for t in ('entry', 'middle', 'exit') if not by_type.get(t)]
    if missing:
        raise RuntimeError(f"Directory is missing node type(s): {missing}")
    entry     = random.choice(by_type['entry'])
    middle    = random.choice(by_type['middle'])
    exit_node = random.choice(by_type['exit'])
    return entry, middle, exit_node


# ── Circuit building ──────────────────────────────────────────────────────────

def make_setup_payload(pub_pem: str, inner: dict) -> dict:
    """
    Hybrid-encrypt inner (dict) for the node whose public key is pub_pem.
    Returns a dict with 'encrypted_key' and 'encrypted_data' (both base64 str).
    Only the target node can decrypt this.
    """
    setup_key = get_random_bytes(16)                       # ephemeral AES key
    pub = RSA.import_key(pub_pem)
    enc_key = PKCS1_OAEP.new(pub).encrypt(setup_key)      # RSA-OAEP(setup_key)
    enc_data = aes_encrypt(setup_key, json.dumps(inner).encode())  # AES(inner json)
    return {
        'encrypted_key':  base64.b64encode(enc_key).decode(),
        'encrypted_data': base64.b64encode(enc_data).decode(),
    }


def build_circuit(entry, middle, exit_node, dest_host, dest_port):
    """
    Negotiate keys and establish the three-hop circuit.
    Returns (circuit_id, K1, K2, K3, entry_sock).
    """
    circuit_id = str(uuid.uuid4())
    K1 = get_random_bytes(16)   # entry relay key
    K2 = get_random_bytes(16)   # middle relay key
    K3 = get_random_bytes(16)   # exit relay key

    # Build payloads from exit inward so each level can embed the next.

    # Exit payload: tells exit node its relay key and the final destination.
    exit_payload = make_setup_payload(exit_node['public_key'], {
        'key':       base64.b64encode(K3).decode(),
        'dest_host': dest_host,
        'dest_port': dest_port,
    })

    # Middle payload: tells middle its relay key, exit's address, and exit's payload.
    middle_payload = make_setup_payload(middle['public_key'], {
        'key':             base64.b64encode(K2).decode(),
        'next_host':       exit_node['host'],
        'next_port':       exit_node['port'],
        'forward_payload': exit_payload,      # opaque to middle; forwarded to exit
    })

    # Entry payload: tells entry its relay key, middle's address, and middle's payload.
    entry_payload = make_setup_payload(entry['public_key'], {
        'key':             base64.b64encode(K1).decode(),
        'next_host':       middle['host'],
        'next_port':       middle['port'],
        'forward_payload': middle_payload,    # opaque to entry; forwarded to middle
    })

    # Connect to entry and send CIRCUIT_SETUP.
    entry_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    entry_sock.connect((entry['host'], entry['port']))

    send_msg(entry_sock, {
        'type':       'CIRCUIT_SETUP',
        'circuit_id': circuit_id,
        'payload':    entry_payload,
    })

    resp = json.loads(recv_msg(entry_sock))
    if resp.get('status') != 'ok':
        entry_sock.close()
        raise RuntimeError(f"Circuit setup failed: {resp}")

    print(f"[CLIENT] Circuit established: {circuit_id[:8]}")
    print(f"[CLIENT]   client -> {entry['host']}:{entry['port']} (entry)")
    print(f"[CLIENT]          -> {middle['host']}:{middle['port']} (middle)")
    print(f"[CLIENT]          -> {exit_node['host']}:{exit_node['port']} (exit)")
    print(f"[CLIENT]          -> {dest_host}:{dest_port} (server, raw)")
    return circuit_id, K1, K2, K3, entry_sock


# ── Relay ─────────────────────────────────────────────────────────────────────

def send_relay(entry_sock, circuit_id, K1, K2, K3, message: bytes) -> bytes:
    """
    Wrap message in three AES layers and send through the circuit.
    Unwrap the three response layers and return plaintext.
    """
    # Wrap: exit layer first (innermost), entry layer last (outermost)
    data = aes_encrypt(K3, message)
    data = aes_encrypt(K2, data)
    data = aes_encrypt(K1, data)

    send_msg(entry_sock, {
        'type':       'RELAY',
        'circuit_id': circuit_id,
        'data':       base64.b64encode(data).decode(),
    })

    raw = recv_msg(entry_sock)
    if raw is None:
        raise RuntimeError("Entry node disconnected")

    resp = json.loads(raw)
    if 'error' in resp:
        raise RuntimeError(f"Circuit error: {resp['error']}")

    # Unwrap: entry layer first, exit layer last
    data = base64.b64decode(resp['data'])
    data = aes_decrypt(K1, data)
    data = aes_decrypt(K2, data)
    data = aes_decrypt(K3, data)
    return data


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Onion Routing Client')
    parser.add_argument('--dest-host', default='127.0.0.1', help='Destination server host')
    parser.add_argument('--dest-port', type=int, default=9000, help='Destination server port')
    parser.add_argument('--dir-host',  default='127.0.0.1', help='Directory server host')
    parser.add_argument('--dir-port',  type=int, default=8000, help='Directory server port')
    args = parser.parse_args()

    print("[CLIENT] Querying directory server...")
    nodes = get_nodes(args.dir_host, args.dir_port)
    print(f"[CLIENT] Found {len(nodes)} node(s): {[n['node_type'] for n in nodes]}")

    entry, middle, exit_node = pick_nodes(nodes)

    circuit_id, K1, K2, K3, entry_sock = build_circuit(
        entry, middle, exit_node, args.dest_host, args.dest_port
    )

    print("[CLIENT] Ready. Type a message and press Enter. Ctrl+C to quit.\n")
    try:
        while True:
            try:
                message = input("you > ").strip()
            except EOFError:
                break
            if not message:
                continue
            response = send_relay(entry_sock, circuit_id, K1, K2, K3, message.encode())
            print(f"srv < {response.decode()}\n")
    except KeyboardInterrupt:
        print("\n[CLIENT] Shutting down.")
    finally:
        entry_sock.close()


if __name__ == '__main__':
    main()
