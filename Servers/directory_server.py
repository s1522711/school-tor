"""
Directory Server
Nodes register here on startup. Clients query here to discover the circuit nodes.
"""

import socket
import json
import threading
import time

nodes = []
nodes_lock = threading.Lock()

_CHECK_INTERVAL = 10   # seconds between health check rounds
_MAX_FAILURES   = 3    # consecutive failures before a node is removed


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


def check_node(node: dict) -> bool:
    """
    Open a short-lived TCP connection to node, send PING, expect pong.
    Returns True if the node responds correctly within 5 seconds.
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(5.0)
        s.connect((node['host'], node['port']))
        send_msg(s, {'type': 'PING'})
        raw = recv_msg(s)
        s.close()
        if raw is None:
            return False
        return json.loads(raw).get('status') == 'pong'
    except Exception:
        return False


def health_check_loop():
    """
    Background daemon thread: every _CHECK_INTERVAL seconds, ping every
    registered node. Increment its fail_count on failure; reset to 0 on
    success. Remove the node once fail_count reaches _MAX_FAILURES.
    """
    while True:
        time.sleep(_CHECK_INTERVAL)

        with nodes_lock:
            snapshot = [(n['host'], n['port']) for n in nodes]

        for host, port in snapshot:
            # Find the live node entry (may have been removed or re-registered
            # since we took the snapshot, so look it up fresh under the lock).
            with nodes_lock:
                node = next((n for n in nodes if n['host'] == host and n['port'] == port), None)
            if node is None:
                continue

            alive = check_node(node)

            with nodes_lock:
                # Re-find by host:port in case the list changed during the check.
                entry = next((n for n in nodes if n['host'] == host and n['port'] == port), None)
                if entry is None:
                    continue
                if alive:
                    entry['fail_count'] = 0
                else:
                    entry['fail_count'] += 1
                    fails = entry['fail_count']
                    label = f"{entry['node_type']} at {host}:{port}"
                    if fails >= _MAX_FAILURES:
                        nodes.remove(entry)
                        print(f"[DIR] Removed unresponsive {label} after {_MAX_FAILURES} failed checks")
                    else:
                        print(f"[DIR] Health check failed ({fails}/{_MAX_FAILURES}): {label}")


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
                    'node_type':  msg['node_type'],
                    'host':       msg['host'],
                    'port':       msg['port'],
                    'public_key': msg['public_key'],
                    'fail_count': 0,
                })
            print(f"[DIR] Registered {msg['node_type']} node at {msg['host']}:{msg['port']}")
            send_msg(conn, {'status': 'ok'})

        elif msg['type'] == 'GET_NODES':
            with nodes_lock:
                public = [
                    {k: v for k, v in n.items() if k != 'fail_count'}
                    for n in nodes
                ]
            send_msg(conn, {'nodes': public})

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

    checker = threading.Thread(target=health_check_loop, daemon=True)
    checker.start()

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
