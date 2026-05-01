#!/usr/bin/env bash
cd "$(dirname "$0")"

DEBUG_FLAG=""
NODE_COUNT=1

while [[ $# -gt 0 ]]; do
    case "$1" in
        --debug) DEBUG_FLAG="--debug"; shift ;;
        --nodes) NODE_COUNT="$2"; shift 2 ;;
        *) echo "Unknown flag: $1" >&2; exit 1 ;;
    esac
done

echo "Starting onion network ($NODE_COUNT node(s) per type)..."
[[ -n "$DEBUG_FLAG" ]] && echo "Debug mode enabled for nodes."

# Open a new Terminal window running the given command.
# The window stays open after the process exits so you can read output.
launch() {
    local cmd="$2"
    osascript > /dev/null 2>&1 <<EOF
tell application "Terminal"
    do script "cd '$PWD' && $cmd"
end tell
EOF
}

# ── Start infrastructure ───────────────────────────────────────────────────────

launch "Dir-Server"  "uv run Servers/directory_server.py"
sleep 1

launch "Chat-Server" "uv run Servers/chat_server.py --port 8001"
sleep 1

# ── Start relay nodes ──────────────────────────────────────────────────────────
# Entry:  ports 9001..900+N
# Middle: ports 9101..910+N
# Exit:   ports 9201..920+N

for ((i=1; i<=NODE_COUNT; i++)); do
    ENTRY_PORT=$((9000 + i))
    MIDDLE_PORT=$((9100 + i))
    EXIT_PORT=$((9200 + i))

    launch "Entry-Node-$i"  "uv run Servers/node.py --type entry  --port $ENTRY_PORT  $DEBUG_FLAG"
    sleep 1
    launch "Middle-Node-$i" "uv run Servers/node.py --type middle --port $MIDDLE_PORT $DEBUG_FLAG"
    sleep 1
    launch "Exit-Node-$i"   "uv run Servers/node.py --type exit   --port $EXIT_PORT   $DEBUG_FLAG"
    sleep 1
done

sleep 1
# launch "Chat-Client-Tor" "uv run Servers/chat_client.py --tor --port 8001"

echo "All components running in separate Terminal windows."
