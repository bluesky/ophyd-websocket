# Changelog

Notable changes to ophyd-websocket, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[PEP 440](https://peps.python.org/pep-0440/).

## [Unreleased] — 0.1.1b1

Merged from the `feat/ophyd-async` → `feat/h5-stream` → `feat/video-stream`
chain. Each sub-section below is one branch.

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

## [0.1.0]

First release.
