#!/bin/bash
set -e

# Ensure the non-root user can write to mounted volumes on every start.
chown -R app:app /data /downloads

set +e
as_app() { exec setpriv --reuid=app --regid=app --clear-groups "$@"; }

as_app python -m app.main &
as_app python -m shop.app &

# Both services ship together: when either exits, stop the other and let Docker restart the container.
trap 'kill -TERM $(jobs -p) 2>/dev/null' TERM INT
wait -n
status=$?
kill -TERM $(jobs -p) 2>/dev/null
wait
exit "$status"
