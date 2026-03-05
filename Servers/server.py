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
    if isinstance(data, str):
        data = data.encode()
    sock.sendall(len(data).to_bytes(4, 'big') + data)


def handle_client(conn, addr):
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
    parser = argparse.ArgumentParser(description='Destination Server')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=9000)
    args = parser.parse_args()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((args.host, args.port))
    srv.listen(10)
    print(f"[SERVER] Listening on {args.host}:{args.port}  (plaintext / no AES)")

    while True:
        conn, addr = srv.accept()
        t = threading.Thread(target=handle_client, args=(conn, addr), daemon=True)
        t.start()


if __name__ == '__main__':
    main()
