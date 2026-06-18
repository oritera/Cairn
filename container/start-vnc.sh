#!/bin/bash
set -e
Xvfb :99 -screen 0 1920x1080x24 -ac &
sleep 0.5
fluxbox &
sleep 0.5
x11vnc -display :99 -forever -nopw -listen 0.0.0.0 -rfbport 5900 -shared &
websockify --web /usr/share/novnc 6080 localhost:5900 &
echo "=== noVNC ready: http://<HOST_IP>:6080/vnc.html ==="
if [ $# -gt 0 ]; then exec "$@"; else wait; fi
