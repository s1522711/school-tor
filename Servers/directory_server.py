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
    if isinstance(data, dict):
        data = json.dumps(data).encode()
    elif isinstance(data, str):
        data = data.encode()
    sock.sendall(len(data).to_bytes(4, 'big') + data)


def handle_client(conn, addr):
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
    HOST = '0.0.0.0'
    PORT = 8000

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((HOST, PORT))
    server.listen(20)
    print(f"[DIR] Directory server listening on {HOST}:{PORT}")

    while True:
        conn, addr = server.accept()
        t = threading.Thread(target=handle_client, args=(conn, addr), daemon=True)
        t.start()


if __name__ == '__main__':
    main()
