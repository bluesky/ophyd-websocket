# Changelog

Notable changes to ophyd-websocket, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[PEP 440](https://peps.python.org/pep-0440/).

## [Unreleased] — 0.1.1b1

Merged from the `feat/ophyd-async` → `feat/h5-stream` → `feat/video-stream`
→ `feat/qserver-auth` chain. Each sub-section below is one branch.

### Upgrading from 0.1.0

- Run the server with the installed `ophyd-websocket` command (or
  `python -m ophyd_websocket.server`). The modules now import as the
  `ophyd_websocket` package, so `python server.py` from inside `src/` no
  longer works. Auto-reload (`reload=True`) was removed.
- Devices in `--startup-dir` are loaded when the server starts. Calling
  `POST /load-devices` is no longer needed, and it still reloads them.
- `subscribe` and `unsubscribe` on `/device-socket` and `/pv-socket` now
  answer with one summary message (`subscribed`, `already_subscribed`,
  `failed`) instead of one text message per name.
- The license changed from BSD 3-Clause to the LBNL license in `LICENSE`.

### ophyd-async support (`feat/ophyd-async`)

#### Added
- ophyd-async devices and signals in the device registry, next to classic
  Ophyd devices. Read, subscribe and set go through a device adapter, and
  calls run on Bluesky's event loop.
- A startup directory runs all its files in one shared namespace, in name
  order, as bluesky-queueserver does. A later file can use names an
  earlier one defined.
- `/camera-socket-shared`: one shared decode/encode worker per camera,
  whatever the number of clients.
- `/tiff-socket`: streams the newest TIFF a detector saved, re-read when
  `<prefix>:TIFF1:FullFileName_RBV` changes.
- `subscribe` and `unsubscribe` accept a list (`devices` / `pvs`) in one
  message.
- An `ophyd-websocket` console script, and package metadata for PyPI.
- A React test harness in `src/frontend`, and caproto simulated IOCs
  (general PVs, an areaDetector, a TIFF writer).
- docker-compose setup, `DEVELOPMENT.md`, a GitHub Actions test workflow and
  a much larger test suite.
- `useOphydDeviceSocket` and `useOphydPVSocket` hooks in `react_examples`,
  replacing `useOphydSocket`.

#### Changed
- Async devices report one headline value (their hinted signal), plus
  units, limits, setpoint and moving state for the UI.
- Startup files are loaded off the event loop, so files that call
  `asyncio.run()` (such as bcsophyd's) work.

#### Fixed
- Setting a device that has more than one writable child.
- Subscription callbacks are removed when a websocket closes.
- Camera socket callbacks from Channel Access threads are handed to the
  event loop safely.
- numpy array PV values serialize to JSON.
- `PUT /devices` reports the value that was set.
- The queue-server status check passes the queue server's 401/403 errors
  through instead of turning them into 500s.

### HDF5 streaming (`feat/h5-stream`)

#### Added
- `/h5-socket`: streams the newest frame from an areaDetector HDF5 plugin.
  It follows `FullFileName_RBV` and `NumCaptured_RBV`, so frames appended
  to an open file show up. It reads SWMR files, and other files with HDF5
  file locking disabled. The dataset defaults to `/entry/data/data`.
- `/tiff-socket` and `/h5-socket` read their settings from the URL query
  string (for example `?prefix=SIMHDF1&dataset=...`), which overrides the
  first message.
- A caproto HDF5-writer IOC, and an HDF5 panel in the test harness.
- `h5py` dependency.

#### Changed
- TIFF and HDF5 sockets share one file-image worker module.

### Registry camera streaming (`feat/video-stream`)

#### Added
- `/camera-socket-shared?device=<name>` streams a camera from the device
  registry, such as bcsophyd's `BCSVideoCamera`. A device with an async
  `get_jpeg()` has its JPEG bytes forwarded unchanged; an ophyd-async
  `SignalR` of numpy arrays is encoded to JPEG. One poll loop serves every
  client of a camera, and it stops when the last client leaves.
- `OAS_CAMERA_POLL_FPS` sets the poll rate (default 2, max 10). Only the
  server sets it: a BCS server handles one request at a time for every
  client, the queueserver included.
- A settings message (`x`, `y`, `colorMode`, `dataType`) is sent before the
  first frame and again when the size or mode changes. A repeated frame is
  not sent again.
- `GET /devices` has a `cameras` list, and device info has a `kind` field.
- The test harness lists cameras in their own sidebar group, with a live
  view for each selected one.

#### Changed
- `/device-socket` refuses to subscribe to a camera, with an error naming
  the camera URL. Subscribing would poll and decode frames, and the arrays
  can't be sent as JSON.
- If a frame request fails, the socket stays open and retries with backoff
  up to 5 s. Only the first failure is logged as a warning.

#### Fixed
- A cancelled camera socket handler could skip its cleanup and leave the
  shared worker running. This applies to the EPICS shared camera path too.
- `src/frontend/src/lib/` was ignored by the root `lib/` rule in
  `.gitignore`.

### Queue Server API-key auth (`feat/qserver-auth`)

#### Added
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
    subscribing and refreshing, and the camera, TIFF and HDF5 streams.
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

#### Changed
- Keys and auth messages are never logged. Logs show an 8-character sha256
  fingerprint instead. The startup banner shows only whether
  `QSERVER_HTTP_SERVER_SINGLE_USER_API_KEY` is set, not its value. Raw
  invalid-JSON messages and camera init messages are no longer logged.
- The TIFF and HDF5 init message and query string drop `api_key`, `apikey`,
  `apiKey` and `token`. A key is only accepted in the auth message.
- `OAS_ALLOWED_ORIGINS` parsing moved to `ophyd_websocket.auth`.

### Numeric HDF5 stream (`feat/h5-raw-socket`)

#### Added
- `/api/v1/h5-array-socket` sends the newest HDF5 detector frame as its raw
  pixel values instead of a JPEG, for numeric plots (Finch's heatmap). It
  follows the file the same way `/h5-socket` does and takes the same init
  message (`pv` or `prefix`/`plugin`, `dataset`, `triggerPV`) and auth
  handshake. Each frame is one binary message: a uint32 little-endian header
  length, a JSON header (`version`, `width`, `height`, `dtype`, `sourceWidth`,
  `sourceHeight`, `binFactor`), then row-major little-endian pixels. Supported
  types are uint8/16/32, int8/16/32 and float32/64. Colour, bool, 64-bit
  integer and complex frames are refused with a `{"frameError": ...}` text
  message, and the socket stays open.
- `maxDimension` (64–4096, default 1024): a larger frame is mean-binned to
  float32 by one integer factor, with partial edge blocks averaged over their
  real pixels. A frame that fits is sent exactly, in its own dtype.
- `maxFps` (0.5–10, default 5) caps each client's send rate. Frames that land
  while a client waits are not queued; it gets the newest one.
- `serve_worker` takes `max_fps`, `allow_log_toggle` and `send_frame_errors`.
  Their defaults keep the existing sockets' behaviour.

#### Changed
- `h5_socket.read_latest_frame_if_new()` now does the HDF5 read and
  unchanged-frame check for both HDF5 sockets. `/h5-socket` output is
  unchanged.
- `SharedFileImageWorker` records the newest load error (`last_error`,
  `error_version`). Only the numeric socket sends it to clients.

#### Fixed
- HDF5 reads now run one at a time per process. With both `/h5-socket` and
  `/h5-array-socket` open on one detector, the two workers opened the file at
  the same moment on each `NumCaptured_RBV` update. h5py 3.16 / HDF5 2.0 then
  returned frame 0's data to one of them, so one view stayed stuck on the
  file's first frame.

## [0.1.0]

First release.
