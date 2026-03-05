# school-tor

A simplified Tor-like onion routing network implemented in Python using raw TCP sockets.

Messages travel through three relay nodes (entry → middle → exit) before reaching a destination server. Each hop has its own AES-128 session key; messages are triple-encrypted on the way out and triple-decrypted on the way back. The exit→server leg is plaintext.

## How it works

```
Client
  │  AES_K1( AES_K2( AES_K3(message) ) )
  ▼
Entry node  ── decrypts K1 layer ──►  AES_K2( AES_K3(message) )
  ▼
Middle node ── decrypts K2 layer ──►  AES_K3(message)
  ▼
Exit node   ── decrypts K3 layer ──►  plaintext message
  ▼
Destination server (echo)
```

Responses travel back through the same circuit with each node re-encrypting its layer, so the client receives a triple-encrypted response it can fully unwrap.

Circuit setup uses hybrid encryption: each node's routing data is encrypted with RSA-OAEP (2048-bit), with the actual payload encrypted via AES-128-CBC using an ephemeral key. This allows arbitrarily large nested payloads while keeping RSA usage minimal.

## Repository layout

```
school-tor/
├── Servers/
│   ├── directory_server.py   # Node registry; clients query this to build circuits
│   ├── node.py               # Relay node (entry / middle / exit roles in one file)
│   ├── client.py             # Interactive client; builds circuit and sends messages
│   └── server.py             # Plaintext destination server (echo)
├── start.bat                 # Windows launcher
├── start.sh                  # Linux/macOS launcher
└── requirements.txt          # pycryptodome
```

## Setup

```bash
# Create and activate a virtual environment
python -m venv venv

# Windows
venv\Scripts\activate

# Linux / macOS
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

## Running

### Quick start (recommended)

```bash
# Windows
start.bat

# Linux / macOS
./start.sh
```

This launches the directory server, destination server, one entry/middle/exit node each, and the client — all in separate terminal windows.

### Options

```bash
start.bat --nodes 3        # 3 entry, 3 middle, 3 exit nodes
start.bat --debug          # hex-dump relay messages at each hop
start.bat --nodes 2 --debug
```

### Manual launch

Launch components in this order (directory server first, client last):

```bash
python Servers/directory_server.py
python Servers/server.py --port 9000

python Servers/node.py --type entry  --port 9001
python Servers/node.py --type middle --port 9002
python Servers/node.py --type exit   --port 9003

python Servers/client.py --dest-port 9000
```

## Port layout

| Component        | Default port         | With `--nodes N`      |
|------------------|----------------------|-----------------------|
| Directory server | 8000                 | 8000                  |
| Destination      | 9000                 | 9000                  |
| Entry nodes      | 9001                 | 9001 … 9000+N         |
| Middle nodes     | 9101                 | 9101 … 9100+N         |
| Exit nodes       | 9201                 | 9201 … 9200+N         |

## Usage

Once everything is running, the client presents an interactive prompt:

```
[CLIENT] Circuit established: a3f1b2c0
[CLIENT]   client -> 127.0.0.1:9001 (entry)
[CLIENT]          -> 127.0.0.1:9102 (middle)
[CLIENT]          -> 127.0.0.1:9203 (exit)
[CLIENT]          -> 127.0.0.1:9000 (server, raw)
[CLIENT] Ready. Type a message and press Enter. Ctrl+C to quit.

you > hello
srv < Echo: hello
```

Each message reuses the same circuit. Press `Ctrl+C` to disconnect and tear down the circuit.

## Cryptography

| Primitive | Details |
|-----------|---------|
| Relay encryption | AES-128-CBC, random IV prepended to ciphertext, PKCS7 padding |
| Circuit setup | RSA-2048 OAEP (ephemeral key only) + AES-128-CBC (payload) |
| Key generation | Fresh RSA key per node process; fresh AES relay keys per circuit |

## What each node knows

| Node      | Knows |
|-----------|-------|
| Entry     | Client address, middle address, K1; forward payload is opaque |
| Middle    | Entry address, exit address, K2; forward payload is opaque |
| Exit      | Middle address, server address, K3; sees plaintext message |
| Server    | Exit address, plaintext message |
| Directory | All node addresses and public keys; nothing about circuits or traffic |

## Dependencies

- [pycryptodome](https://pycryptodome.readthedocs.io/) — AES and RSA primitives
