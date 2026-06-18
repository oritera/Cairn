#!/bin/bash
set -e

# --- Start virtual display ---
echo "[VNC] Starting Xvfb on ${DISPLAY}"
Xvfb "${DISPLAY}" -screen 0 1920x1080x24 -ac +extension RANDR &
XVFB_PID=$!
sleep 0.5

# --- Lightweight window manager ---
echo "[VNC] Starting fluxbox"
fluxbox &
FLUXBOX_PID=$!
sleep 0.5

# --- VNC server ---
echo "[VNC] Starting x11vnc on port ${VNC_PORT}"
x11vnc -display "${DISPLAY}" \
    -forever \
    -nopw \
    -listen 0.0.0.0 \
    -rfbport "${VNC_PORT}" \
    -xkb \
    -noxrecord \
    -noxfixes \
    -noxdamage \
    -wait 5 \
    -shared &
X11VNC_PID=$!

# --- noVNC web client ---
echo "[VNC] Starting noVNC on port ${NOVNC_PORT}"
websockify --web /usr/share/novnc "${NOVNC_PORT}" localhost:"${VNC_PORT}" &
WEBSOCKIFY_PID=$!

echo "[VNC] ========================================"
echo "[VNC]   Remote browser: http://<THIS_HOST>:${NOVNC_PORT}/vnc.html"
echo "[VNC] ========================================"
echo "[VNC]   DISPLAY=${DISPLAY}"
echo "[VNC]   VNC port=${VNC_PORT}"
echo "[VNC]   noVNC port=${NOVNC_PORT}"
echo "[VNC] ========================================"

# Allow services to exit cleanly
cleanup() {
    echo "[VNC] Shutting down..."
    kill ${WEBSOCKIFY_PID} 2>/dev/null || true
    kill ${X11VNC_PID} 2>/dev/null || true
    kill ${FLUXBOX_PID} 2>/dev/null || true
    kill ${XVFB_PID} 2>/dev/null || true
    wait
}
trap cleanup EXIT

# Hand off to container CMD or keep alive
if [ $# -gt 0 ]; then
    exec "$@"
else
    wait
fi
