#!/bin/sh
set -e

DISPLAY_NUM=${DISPLAY_NUM:-99}
SCREEN_GEOMETRY=${SCREEN_GEOMETRY:-1920x1080x24}

# -nolisten tcp keeps the display off the network; the browser uses the unix
# socket. Deliberately NOT xvfb-run: that wrapper does not forward signals, so
# SIGTERM would kill an in-flight crawl instead of draining it.
Xvfb ":${DISPLAY_NUM}" -screen 0 "${SCREEN_GEOMETRY}" -nolisten tcp &
XVFB_PID=$!
export DISPLAY=":${DISPLAY_NUM}"

i=0
while [ ! -e "/tmp/.X11-unix/X${DISPLAY_NUM}" ]; do
  if ! kill -0 "$XVFB_PID" 2>/dev/null; then
    echo "Xvfb exited before the display was ready" >&2
    exit 1
  fi
  i=$((i + 1))
  if [ "$i" -ge 100 ]; then
    echo "Timed out waiting for Xvfb display :${DISPLAY_NUM}" >&2
    exit 1
  fi
  sleep 0.1
done

exec "$@"
