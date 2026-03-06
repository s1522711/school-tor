#!/usr/bin/env bash
cd "$(dirname "$0")"

# Activate venv
if [ -f "venv/bin/activate" ]; then
    source venv/bin/activate
elif [ -f "venv/Scripts/activate" ]; then
    source venv/Scripts/activate
fi

TERM_PIDS=()
DEBUG_FLAG=""
NODE_COUNT=1

# Parse arguments (any order)
while [[ $# -gt 0 ]]; do
    case "$1" in
        --debug) DEBUG_FLAG="--debug"; shift ;;
        --nodes) NODE_COUNT="$2"; shift 2 ;;
        *) echo "Unknown flag: $1" >&2; exit 1 ;;
    esac
done

echo "Starting onion network ($NODE_COUNT node(s) per type)..."
[[ "$DEBUG_FLAG" ]] && echo "Debug mode enabled for nodes."

cleanup() {
    echo ""
    echo "Stopping all components..."
    pkill -f "python Servers/directory_server.py" 2>/dev/null || true
    pkill -f "python Servers/chat_server.py"      2>/dev/null || true
    pkill -f "python Servers/node.py"             2>/dev/null || true
    pkill -f "python Servers/chat_client.py"      2>/dev/null || true
    for pid in "${TERM_PIDS[@]}"; do
        kill "$pid" 2>/dev/null || true
    done
    echo "Done."
}

trap cleanup EXIT INT TERM

# ── Launch helpers ────────────────────────────────────────────────────────────

launch_gnome() {
    gnome-terminal --title="$1" -- bash -c "$2" &
    TERM_PIDS+=($!)
}

launch_xterm() {
    xterm -title "$1" -e bash -c "$2" &
    TERM_PIDS+=($!)
}

launch_macos() {
    osascript -e "tell application \"Terminal\" to do script \"cd '$PWD' && source venv/bin/activate && $2\"" &
}

launch() {
    local title="$1"
    local cmd="$2"
    if command -v gnome-terminal &>/dev/null; then
        launch_gnome "$title" "$cmd"
    elif command -v xterm &>/dev/null; then
        launch_xterm "$title" "$cmd"
    elif command -v osascript &>/dev/null; then
        launch_macos "$title" "$cmd"
    else
        echo "ERROR: No supported terminal emulator found (gnome-terminal, xterm, macOS Terminal)." >&2
        exit 1
    fi
}

# ── Start components ──────────────────────────────────────────────────────────

launch "Dir-Server"  "python Servers/directory_server.py"
sleep 1

launch "Chat-Server" "python Servers/chat_server.py --port 8001"
sleep 1

# Entry:  ports 9001..900N
# Middle: ports 9101..910N
# Exit:   ports 9201..920N
for ((i=1; i<=NODE_COUNT; i++)); do
    ENTRY_PORT=$((9000 + i))
    MIDDLE_PORT=$((9100 + i))
    EXIT_PORT=$((9200 + i))
    launch "Entry-Node-$i"  "python Servers/node.py --type entry  --port $ENTRY_PORT  $DEBUG_FLAG"
    sleep 1
    launch "Middle-Node-$i" "python Servers/node.py --type middle --port $MIDDLE_PORT $DEBUG_FLAG"
    sleep 1
    launch "Exit-Node-$i"   "python Servers/node.py --type exit   --port $EXIT_PORT   $DEBUG_FLAG"
    sleep 1
done

sleep 1
launch "Chat-Client-Tor" "python Servers/chat_client.py --tor --port 8001"

echo "All components running. Press Ctrl+C to stop everything."
while true; do sleep 1; done
