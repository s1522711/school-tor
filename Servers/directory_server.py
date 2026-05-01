"""
Directory Server
Nodes register here on startup. Clients query here to discover the circuit nodes.
"""

import socket
import json
import threading

nodes = []
nodes_lock = threading.Lock()


def recv_msg(sock):
    """
    Read one complete length-prefixed message from a TCP socket.

    How it works:
        TCP is a stream protocol — it does not preserve message boundaries on
        its own. To know where one message ends and the next begins, every
        message in this system is preceded by a 4-byte big-endian integer that
        states the number of payload bytes that follow.

        The function first reads exactly 4 bytes (looping because a single
        sock.recv() call may return fewer than requested), converts those bytes
        to an integer `length`, then reads exactly `length` more bytes in the
        same loop-until-complete fashion.

    Why it exists:
        Without framing, a receiver has no way to tell how many bytes belong to
        a given message — two messages could arrive in the same recv() call, or
        one message could be split across many calls. This function hides that
        complexity and always returns a single, complete message payload.

    Returns:
        bytes — the raw payload of the message, or None if the socket closed.
    """
    raw = b''
    while len(raw) < 4:
        chunk = sock.recv(4 - len(raw))
        if not chunk:
            return None
        raw += chunk
    length = int.from_bytes(raw, 'big')
    data = b''
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            return None
        data += chunk
    return data


def send_msg(sock, data):
    """
    Send data over a TCP socket with a 4-byte big-endian length prefix.

    How it works:
        Accepts a dict (serialised to JSON), a str (encoded to UTF-8), or raw
        bytes. Prepends len(data).to_bytes(4, 'big') and calls sendall() so
        the OS flushes the entire payload in one shot.

    Why it exists:
        Mirrors recv_msg — together they implement the framing protocol used
        by every component in the system. Using sendall() (rather than send())
        guarantees the whole message is written even if the kernel buffer is
        temporarily full.
    """
    if isinstance(data, dict):
        data = json.dumps(data).encode()
    elif isinstance(data, str):
        data = data.encode()
    sock.sendall(len(data).to_bytes(4, 'big') + data)


def handle_client(conn, addr):
    """
    Handle one incoming connection to the directory server.

    How it works:
        Reads exactly one message, inspects its 'type' field, and responds:

        REGISTER — a node announcing its presence.
            The node sends its type ('entry'/'middle'/'exit'), host, port, and
            RSA-2048 public key (PEM string). The handler removes any stale
            entry for the same host:port (in case the node restarted) and
            appends the new record. This upsert approach means the list never
            accumulates dead entries from node restarts.

        GET_NODES — a client asking for the full node list.
            Returns a JSON snapshot of the entire nodes list. The client will
            then pick one entry, one middle and one exit node at random.

        The connection is always closed after one request; the directory is
        purely request/response with no persistent state per connection.

    Why it exists:
        The directory server is the bootstrap point for the whole network.
        Without it, clients would have no way to discover which nodes exist or
        obtain their public keys (needed for hybrid circuit-setup encryption).

    Args:
        conn — the accepted TCP socket for this client.
        addr — (host, port) tuple, used only for logging.
    """
    try:
        raw = recv_msg(conn)
        if not raw:
            return
        msg = json.loads(raw)

        if msg['type'] == 'REGISTER':
            with nodes_lock:
                # Replace existing entry for same host:port if re-registering
                nodes[:] = [n for n in nodes if not (n['host'] == msg['host'] and n['port'] == msg['port'])]
                nodes.append({
                    'node_type': msg['node_type'],
                    'host': msg['host'],
                    'port': msg['port'],
                    'public_key': msg['public_key'],
                })
            print(f"[DIR] Registered {msg['node_type']} node at {msg['host']}:{msg['port']}")
            send_msg(conn, {'status': 'ok'})

        elif msg['type'] == 'GET_NODES':
            with nodes_lock:
                send_msg(conn, {'nodes': list(nodes)})

    except Exception as e:
        print(f"[DIR] Error handling {addr}: {e}")
    finally:
        conn.close()


def main():
    """
    Entry point — bind, listen, and dispatch one thread per connection.

    How it works:
        Creates a TCP server socket with SO_REUSEADDR (so restarts don't have
        to wait for TIME_WAIT to expire). Sets a 1-second accept() timeout so
        the KeyboardInterrupt check in the outer while-loop fires promptly —
        without the timeout, accept() would block indefinitely and Ctrl+C
        would not be noticed until a new connection arrived.

        Each accepted connection gets its own daemon thread running
        handle_client. Daemon threads are killed automatically when the main
        thread exits, so no explicit thread cleanup is needed on shutdown.

    Why it exists:
        This is a standalone process — it needs its own accept loop. The
        settimeout + try/except pattern is the standard Python idiom for a
        server that can be stopped cleanly with Ctrl+C.
    """
    HOST = '0.0.0.0'
    PORT = 8000

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((HOST, PORT))
    server.listen(20)
    server.settimeout(1.0)
    print(f"[DIR] Directory server listening on {HOST}:{PORT}")

    try:
        while True:
            try:
                conn, addr = server.accept()
            except socket.timeout:
                continue
            t = threading.Thread(target=handle_client, args=(conn, addr), daemon=True)
            t.start()
    except KeyboardInterrupt:
        print("\n[DIR] Shutting down...")
    finally:
        server.close()


if __name__ == '__main__':
    main()
