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
    """
    Read one complete length-prefixed message from a TCP socket.

    How it works:
        TCP delivers a continuous byte stream with no inherent message
        boundaries. This function enforces the framing protocol used
        throughout the system: a 4-byte big-endian integer header followed
        by exactly that many payload bytes. Both reads loop until complete
        because a single recv() call can return fewer bytes than requested.

    Why it exists:
        Without framing, successive JSON messages could be fused together or
        split mid-object in the byte stream. This helper ensures the caller
        always receives exactly one complete, parseable message.

    Returns:
        bytes — the raw payload, or None if the connection closed.
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
    Send data over TCP with a 4-byte big-endian length prefix.

    How it works:
        Serialises dicts to JSON, encodes strs to UTF-8, then prepends the
        4-byte payload length and calls sendall() — which loops internally
        until all bytes are handed to the OS, preventing partial writes.

    Why it exists:
        The receiving side (relay nodes, destination server) uses recv_msg to
        read messages; both sides must use the same framing or the stream will
        be misinterpreted.
    """
    if isinstance(data, dict):
        data = json.dumps(data).encode()
    elif isinstance(data, str):
        data = data.encode()
    sock.sendall(len(data).to_bytes(4, 'big') + data)


# ── AES helpers ───────────────────────────────────────────────────────────────

def aes_encrypt(key: bytes, plaintext: bytes) -> bytes:
    """
    AES-128-CBC encrypt plaintext and prepend the IV to the output.

    How it works:
        1. Generates a cryptographically random 16-byte IV (Initialization
           Vector). A fresh IV for every call means identical plaintexts
           produce different ciphertexts, preventing pattern analysis.
        2. Pads plaintext to a 16-byte multiple using PKCS7 — required because
           AES-CBC works on fixed-size 128-bit (16-byte) blocks.
        3. Encrypts with AES-CBC mode using the provided key and the new IV.
        4. Returns iv + ciphertext so the IV travels with the data and the
           decryptor can recover it without a separate channel.

    Why it exists:
        Each hop in the onion circuit has its own key (K1, K2, K3). This
        function applies one layer of encryption. The three-layer wrapping in
        send_relay calls it three times with different keys.

    Args:
        key       — 16-byte AES-128 key.
        plaintext — arbitrary bytes (may be empty for poll messages).

    Returns:
        bytes — 16-byte IV + ciphertext (always a multiple of 16 bytes).
    """
    iv = get_random_bytes(16)
    cipher = AES.new(key, AES.MODE_CBC, iv)
    return iv + cipher.encrypt(pad(plaintext, AES.block_size))


def aes_decrypt(key: bytes, ciphertext: bytes) -> bytes:
    """
    AES-128-CBC decrypt, reading the IV from the first 16 bytes.

    How it works:
        Splits the input into iv (first 16 bytes) and ct (the rest), creates
        an AES-CBC cipher with the same key and iv, decrypts, then strips the
        PKCS7 padding to recover the original plaintext.

    Why it exists:
        The inverse of aes_encrypt. In send_relay the decryption order is
        K1 → K2 → K3 (outermost first), which is the reverse of the
        K3 → K2 → K1 wrapping order used when sending.

    Args:
        key        — 16-byte AES-128 key.
        ciphertext — iv + ciphertext bytes as produced by aes_encrypt.

    Returns:
        bytes — original plaintext.
    """
    iv, ct = ciphertext[:16], ciphertext[16:]
    cipher = AES.new(key, AES.MODE_CBC, iv)
    return unpad(cipher.decrypt(ct), AES.block_size)


# ── Directory ─────────────────────────────────────────────────────────────────

def get_nodes(dir_host, dir_port):
    """
    Query the directory server for the list of registered relay nodes.

    How it works:
        Opens a short-lived TCP connection to the directory server, sends a
        GET_NODES request, reads the JSON response, closes the socket, and
        returns the list of node dicts. Each dict contains:
            {node_type, host, port, public_key (PEM string)}.

    Why it exists:
        The directory server is the only place that knows which relay nodes
        are currently online and what their RSA public keys are. Without those
        public keys the client cannot build the hybrid-encrypted CIRCUIT_SETUP
        payloads that each node expects.

    Returns:
        list[dict] — the full node list.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect((dir_host, dir_port))
    send_msg(s, {'type': 'GET_NODES'})
    resp = json.loads(recv_msg(s))
    s.close()
    return resp['nodes']


def pick_nodes(nodes):
    """
    Choose one entry, one middle, and one exit node at random.

    How it works:
        Groups the flat node list by 'node_type', then calls random.choice()
        on each group independently. This ensures a balanced selection even
        when there are multiple nodes of each type (see --nodes N in
        start.bat/start.sh). Raises RuntimeError if any type is absent so
        the caller gets a clear error instead of a silent KeyError.

    Why it exists:
        Real Tor randomises path selection to make traffic correlation harder
        — even an adversary watching some nodes cannot predict which specific
        three nodes any circuit will use. This function replicates that idea.

    Returns:
        tuple — (entry_node, middle_node, exit_node), each a dict from the
                directory server.
    """
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

    How it works:
        RSA-OAEP with a 2048-bit key can encrypt at most ~214 bytes — far too
        small to hold a routing dict plus a nested payload for the next hop.
        Hybrid encryption solves this:
            1. Generate a random 16-byte 'setup_key' (ephemeral AES key).
            2. RSA-OAEP encrypt the 16-byte setup_key with the node's public key
               → 'encrypted_key' (256 bytes, safe to include in JSON as base64).
            3. AES-CBC encrypt the full JSON of 'inner' with the setup_key
               → 'encrypted_data' (can be arbitrarily large).
        Only the node that holds the matching RSA private key can decrypt
        'encrypted_key' to recover setup_key, then use setup_key to decrypt
        'encrypted_data' and read its routing instructions.

    Why it exists:
        This pattern (RSA wraps AES key, AES wraps data) is exactly how TLS
        handshakes and PGP work. Here it provides forward secrecy per-circuit:
        each circuit uses a fresh setup_key, so compromising the node's RSA
        private key after the fact does not reveal past traffic.

    Args:
        pub_pem — PEM-encoded RSA-2048 public key of the target node.
        inner   — dict of routing data for this node only.

    Returns:
        dict with 'encrypted_key' (base64) and 'encrypted_data' (base64).
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

    How it works:
        1. Generates a UUID circuit_id and three independent random 16-byte AES
           relay keys: K1 (entry), K2 (middle), K3 (exit).

        2. Builds payloads from the inside out — exit first, then middle, then
           entry — because each layer embeds the next as an opaque blob:

               exit_payload   tells the exit node: its relay key K3 + the
                              destination (dest_host:dest_port).

               middle_payload tells the middle node: its relay key K2 + the
                              exit node's address + exit_payload as a blob
                              (middle cannot read exit_payload — different key).

               entry_payload  tells the entry node: its relay key K1 + the
                              middle node's address + middle_payload as a blob.

           Each payload is independently hybrid-encrypted with make_setup_payload
           so only the intended node can read its routing instructions.

        3. Connects to the entry node and sends a CIRCUIT_SETUP message
           containing circuit_id and entry_payload. The entry node decrypts its
           own instructions, forwards middle_payload to the middle node, which
           forwards exit_payload to the exit node. Each node opens a persistent
           TCP socket to the next hop. When the exit node stores its dest_sock
           and replies 'ok', the confirmation bubbles back: exit → middle →
           entry → this client.

        4. If the entry node returns 'ok', the circuit is live. K1/K2/K3 are
           known only to this client and the respective nodes.

    Why it exists:
        Circuit building is the core of onion routing. After this function
        returns, the client can send messages through a single TCP connection
        to the entry node and they will arrive at the destination decrypted,
        with no single node knowing both the source and destination.

    Returns:
        (circuit_id: str, K1: bytes, K2: bytes, K3: bytes, entry_sock: socket)
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

    How it works:
        Sending (wrapping, innermost first):
            data = AES_K3(message)     ← exit layer (innermost)
            data = AES_K2(data)        ← middle layer
            data = AES_K1(data)        ← entry layer (outermost)

        The triple-encrypted blob is base64-encoded and sent to the entry node
        as a RELAY message. Each node strips its own outermost layer:
            entry  peels K1 → forwards AES_K2(AES_K3(message)) to middle
            middle peels K2 → forwards AES_K3(message) to exit
            exit   peels K3 → sends plaintext 'message' to the destination server

        On the way back each node re-wraps with its own key:
            exit   wraps server_response in K3
            middle wraps in K2
            entry  wraps in K1

        Receiving (unwrapping, outermost first):
            data = AES_K1_decrypt(data)  ← remove entry layer
            data = AES_K2_decrypt(data)  ← remove middle layer
            data = AES_K3_decrypt(data)  ← remove exit layer → plaintext response

    Why it exists:
        This is the core relay operation. The symmetric encryption ensures that
        each node sees only what it needs: entry knows the next hop but not the
        message, middle knows neither source nor destination, exit knows the
        destination but not the source's real IP.

    Args:
        entry_sock  — open TCP socket to the entry node.
        circuit_id  — UUID that every node uses to look up the circuit state.
        K1, K2, K3  — the three 16-byte relay keys for entry, middle, exit.
        message     — raw bytes to deliver to the destination server.

    Returns:
        bytes — the plaintext response from the destination server.
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
    """
    Entry point — build a Tor circuit and run an interactive echo REPL.

    How it works:
        1. Parses --dest-host/--dest-port (destination server address) and
           --dir-host/--dir-port (directory server address).
        2. Queries the directory for the node list, picks one of each type,
           and builds a three-hop circuit to the destination server.
        3. Drops into a REPL: reads a line from stdin, calls send_relay() to
           send it through the circuit, prints the echoed response.
        4. On KeyboardInterrupt or EOFError, closes entry_sock and exits.

    Why it exists:
        This is a standalone testing tool for the onion relay infrastructure.
        It demonstrates and verifies the complete send_relay / decrypt cycle
        without any chat protocol on top.
    """
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
