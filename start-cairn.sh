#!/usr/bin/env bash
# Manual launcher for the Cairn stack: API server + dispatcher supervisor.
# The supervisor owns the dispatcher — start/stop/restart it from the dashboard's
# "Конфигуратор" (no need to touch the dispatcher process by hand).
#
#   ./start-cairn.sh                       # start with dispatch.yaml
#   ./start-cairn.sh dispatch.yaml.gpt     # start with a different config
#   ./start-cairn.sh stop                  # stop server + supervisor (+ its dispatcher)
#
# Idempotent: re-running won't double-start anything.
set -u
# resolve the repo dir from this script's own location, so it works on any machine
REPO_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
cd "$REPO_DIR" || { echo "cannot cd to repo"; exit 1; }
export PATH="$HOME/.local/bin:$PATH"
mkdir -p logs

if [ "${1:-}" = "stop" ]; then
  echo "stopping supervisor (and its dispatcher) + server …"
  pkill -f "[c]airn supervise" 2>/dev/null
  pkill -f "[c]airn dispatch"  2>/dev/null
  pkill -f "[c]airn serve"     2>/dev/null
  echo "stopped."
  exit 0
fi

CONFIG="${1:-dispatch.yaml}"
if [ ! -f "$CONFIG" ]; then
  echo "config not found: $CONFIG"
  echo "available: $(ls dispatch.yaml dispatch.yaml.* 2>/dev/null | tr '\n' ' ')"
  echo "run ./install.sh first, or copy dispatch.example.yaml -> dispatch.yaml and add your keys"
  exit 1
fi

start_if_absent() {
  local name="$1" pat="$2"; shift 2
  if pgrep -f "$pat" >/dev/null 2>&1; then
    echo "• $name already running"
  else
    echo "• starting $name …"
    nohup "$@" >>"logs/$name.log" 2>&1 &
    disown 2>/dev/null || true
  fi
}

start_if_absent server "[c]airn serve" uv run --project cairn cairn serve

# wait for the API to answer before the dispatcher leans on it
for i in $(seq 1 30); do
  curl -sf -o /dev/null http://127.0.0.1:8000/ && break
  sleep 0.5
done

start_if_absent supervisor "[c]airn supervise" uv run --project cairn cairn supervise --config "$CONFIG"

echo
echo "Cairn is up:"
echo "  dashboard : http://127.0.0.1:8000   (open a project → Navigation tab)"
echo "  config    : $CONFIG   (change / restart from the Конфигуратор button)"
echo "  logs      : logs/server.log · logs/supervisor.log · ~/.local/share/cairn/supervisor/dispatch.log"
echo "  stop with : ./start-cairn.sh stop"
