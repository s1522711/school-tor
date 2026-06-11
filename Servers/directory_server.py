"""
Directory Server
Nodes register here on startup. Clients query here to discover the circuit nodes.
"""

import socket
import json
import threading
import time

nodes = [] # list of dicts: {node_type, host, port, public_key, fail_count}
nodes_lock = threading.Lock() # protects the nodes list from concurrent access by the main thread and the health check thread

_CHECK_INTERVAL = 10   # seconds between health check rounds
_MAX_FAILURES   = 3    # consecutive failures before a node is removed


def recv_msg(sock):
    """
    Read one complete length-prefixed message from a TCP socket.

    How it works:
        Reads a 4-byte big-endian header to learn the payload size, then reads
        exactly that many bytes, looping in both cases because a single recv()
        call on a TCP stream can return fewer bytes than requested.
        Returns None on clean EOF (the client disconnected).

    Why it exists:
        TCP is a stream protocol. Without explicit framing, two successive JSON
        messages could arrive fused in one recv() call, or a single message
        could be split across multiple calls. This function makes the chat
        server completely independent of packet boundaries.

    Returns:
        bytes — the raw JSON payload, or None if the socket closed.
    """
    header = b''
    while len(header) < 4: # loop until we have the full 4-byte header
        chunk = sock.recv(4 - len(header))
        if not chunk:
            return None
        header += chunk
    length = int.from_bytes(header, 'big') # parse the length from the header
    data = b''
    while len(data) < length: # loop until we have the full payload
        chunk = sock.recv(length - len(data))
        if not chunk:
            return None
        data += chunk
    return data


def send_msg(sock, data):
    """
    Send data over TCP with a 4-byte big-endian length prefix.

    How it works:
        Accepts dict (JSON bytes), str (UTF-8 bytes), or raw bytes.
        Prepends the 4-byte length and calls sendall() which loops until all
        bytes have been handed to the kernel, even if the send buffer is full.

    Why it exists:
        The paired receiving side (recv_msg) expects this exact framing.
        sendall() is used rather than send() because a broken socket or full
        buffer could cause send() to write only a partial payload without
        raising an exception, silently corrupting the stream.
    """
    if isinstance(data, dict): # convert dict to JSON bytes
        data = json.dumps(data).encode()
    elif isinstance(data, str): # convert str to UTF-8 bytes
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
        if raw is None: # clean EOF, node didn't respond
            return False
        return json.loads(raw).get('status') == 'pong'
    except Exception:
        return False


def health_check_loop():
    """
    Background daemon thread: every _CHECK_INTERVAL seconds, ping every
    registered node. Increment its fail_count on failure, reset to 0 on
    success. Remove the node once fail_count reaches _MAX_FAILURES.
    """
    while True:
        time.sleep(_CHECK_INTERVAL)

        with nodes_lock: # take a snapshot of the current nodes list to iterate over without holding the lock
            snapshot = [(n['host'], n['port']) for n in nodes]

        for host, port in snapshot:
            # Find the live node entry (may have been removed or re-registered
            # since we took the snapshot, so look it up fresh under the lock).
            with nodes_lock:
                node = next((n for n in nodes if n['host'] == host and n['port'] == port), None)
            if node is None: # node was removed or re-registered while we were looking, skip it
                continue
            
            # Check if the node is alive without holding the lock.
            alive = check_node(node)

            with nodes_lock:
                # Re-find by host:port in case the list changed during the check.
                entry = next((n for n in nodes if n['host'] == host and n['port'] == port), None)
                if entry is None: # node was removed or re-registered while we were checking, skip it
                    continue
                if alive:
                    entry['fail_count'] = 0
                else: # node failed the health check, increment fail_count and remove if it exceeds the threshold
                    entry['fail_count'] += 1 # increment fail_count on failure
                    fails = entry['fail_count'] # read the updated fail_count for logging
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
            appends the new record. This replace approach means the list never
            accumulates dead entries from node restarts.

        GET_NODES — a client asking for the full node list.
            Returns a JSON snapshot of the entire nodes list. The client will
            then pick one entry, one middle and one exit node at random.

        The connection is always closed after one request, the directory is
        purely request/response with no persistent state per connection.

    Why it exists:
        The directory server is the starting point for the whole network.
        Without it, clients would have no way to discover which nodes exist or
        obtain their public keys (needed for hybrid circuit-setup encryption).

    Args:
        conn — the accepted TCP socket for this client.
        addr — (host, port) tuple, used only for logging.
    """
    try: # read one message, respond, and close
        raw = recv_msg(conn)
        if not raw:
            return
        msg = json.loads(raw) # parse the JSON message into a dict

        if msg['type'] == 'REGISTER': # a node is registering itself
            with nodes_lock:
                # Replace existing entry for same host:port if re-registering
                nodes[:] = [n for n in nodes if not (n['host'] == msg['host'] and n['port'] == msg['port'])] # remove any existing entry for the same host:port (in case of node restart)
                nodes.append({
                    'node_type':  msg['node_type'],
                    'host':       msg['host'],
                    'port':       msg['port'],
                    'public_key': msg['public_key'],
                    'fail_count': 0,
                })
            print(f"[DIR] Registered {msg['node_type']} node at {msg['host']}:{msg['port']}")
            send_msg(conn, {'status': 'ok'})

        elif msg['type'] == 'GET_NODES': # a client is requesting the node list
            with nodes_lock:
                by_type: dict[str, list] = {} # group nodes by type, stripping the fail_count field since it's internal state that clients don't need to see
                for node in nodes:
                    entry = {key: value for key, value in node.items() if key != 'fail_count'}
                    by_type.setdefault(node['node_type'], []).append(entry)
            send_msg(conn, {'nodes': by_type})

    except Exception as e: # catch all exceptions to prevent one bad client from crashing the server, log the error and close the connection
        print(f"[DIR] Error handling {addr}: {e}")
    finally:
        conn.close()


def main():
    """
    Entry point: bind, listen, and dispatch one thread per connection.

    How it works:
        Creates a TCP server socket with SO_REUSEADDR (so restarts don't have
        to wait for TIME_WAIT to expire). Sets a 1-second accept() timeout so
        the KeyboardInterrupt check in the outer while-loop fires promptly,
        without the timeout, accept() would block indefinitely and Ctrl+C
        would not be noticed until a new connection arrived.

        Each accepted connection gets its own daemon thread running
        handle_client. Daemon threads are killed automatically when the main
        thread exits, so no explicit thread cleanup is needed on shutdown.

    Why it exists:
        This is a standalone process, it needs its own accept loop. The
        settimeout + try/except pattern is the standard Python template for a
        server that can be stopped cleanly with Ctrl+C.
    """
    # Configuration: bind to all interfaces on port 8000
    HOST = '0.0.0.0'
    PORT = 8000

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((HOST, PORT))
    server.listen(20)
    server.settimeout(1.0)
    print(f"[DIR] Directory server listening on {HOST}:{PORT}")

    # Start the health check thread as a daemon so it doesn't block shutdown.
    checker = threading.Thread(target=health_check_loop, daemon=True)
    checker.start()

    try: # main accept loop, spawns a new thread for each incoming connection
        while True:
            try:
                conn, addr = server.accept()
            except socket.timeout:
                continue
            t = threading.Thread(target=handle_client, args=(conn, addr), daemon=True)
            t.start()
    except KeyboardInterrupt: # allow Ctrl+C to stop the server cleanly
        print("\n[DIR] Shutting down...")
    finally:
        server.close()


if __name__ == '__main__':
    main()
