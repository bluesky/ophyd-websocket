#!/bin/sh
# Regenerates qserver/bluesky_httpserver.sqlite and qserver/demo-api-keys.env.
#
# Builds the qserver image and runs generate_auth_db.py inside it, so the
# database comes from the same bluesky-httpserver version docker-compose-auth.yml
# serves it with. Run from anywhere:
#
#   ./qserver/generate_auth_db.sh
#
# Every run mints NEW keys and invalidates the old ones, so commit both files
# together.
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
IMAGE=ophyd-websocket-qserver:generate

docker build -t "$IMAGE" "$HERE"
docker run --rm \
    --env-file "$HERE/demo-passwords.env" \
    -e OUT_DIR=/out \
    -v "$HERE:/out" \
    "$IMAGE" python /qserver/generate_auth_db.py
