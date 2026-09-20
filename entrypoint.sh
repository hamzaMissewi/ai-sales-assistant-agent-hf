#!/bin/sh
set -e

# Render mounts the persistent disk root-owned. chown it for the app user,
# then drop privileges and run the real command (./start.sh).
if [ "$(id -u)" = "0" ]; then
  chown -R user:user /var/chroma 2>/dev/null || echo "WARNING: could not chown /var/chroma"
  exec setpriv --reuid=user --regid=user --init-groups "$@"
fi

exec "$@"