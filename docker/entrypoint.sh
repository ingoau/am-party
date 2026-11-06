#!/bin/sh
# am-party container entrypoint.
#
# Docker creates a missing bind-mount source (./data) owned by root, which the
# unprivileged app user (uid 1000) can't write to. When started as root we fix the
# ownership of a root-owned data dir, then drop to uid/gid 1000 with setpriv
# (util-linux) before starting the app. Started as another user (compose `user:`),
# we only check that the data dir is writable and fail with an actionable message.
set -eu

DATA_DIR="${AM_PARTY_DATA_DIR:-/data}"
APP_UID=1000
APP_GID=1000

fail() {
    echo "ERROR: $1" >&2
    echo "       Fix on the host:  mkdir -p data && sudo chown -R $2 data" >&2
    exit 1
}

if [ "$(id -u)" = "0" ]; then
    mkdir -p "$DATA_DIR"
    if [ "$(stat -c %u "$DATA_DIR")" = "0" ]; then
        echo "am_party: $DATA_DIR is owned by root, handing it to uid $APP_UID" >&2
        chown -R "$APP_UID:$APP_GID" "$DATA_DIR"
    fi
    if ! setpriv --reuid="$APP_UID" --regid="$APP_GID" --init-groups test -w "$DATA_DIR"; then
        fail "$DATA_DIR is not writable by uid $APP_UID (owned by uid $(stat -c %u "$DATA_DIR"))." "$APP_UID:$APP_GID"
    fi
    exec setpriv --reuid="$APP_UID" --regid="$APP_GID" --init-groups env HOME=/home/am_party "$@"
fi

if [ ! -w "$DATA_DIR" ]; then
    fail "$DATA_DIR is not writable by uid $(id -u) (owned by uid $(stat -c %u "$DATA_DIR" 2>/dev/null || echo '?'))." "$(id -u):$(id -g)"
fi
exec "$@"
