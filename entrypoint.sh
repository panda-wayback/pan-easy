#!/bin/sh
set -e

# Ensure the non-root user can write to mounted volumes on every start.
chown -R app:app /data /downloads

exec setpriv --reuid=app --regid=app --clear-groups python -m app.main
