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
    """
    Read one complete length-prefixed message from a TCP socket.

    How it works:
        Reads a 4-byte big-endian header that tells us the payload size, then
        reads exactly that many payload bytes. Both reads loop because a single
        sock.recv() call can return fewer bytes than requested — TCP is a stream,
        not a datagram protocol, so partial reads are normal under load.

        Returning None on any recv() that yields empty bytes signals the caller
        that the remote side has closed the connection gracefully (EOF). The
        caller's loop then breaks and triggers connection teardown.

    Why it exists:
        The same framing protocol (4-byte length + payload) is used by every
        component in the system. Centralising the logic here avoids bugs where
        different parts of the node might handle partial reads differently.

    Returns:
        bytes — the raw message payload, or None if the socket closed.
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
    Send a message over TCP with a 4-byte big-endian length prefix.

    How it works:
        Accepts dict (→ JSON → UTF-8 bytes), str (→ UTF-8 bytes), or raw bytes.
        Prepends the 4-byte payload length and calls sendall() which loops
        internally until all bytes are passed to the kernel, preventing the
        silent truncation that a bare send() could cause.

    Why it exists:
        Paired with recv_msg to implement the system-wide framing protocol.
        sendall() is critical in a multi-threaded server because TCP's send()
        may return before all bytes are written when the socket send buffer
        is close to full.
    """
    if isinstance(data, dict):
        data = json.dumps(data).encode()
    elif isinstance(data, str):
        data = data.encode()
    sock.sendall(len(data).to_bytes(4, 'big') + data)


# ── AES helpers ───────────────────────────────────────────────────────────────

def aes_encrypt(key: bytes, plaintext: bytes) -> bytes:
    """
    AES-128-CBC encrypt and prepend a fresh random IV to the output.

    How it works:
        Generates 16 cryptographically random bytes as the IV (Initialization
        Vector). AES-CBC XORs each plaintext block with the previous ciphertext
        block (or the IV for the first block) before encrypting, which means
        identical plaintexts encrypt to different ciphertexts when the IV
        differs — critical for security. PKCS7 pads the plaintext to a 16-byte
        multiple as required by CBC mode. Returns iv + ciphertext so the
        recipient can read the IV from the first 16 bytes.

    Why it exists:
        Each relay key (K1, K2, K3) is applied via this function. On the
        forward path, the exit node calls it when re-encrypting the server's
        response. On the backward path, middle and entry also call it to add
        their own layers. A fresh IV each time prevents ciphertext comparison
        attacks across repeated relay messages.

    Args:
        key       — 16-byte AES-128 key (any of K1, K2, K3).
        plaintext — bytes to encrypt (may be the empty bytes b'' for poll).

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
        Splits the input at byte 16: the first 16 bytes are the IV, the rest
        is the actual ciphertext. Reconstructs the same AES-CBC cipher (key +
        IV must match what was used to encrypt), decrypts, then removes PKCS7
        padding to restore the original plaintext.

        If the data is corrupt or was encrypted with the wrong key, unpad()
        will raise a ValueError — the caller's except block catches this.

    Why it exists:
        The inverse of aes_encrypt. Called in handle_relay when the node
        "peels" its own layer off the incoming onion message.

    Args:
        key        — 16-byte AES-128 key matching the one used in aes_encrypt.
        ciphertext — iv + ciphertext as returned by aes_encrypt.

    Returns:
        bytes — the original plaintext.
    """
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
    """
    Print a hex preview of a byte blob when debug mode is on.

    How it works:
        Shows the first 32 bytes of 'data' in hex, followed by '...' if there
        are more bytes, along with a label and the total byte count. The label
        is left-justified in a 28-character field so that consecutive dbg()
        calls in handle_relay line up neatly for visual comparison.

    Why it exists:
        Onion encryption is opaque by design — the bytes look like noise. When
        debugging a failing circuit, being able to see the raw bytes *before*
        decryption and *after* decryption at each hop is invaluable for
        diagnosing wrong keys, wrong order of operations, or framing bugs.
        The --debug flag enables this output without affecting normal operation.

    Args:
        label — human-readable description of what 'data' represents.
        data  — the bytes to preview.
    """
    if not debug:
        return
    preview = data[:32].hex()
    suffix = '...' if len(data) > 32 else ''
    print(f"  [DBG] {label:28s} {len(data):4d}B  {preview}{suffix}")


# ── Decrypt setup payload ─────────────────────────────────────────────────────

def decrypt_setup_payload(payload: dict) -> dict:
    """
    Decrypt a hybrid-encrypted circuit-setup payload addressed to this node.

    How it works:
        The payload has two fields:
            'encrypted_key'  — the 16-byte setup_aes_key, RSA-OAEP encrypted
                               with *this node's* public key. Only this node's
                               RSA private key can decrypt it.
            'encrypted_data' — the routing instructions (JSON), AES-CBC
                               encrypted with setup_aes_key.

        Step 1: Use this node's RSA private key (rsa_key) with PKCS1_OAEP to
                decrypt 'encrypted_key' → setup_aes_key (16 bytes).
        Step 2: Use setup_aes_key to AES-decrypt 'encrypted_data' → raw JSON.
        Step 3: Parse and return the JSON dict.

        The inner dict contains:
            - 'key'            — the relay AES key for this hop (base64).
            - 'next_host'/'next_port' + 'forward_payload'  (entry/middle)
            OR
            - 'dest_host'/'dest_port'  (exit node).

    Why it exists:
        Separating RSA decryption into this function keeps handle_circuit_setup
        clean. It also makes it easy to test in isolation: a valid payload
        should always produce a parseable inner dict; an invalid one (wrong
        key, corrupt data) should raise an exception that handle_circuit_setup
        catches and turns into an error response.

    Args:
        payload — dict with 'encrypted_key' and 'encrypted_data' (base64 strs).

    Returns:
        dict — the decrypted inner routing instructions.

    Raises:
        Exception — on RSA decryption failure (wrong key) or AES/JSON error.
    """
    setup_key = PKCS1_OAEP.new(rsa_key).decrypt(
        base64.b64decode(payload['encrypted_key'])
    )
    raw = aes_decrypt(setup_key, base64.b64decode(payload['encrypted_data']))
    return json.loads(raw.decode())


# ── Message handlers ──────────────────────────────────────────────────────────

def handle_circuit_setup(conn, msg, local_circuits: set):
    """
    Handle a CIRCUIT_SETUP message and establish this hop of the circuit.

    How it works:
        1. Extracts the circuit_id from the message and decrypts this node's
           routing payload using decrypt_setup_payload (hybrid RSA+AES).

        2. Examines the inner dict to decide the node's role in this circuit:

           EXIT path (inner has 'dest_host'):
               Opens a persistent TCP socket to the destination server.
               This socket (dest_sock) lives for the circuit's lifetime —
               the exit node reuses it for every RELAY message in this
               circuit, and the destination server's push responses arrive
               on it for the poll mechanism to pick up.
               Stores the circuit with is_exit=True, no next_sock.

           ENTRY/MIDDLE path (inner has 'next_host'):
               Opens a persistent TCP socket to the next relay node (next_sock).
               Forwards the nested payload ('forward_payload', which is the
               inner dict for the next node) via CIRCUIT_SETUP to that next
               node. Waits for 'ok' to propagate back from the next node before
               responding to the previous hop.
               Stores the circuit with is_exit=False, no dest_sock.

        3. Records circuit_id in both the global `circuits` dict and the
           per-connection `local_circuits` set. The local set is used by
           handle_connection's finally block to know which circuits to tear
           down when this connection closes.

        4. Sends {'status': 'ok'} to the previous hop on success.

    Why it exists:
        This is where the onion is "assembled" from the inside out. When the
        client sends CIRCUIT_SETUP to the entry node, this function fires on
        the entry node, which calls it on the middle node (via a recursive
        CIRCUIT_SETUP forward), which calls it on the exit node. The 'ok'
        responses bubble back: exit → middle → entry → client.

    Args:
        conn          — socket to the *previous* hop (or the client).
        msg           — parsed CIRCUIT_SETUP message dict.
        local_circuits — set of circuit_ids opened by this connection;
                         mutated in-place so handle_connection can clean up.
    """
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
    """
    Handle one RELAY message — peel one encryption layer, forward, re-wrap response.

    How it works:
        Looks up the circuit by circuit_id. If the circuit is unknown (already
        torn down or never set up), returns an error immediately.

        Decrypts the base64-encoded 'data' field with this node's relay_key
        (aes_decrypt) — this removes exactly one layer of the onion.

        Then branches on is_exit:

        EXIT path:
            The decrypted bytes are the *plaintext* payload for the destination
            server — unless they are empty, which signals a *poll* (the client
            asking "do you have any pushed messages for me?").

            Non-empty: sends the plaintext to dest_sock via send_msg, then uses
            select.select() with a 5-second timeout to wait for the server's
            response. Five seconds is long enough for the server to process a
            SendMessage and send an Ack back.

            Empty (poll): skips the send_msg entirely, uses select.select() with
            a 0.5-second timeout to check whether the server has pushed anything
            since the last poll. This short timeout keeps the client's receiver
            thread responsive (~2 polls/second) without burning CPU.

            In both cases: if select() says data is ready, reads one message from
            dest_sock. Otherwise (or if the read returns None), raw_response = b''.
            Re-encrypts raw_response with relay_key and sends it as RELAY_RESPONSE
            back to the previous hop.

        ENTRY/MIDDLE path:
            Forwards the peeled data as a new RELAY to next_sock, blocks for the
            RELAY_RESPONSE, then re-encrypts the response data with relay_key
            (adding this node's layer back) and sends RELAY_RESPONSE to conn.

            This re-wrapping is what makes the backward path "add layers": the
            exit node wraps with K3, middle wraps with K2, entry wraps with K1,
            and the client peels them in reverse order (K1 → K2 → K3).

    Why it exists:
        This is the core of onion routing during live message exchange. The
        separation between the CIRCUIT_SETUP phase (which establishes the
        persistent sockets and keys) and the RELAY phase (which actually sends
        data) allows the same circuit to be reused for thousands of messages
        with no per-message handshake overhead.

    Args:
        conn — socket from the *previous* hop (or the client for entry).
        msg  — parsed RELAY message dict with 'circuit_id' and 'data' (base64).
    """
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
    """
    Manage one TCP connection for its entire lifetime.

    How it works:
        Enters a read loop: calls recv_msg in a blocking fashion, parses the
        JSON, and dispatches to handle_circuit_setup or handle_relay based on
        the 'type' field. The loop runs until recv_msg returns None (client
        disconnected) or an exception is raised.

        The `local_circuits` set tracks every circuit_id that was established
        over *this specific connection*. One TCP connection can carry multiple
        circuits (if the same client opens several), so we need per-connection
        accounting to know what to clean up.

        The finally block is the teardown cascade:
            1. conn.close() — tells the previous hop the connection is dead.
            2. For each local circuit: pops it from `circuits`, closes next_sock
               if present. Closing next_sock causes the *next* node's recv_msg
               to return None, which triggers that node's own finally block.
               This cascade propagates automatically all the way to the exit
               node, which then closes dest_sock and disconnects from the
               destination server.

    Why it exists:
        In a multi-threaded server each accepted connection needs its own thread
        to block on recv_msg without stalling other connections. The cascade
        teardown ensures no zombie sockets or orphaned circuits remain after a
        client disconnects or crashes.

    Args:
        conn — accepted TCP socket from the previous hop or the client.
        addr — (host, port) of the connecting party, used for logging.
    """
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
    """
    Send a REGISTER message to the directory server and verify the response.

    How it works:
        Opens a short-lived TCP connection to the directory server and sends a
        REGISTER message containing this node's type, address, and RSA public
        key (PEM string). The directory server upserts the entry (removing any
        stale record for the same host:port) and replies {'status': 'ok'}.

        If registration fails (directory unreachable or returns an error),
        RuntimeError is raised and main() will exit — a node that cannot
        register is not discoverable by clients and cannot participate in any
        circuit.

    Why it exists:
        Clients build circuits using addresses and public keys fetched from the
        directory. A node that never registers is invisible to clients and
        cannot be selected for any circuit. The public key registered here is
        exactly the RSA key clients will later use in make_setup_payload to
        encrypt CIRCUIT_SETUP payloads for this node.

    Args:
        dir_host — directory server IP.
        dir_port — directory server port.
        n_type   — 'entry', 'middle', or 'exit'.
        host     — this node's IP that clients should connect to.
        port     — this node's port.
        pub_pem  — PEM string of this node's RSA-2048 public key.
    """
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
    """
    Entry point — initialise node state, register, bind, and accept connections.

    How it works:
        1. Parses --type (required: entry/middle/exit), --host, --port,
           --dir-host, --dir-port, and --debug.

        2. Sets the global `node_type` and `debug` variables. These are read by
           handle_circuit_setup, handle_relay, and dbg() across all threads.
           They are set before any thread starts, so no lock is needed.

        3. Generates an RSA-2048 key pair. This is the most time-consuming step
           (~0.5–2 s). The private key is stored in the global `rsa_key`;
           only the public key is shared (sent to the directory server and from
           there to clients during circuit building).

        4. Calls register_with_directory to advertise this node.

        5. Binds a TCP listen socket with a 1-second timeout (for clean
           KeyboardInterrupt handling) and accepts connections in a loop.
           Each connection gets a dedicated daemon thread running
           handle_connection.

        6. On KeyboardInterrupt: the finally block iterates `circuits` and
           closes any remaining next_sock/dest_sock. This handles circuits that
           were open at shutdown time (handle_connection's finally won't run
           because the threads are daemon threads and are killed immediately).

    Why it exists:
        All global mutable state (rsa_key, node_type, debug, circuits) is
        set up here before the accept loop starts. Doing it in main() rather
        than at module level means restarting the process regenerates the RSA
        key, giving each run a fresh identity — consistent with the ephemeral
        nature of the relay nodes.
    """
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
