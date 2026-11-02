#!/bin/sh
# Starts the Bluesky HTTP server on a throwaway copy of the seed database.
#
# The server writes to its database (login sessions, key last-used times), so
# the committed seed mounted at /seed is copied to /data on every start. That
# keeps the git copy pristine and resets the demo to the same keys each time.
set -eu

# config.auth.yml expands these with os.path.expandvars, which turns an unset
# variable into the literal, guessable text "${NAME}". Refuse to start instead.
: "${QSERVER_OPERATOR_PASSWORD:?must be set}"
: "${QSERVER_OBSERVER_PASSWORD:?must be set}"
: "${QSERVER_ZMQ_HOST:?must be set}"

SEED=/seed/bluesky_httpserver.sqlite
if [ ! -f "$SEED" ]; then
    echo "No seed database at $SEED; run qserver/generate_auth_db.sh" >&2
    exit 1
fi
cp "$SEED" /data/bluesky_httpserver.sqlite
export QSERVER_HTTP_SERVER_DATABASE_URI=sqlite:////data/bluesky_httpserver.sqlite

exec start-bluesky-httpserver --host 0.0.0.0 --port 60610
