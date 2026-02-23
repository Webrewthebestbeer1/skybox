#!/bin/bash
# Hot-deploy app code to the running container via balena tunnel + host OS.
# Usage: ./deploy-hot.sh [device-uuid]
# Then restart the motor-control service from the balena dashboard.
#
# Automatically opens a balena tunnel, deploys, then closes it.

DEVICE="${1:-0197707}"
LOCAL_PORT=22222
APP_DIR="motor-control/app"
REMOTE_DIR="/usr/src/app"

cd "$(dirname "$0")" || exit 1

if [ ! -d "$APP_DIR" ]; then
  echo "Error: $APP_DIR not found."
  exit 1
fi

# Start tunnel in background
echo "Opening tunnel to device $DEVICE on port $LOCAL_PORT..."
balena device tunnel "$DEVICE" -p "$LOCAL_PORT:$LOCAL_PORT" &
TUNNEL_PID=$!

# Clean up tunnel on exit (success or failure)
cleanup() {
  if kill -0 "$TUNNEL_PID" 2>/dev/null; then
    kill "$TUNNEL_PID" 2>/dev/null
    wait "$TUNNEL_PID" 2>/dev/null
    echo "Tunnel closed."
  fi
}
trap cleanup EXIT

# Wait for tunnel to be ready
echo "Waiting for tunnel..."
for i in $(seq 1 15); do
  if ssh -p "$LOCAL_PORT" -o StrictHostKeyChecking=no -o ConnectTimeout=2 root@localhost true 2>/dev/null; then
    break
  fi
  if [ "$i" -eq 15 ]; then
    echo "Error: Tunnel failed to connect after 15 seconds."
    exit 1
  fi
  sleep 1
done
echo "Tunnel ready."

# Find the container ID for motor-control on the host
echo "Finding motor-control container..."
CONTAINER_ID=$(ssh -p "$LOCAL_PORT" -o StrictHostKeyChecking=no root@localhost \
  'balena-engine ps --format "{{.ID}} {{.Names}}" | grep motor' | awk '{print $1}')

if [ -z "$CONTAINER_ID" ]; then
  echo "Error: Could not find motor-control container."
  exit 1
fi

echo "Found container: $CONTAINER_ID"
echo "Uploading app code..."
tar -cf - -C "$APP_DIR" --exclude='static/wiring_img.jpg' . | \
  ssh -p "$LOCAL_PORT" -o StrictHostKeyChecking=no root@localhost \
  "balena-engine cp - ${CONTAINER_ID}:${REMOTE_DIR}"

DEPLOYED=false

if [ $? -eq 0 ]; then
  DEPLOYED=true
else
  echo "Deploy failed. Trying alternative method..."
  # Fallback: copy tar to host tmp, then balena-engine cp
  tar -cf - -C "$APP_DIR" --exclude='static/wiring_img.jpg' . | \
    ssh -p "$LOCAL_PORT" -o StrictHostKeyChecking=no root@localhost \
    "cat > /tmp/app.tar && balena-engine exec ${CONTAINER_ID} tar -xf - -C ${REMOTE_DIR} < /tmp/app.tar && rm /tmp/app.tar"

  if [ $? -eq 0 ]; then
    DEPLOYED=true
  else
    echo "Both methods failed. Check the error above."
    exit 1
  fi
fi

if [ "$DEPLOYED" = true ]; then
  echo "Code uploaded. Killing python3 to trigger restart..."
  ssh -p "$LOCAL_PORT" -o StrictHostKeyChecking=no root@localhost \
    "balena-engine exec ${CONTAINER_ID} pkill -f 'python3 server.py'"
  echo "Done. Server restarting with new code."
fi
