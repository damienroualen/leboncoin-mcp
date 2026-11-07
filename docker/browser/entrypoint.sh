#!/usr/bin/env bash
# Headed Chrome on a virtual display, reachable through noVNC (humans) and CDP (the mcp service).
set -euo pipefail

PROFILE=/data/chrome
export DISPLAY=:99

# A hard stop leaves these behind, and they would stop Xvfb or Chrome from starting.
rm -f /tmp/.X99-lock "$PROFILE"/Singleton{Lock,Cookie,Socket}

Xvfb :99 -screen 0 1440x900x24 -nolisten tcp &
until [ -S /tmp/.X11-unix/X99 ]; do sleep 0.1; done

openbox &
x11vnc -display :99 -forever -shared -localhost -nopw -rfbport 5900 -quiet &
websockify --web=/usr/share/novnc 6080 localhost:5900 &

# Chrome only binds CDP to loopback; socat exposes it to the private compose network.
socat TCP-LISTEN:9222,fork,reuseaddr,bind=0.0.0.0 TCP:127.0.0.1:9223 &

google-chrome \
  --user-data-dir="$PROFILE" \
  --remote-debugging-port=9223 \
  --no-first-run \
  --no-default-browser-check \
  --password-store=basic \
  --lang=fr-FR \
  --window-size=1440,900 \
  https://www.leboncoin.fr/ &

trap 'kill $(jobs -p) 2>/dev/null' TERM INT
# If any process dies the container exits, and compose restarts the whole stack cleanly.
wait -n
