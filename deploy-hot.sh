#!/bin/bash
# Hot-deploy app code to the running container via balena tunnel + host OS.
# Usage: ./deploy-hot.sh [device-uuid]
# Then restart the motor-control service from the balena dashboard.
#
# Requires a tunnel to be running in another terminal:
#   balena device tunnel 0197707 -p 22222:22222

DEVICE="${1:-0197707}"
LOCAL_PORT=22222
APP_DIR="motor-control/app"
REMOTE_DIR="/usr/src/app"

cd "$(dirname "$0")" || exit 1

if [ ! -d "$APP_DIR" ]; then
  echo "Error: $APP_DIR not found."
  exit 1
fi

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

if [ $? -eq 0 ]; then
  echo "Done. Restart the motor-control service from the balena dashboard."
else
  echo "Deploy failed. Trying alternative method..."
  # Fallback: copy tar to host tmp, then balena-engine cp
  tar -cf - -C "$APP_DIR" --exclude='static/wiring_img.jpg' . | \
    ssh -p "$LOCAL_PORT" -o StrictHostKeyChecking=no root@localhost \
    "cat > /tmp/app.tar && balena-engine exec ${CONTAINER_ID} tar -xf - -C ${REMOTE_DIR} < /tmp/app.tar && rm /tmp/app.tar"

  if [ $? -eq 0 ]; then
    echo "Done (fallback method). Restart the motor-control service from the balena dashboard."
  else
    echo "Both methods failed. Check the error above."
    exit 1
  fi
fi
