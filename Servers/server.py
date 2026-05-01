"""
Destination Server
Receives plaintext messages from the exit node (no AES, just length-prefixed frames).
Responds with an echo reply.

Usage:
    python server.py
    python server.py --host 127.0.0.1 --port 9000
"""

import socket
import threading
import argparse


def recv_msg(sock):
    """
    Read one complete length-prefixed message from a TCP socket.

    How it works:
        Reads a 4-byte big-endian header that encodes the payload length, then
        reads exactly that many bytes. Both reads loop until complete because
        a single sock.recv() call may return fewer bytes than requested — TCP
        delivers a stream, not discrete messages.

    Why it exists:
        The exit node sends messages to this server using the same framing
        protocol (4-byte length + payload) as every other component in the
        system. This function is the receiving half of that protocol.

    Returns:
        bytes — the complete payload, or None if the socket closed mid-read.
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


def send_msg(sock, data: bytes):
    """
    Send bytes over TCP with a 4-byte big-endian length prefix.

    How it works:
        Accepts bytes or a str (auto-encoded to UTF-8). Prepends the 4-byte
        length and calls sendall() to guarantee the entire payload is written
        in one call, even if the OS send buffer is temporarily full.

    Why it exists:
        The exit node reads the echo response with recv_msg, which expects the
        same framing. Without the length prefix, the exit node would not know
        how many bytes constitute the response.
    """
    if isinstance(data, str):
        data = data.encode()
    sock.sendall(len(data).to_bytes(4, 'big') + data)


def handle_client(conn, addr):
    """
    Handle one connection: read one message, echo it back, close.

    How it works:
        This is a deliberately simple echo server used to verify that a Tor
        circuit is working end-to-end. It reads one framed message from the
        exit node (already decrypted by the time it arrives here — the exit
        node strips all AES layers), prints it, prepends "Echo: ", and sends
        it back. The exit node then re-encrypts the response for the backward
        trip through the circuit.

        One message per connection is intentional: the exit node opens a
        persistent TCP socket to this server when the circuit is set up, reuses
        it for every relay message, and closes it when the circuit tears down.
        Each handle_client call therefore maps to one relay message, after which
        the connection continues to exist but this thread has ended.

        Actually the dest_sock is persistent (opened once per circuit in
        handle_circuit_setup), so this function may be called multiple times
        on the same connection during the circuit lifetime. The loop in the
        exit handler calls recv_msg on the dest_sock each relay; each such read
        gives one echo reply. The thread here simply prints and echoes once then
        exits — but because the socket stays open, the next relay message will
        open a new handle_client thread via accept().

    Why it exists:
        Provides a concrete, observable destination at the far end of the onion
        circuit for testing purposes. The plaintext arriving here proves the
        onion was correctly unwrapped by the three relay nodes.

    Args:
        conn — accepted TCP socket.
        addr — (host, port) of the connecting party (the exit node's address).
    """
    print(f"[SERVER] Connection from {addr}")
    try:
        msg = recv_msg(conn)
        if msg is None:
            return
        text = msg.decode(errors='replace')
        print(f"[SERVER] Received: {text!r}")
        response = f"Echo: {text}"
        send_msg(conn, response.encode())
        print(f"[SERVER] Sent response back to exit node")
    except Exception as e:
        print(f"[SERVER] Error: {e}")
    finally:
        conn.close()


def main():
    """
    Entry point — parse CLI arguments, bind, listen, dispatch threads.

    How it works:
        Binds a TCP socket to --host/--port (default 127.0.0.1:9000).
        Uses a 1-second accept() timeout so KeyboardInterrupt is noticed
        promptly rather than only when the next connection arrives.
        Spawns a daemon thread per connection so the main thread stays free
        for accepting new ones.

    Why it exists:
        This is a standalone process that must run independently of the nodes.
        The exit node connects to this address when setting up a circuit
        (handle_circuit_setup stores it as dest_sock). Every subsequent RELAY
        message through that circuit delivers its payload here.
    """
    parser = argparse.ArgumentParser(description='Destination Server')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=9000)
    args = parser.parse_args()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((args.host, args.port))
    srv.listen(10)
    srv.settimeout(1.0)
    print(f"[SERVER] Listening on {args.host}:{args.port}  (plaintext / no AES)")

    try:
        while True:
            try:
                conn, addr = srv.accept()
            except socket.timeout:
                continue
            t = threading.Thread(target=handle_client, args=(conn, addr), daemon=True)
            t.start()
    except KeyboardInterrupt:
        print("\n[SERVER] Shutting down...")
    finally:
        srv.close()


if __name__ == '__main__':
    main()
