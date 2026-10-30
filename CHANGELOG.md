# Changelog

Notable changes to ophyd-websocket, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[PEP 440](https://peps.python.org/pep-0440/).

## [Unreleased]

Queue Server API-key auth (`feat/staging-auth`).

### Added
- Optional API-key auth, off by default. Turn it on with
  `OAS_AUTH_ENABLED=true`. When it's off, the server logs a warning at
  startup and the sockets make no Queue Server calls, as before.
- Keys are the same keys the Bluesky Queue Server HTTP server uses. For each
  key, the server asks the Queue Server for its effective scopes
  (`GET /api/auth/scopes`) at `OAS_AUTH_QSERVER_URL`. The default URL is
  built from `QSERVER_HTTP_SERVER_HOST` and `QSERVER_HTTP_SERVER_PORT`.
  ophyd-websocket keeps no users or keys of its own.
- Scopes map to operations as follows. Each scope can be changed with an env
  variable:
  - `read:monitor` (`OAS_AUTH_READ_SCOPE`): REST GETs, `POST /pvs/{pv}`,
    subscribing and refreshing, and the camera and TIFF streams.
  - `write:queue:control` (`OAS_AUTH_WRITE_SCOPE`): `PUT /pvs`,
    `PUT /devices`, `POST /load-devices` and the socket `set` action.
  - `read:console` (`OAS_AUTH_CONSOLE_SCOPE`): `/qs-console-socket`.
- REST calls send `Authorization: ApiKey <key>`. The server answers 401 for
  a bad key, 403 for a missing scope and 503 when the Queue Server can't
  check the key. A failed check refuses the request; it never lets it
  through.
- Every socket's first message is `{"action": "auth", "api_key": "..."}`. The
  server replies `{"action": "auth", "ok": ..., "permissions": {...}}`. On
  failure it closes with 1008, or with 1013 when the Queue Server is
  unavailable. The socket subscribes to nothing and starts no camera or file
  worker until auth succeeds. The first message must be under 4 KB and
  arrive within `OAS_AUTH_WS_HANDSHAKE_TIMEOUT` seconds (default 5).
- Each socket `set` checks the key again, then runs the queue-safety check.
  A refused write replies with a `code` of `forbidden`, `unauthorized`,
  `auth_unavailable` or `queue_busy`.
- Open sockets check their key again every `OAS_AUTH_REVALIDATE_SECONDS`
  (default 30) and close if it was revoked or lost its scope. A successful
  read check is reused for `OAS_AUTH_READ_CACHE_SECONDS` (default 10).
- A client can send a new `auth` message during a session to switch keys. A
  failed switch keeps the old key. With auth off, the auth message gets a
  reply with full permissions, so one client works with either kind of
  server.
- `OAS_ALLOWED_ORIGINS`, when set and auth is on, refuses socket connections
  from browsers whose `Origin` is not in the list.
- `GET /api/v1/auth/permissions` returns `authEnabled`, `read`, `write`,
  `console` and `roles` for the caller's key, so a UI can hide write
  controls.
- `GET /api/v1/websockets` has an `authentication` section.
- The startup banner shows the auth settings.
- `useOphydPVSocket` and `useOphydDeviceSocket` take an `apiKey` option and
  return `permissions` and `authError`.
- README "Authentication" section, and `src/tests/test_auth.py`.
- `docker-compose-auth.yml`: the simulated stack with `OAS_AUTH_ENABLED=true`,
  plus Redis, a Queue Server RE Manager and the Bluesky HTTP server on the
  private network. None of the three publish ports.
- `qserver/`: the image for those services and the HTTP server config
  (`config.auth.yml`, moved from the repo root). It also holds a committed
  **demo** database (`bluesky_httpserver.sqlite`) with operator and observer
  users, their demo passwords (`demo-passwords.env`) and API keys
  (`demo-api-keys.env`). `generate_auth_db.sh` regenerates the database and
  keys. The HTTP server works on a copy of the database, so the committed
  file never changes.
- Test frontend: an "API key" panel that saves a key to localStorage, offers
  demo-key buttons under `docker-compose-auth.yml`, and shows the key's
  permissions. Set controls are disabled unless the key can write.
- Test frontend: local, finch-style `useOphydPVSocket`, `useOphydDeviceSocket`
  and `useImageSocket` hooks that send the API key. They replace finch's hooks
  and its `CameraCanvas`/`TIFFCanvas` until finch supports auth.

### Changed
- Keys and auth messages are never logged. Logs show an 8-character sha256
  fingerprint instead. The startup banner shows only whether
  `QSERVER_HTTP_SERVER_SINGLE_USER_API_KEY` is set, not its value. Raw
  invalid-JSON messages and camera init messages are no longer logged.
- The camera and TIFF sockets skip `auth` messages sent before their init
  message, so a client can always authenticate first.
- `OAS_ALLOWED_ORIGINS` parsing moved to `ophyd_websocket.auth`.

## [0.1.0]

First release.
