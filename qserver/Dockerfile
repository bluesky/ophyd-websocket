# Bluesky Queue Server (RE Manager) + HTTP server, used only by
# docker-compose-auth.yml and qserver/generate_auth_db.sh.
#
# One image serves both roles; compose picks the command. The same pinned
# bluesky-httpserver both generates the committed bluesky_httpserver.sqlite and
# serves it, so the database schema always matches. Bump the pins together and
# regenerate the database (see DEVELOPMENT.md).
#
# No EPICS here: with no --startup-dir the RE Manager runs the queue server's
# built-in simulated profile (ophyd.sim devices), which is all the auth demo
# needs and keeps this image native on every architecture.
FROM python:3.12-slim

RUN pip install --no-cache-dir \
        bluesky-queueserver==0.0.25 \
        bluesky-httpserver==0.0.14 \
        # Imported by the HTTP server's spreadsheet upload route but not
        # declared as a dependency, so it fails to start without it.
        pandas

WORKDIR /qserver

COPY config.auth.yml entrypoint-http.sh generate_auth_db.py ./
RUN chmod +x entrypoint-http.sh && mkdir -p /data

ENV QSERVER_HTTP_SERVER_CONFIG=/qserver/config.auth.yml

# Default role is the RE Manager; compose overrides this for the HTTP server.
CMD ["start-re-manager", "--zmq-publish-console", "ON"]
