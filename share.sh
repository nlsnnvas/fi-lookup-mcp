#!/usr/bin/env bash
#
# share.sh — expose FI Explorer publicly for occasional sharing, for free.
#
# Brings up an AUTH-PROTECTED dashboard instance on a separate port and opens a
# Cloudflare "quick tunnel" (no account / no domain / no cost). Prints a public
# HTTPS URL + the credentials to hand out, then tears everything down on Ctrl-C —
# so the URL only lives while you're sharing.
#
# Your normal local instance (launchd, port 8765, no auth) is left untouched.
#
#   ./share.sh                 # random password, port 8766
#   FI_AUTH_USER=team ./share.sh
#   FI_AUTH_PASS=hunter2 SHARE_PORT=8770 ./share.sh
#
set -euo pipefail
cd "$(dirname "$0")"

PORT="${SHARE_PORT:-8766}"
USER_NAME="${FI_AUTH_USER:-demo}"
# Strong random password unless one is supplied.
PASS="${FI_AUTH_PASS:-$(LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 16)}"
APP_LOG="$(mktemp -t fi-share-app)"
TUN_LOG="$(mktemp -t fi-share-tunnel)"

command -v cloudflared >/dev/null || { echo "cloudflared not found — 'brew install cloudflared'"; exit 1; }
[ -x .venv/bin/python ] || { echo "no .venv — create it and 'pip install -r requirements.txt' first"; exit 1; }

APP_PID="" TUNNEL_PID=""
cleanup() {
    echo; echo "Stopping share session…"
    [ -n "$TUNNEL_PID" ] && kill "$TUNNEL_PID" 2>/dev/null || true
    [ -n "$APP_PID" ]    && kill "$APP_PID"    2>/dev/null || true
    rm -f "$APP_LOG" "$TUN_LOG"
}
trap cleanup EXIT INT TERM

# Auth ON, portal-check fan-out OFF, rate-limited — bound to localhost; only the
# local cloudflared process bridges it out.
echo "Starting auth-protected dashboard on 127.0.0.1:$PORT …"
FI_AUTH_USER="$USER_NAME" FI_AUTH_PASS="$PASS" \
FI_DISABLE_PORTAL_CHECKS=1 FI_RATE_LIMIT_PER_MIN=120 \
    .venv/bin/python web_app.py --port "$PORT" >"$APP_LOG" 2>&1 &
APP_PID=$!

for _ in $(seq 1 60); do
    curl -fsS -o /dev/null "http://127.0.0.1:$PORT/healthz" 2>/dev/null && break
    sleep 0.5
done

echo "Opening Cloudflare quick tunnel …"
# Account-less quick tunnels occasionally return a transient 500 from
# trycloudflare.com — retry a few times rather than giving up on one hiccup.
URL=""
for attempt in 1 2 3; do
    : > "$TUN_LOG"
    cloudflared tunnel --url "http://localhost:$PORT" >"$TUN_LOG" 2>&1 &
    TUNNEL_PID=$!
    for _ in $(seq 1 30); do
        URL=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$TUN_LOG" | head -1 || true)
        [ -n "$URL" ] && break
        kill -0 "$TUNNEL_PID" 2>/dev/null || break   # cloudflared exited early
        sleep 0.5
    done
    [ -n "$URL" ] && break
    echo "  tunnel attempt $attempt failed (Cloudflare quick-tunnel hiccup); retrying…"
    kill "$TUNNEL_PID" 2>/dev/null || true
    sleep 2
done
[ -n "$URL" ] || { echo "Tunnel did not come up after 3 tries; last log:"; cat "$TUN_LOG"; exit 1; }

cat <<EOF

==================== SHARE THIS ====================
  URL:      $URL
  Username: $USER_NAME
  Password: $PASS
====================================================
Public HTTPS, basic-auth protected, rate-limited, portal checks off.
Leave this running while sharing. Press Ctrl-C to stop — the URL dies immediately.

EOF

wait "$TUNNEL_PID"
