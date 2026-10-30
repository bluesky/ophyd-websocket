# Developing ophyd-websocket

This guide covers running the project locally against simulated hardware: the
docker compose test stack, the simulated IOCs, the ophyd devices, and the
frontend. For the server's API, response types, and configuration, see the
[README](README.md).

## Building from source

Clone the repo and install with dev dependencies:
```bash
git clone https://github.com/bluesky/ophyd-websocket.git
cd ophyd-websocket
pip install uv        # one-time: install uv
uv sync --dev
```

Run the server from source:
```bash
uv run ophyd-websocket
```

## Tests

```bash
uv run pytest                  # everything
uv run pytest -m "not ioc"     # no EPICS needed (what CI runs on every push)
uv run pytest -m ioc           # only the Channel Access integration tests
```

Two layers, both under `src/tests/`:

| Layer | Files | How it works |
| --- | --- | --- |
| Route and socket tests | `test_core_api.py`, `test_pv_socket.py`, `test_device_socket.py`, `test_camera_socket.py`, `test_camera_shared_socket.py`, `test_tiff_socket.py`, `test_qs_console_socket.py`, `test_queue_safety.py`, `test_server_app.py` | Every endpoint is driven through a FastAPI `TestClient`. The ophyd classes each router imports (`EpicsSignal`, `EpicsSignalRO`, `EpicsMotor`, `PseudoPositioner`) are monkeypatched with the in-memory fakes in `fakes.py`, so no IOC, no libca connection, and no queue server is required. Tests push values by calling `signal.emit_value(...)`, which invokes the router's own subscription callbacks. |
| Channel Access integration | `test_websockets.py` (marked `ioc`) | Runs against the caproto IOC in `test_ioc.py` and the devices in `test_startup.py`, over real CA. |

The test IOC listens on CA port **5094**, not the default 5064, so it never
collides with a simulated detector or a real IOC already running on the machine.
Override with `OAS_TEST_IOC_CA_PORT`. `conftest.py` sets `EPICS_CA_ADDR_LIST`
and friends at import time, because libca reads them once when the CA context is
first created.

Adding a fake device to a socket test means giving the fake whatever surface the
handler touches — see `FakeSignalBase` in `fakes.py`. The concrete fakes are
deliberately *siblings*, never subclasses of each other, because
`device_socket.recursively_subscribe` dispatches with `isinstance`.

GitHub Actions (`.github/workflows/tests.yml`) runs the IOC-free suite on Python
3.12 and 3.13, and the CA integration suite in a separate job.

# Local test stack (docker compose)

A batteries-included stack for developing against ophyd-websocket without a
real beamline. Three services:

| Service | What it is | Where |
| --- | --- | --- |
| `caproto-ioc` | Simulated EPICS IOC serving `SIM:*` PVs | `caproto/sim_ioc.py` |
| `caproto-detector` | Simulated areaDetector serving `SIMDET1:*` PVs | `caproto/sim_detector_ioc.py` |
| `caproto-tiff-detector` | Simulated TIFF-writer serving `SIMTIFF1:*` PVs | `caproto/sim_tiff_detector_ioc.py` |
| `ophyd-websocket` | This server, with ophyd devices wrapping those PVs | `startup/` |
| `frontend` | Minimal React/Tailwind SPA with finch-style socket hooks | `src/frontend/` |

```bash
docker compose up --build
open http://localhost:5173
```

Ports: frontend `5173`, ophyd-websocket `8001`, IOC Channel Access `5064`.
`caproto-detector` is reachable only from inside the compose network — Channel
Access is port-sensitive and `caproto-ioc` already owns 5064 on the host.

## With authentication (`docker-compose-auth.yml`)

The same stack with `OAS_AUTH_ENABLED=true`, plus a Bluesky Queue Server that
validates API keys:

```bash
docker compose down                      # same container names and ports
docker compose -f docker-compose-auth.yml up --build
open http://localhost:5173               # then click "Demo operator" or "Demo observer"
```

| Service | What it is |
| --- | --- |
| `redis` | Queue storage for the RE Manager |
| `qserver` | RE Manager running the queue server's built-in simulated profile (`ophyd.sim`) |
| `qserver-http` | Bluesky HTTP server; users and roles from `qserver/config.auth.yml` |

The other services come from `docker-compose.yml` through `extends`. None of the
three new services publish ports: the browser only talks to ophyd-websocket,
which reaches `qserver-http:60610` over the compose network.

**Users and keys.** `qserver/config.auth.yml` defines two users. `operator` has
full control, including `write:queue:control`. `observer` is read-only. Their
passwords are in `qserver/demo-passwords.env`. API keys are minted once and
stored, sha256-hashed, in the committed `qserver/bluesky_httpserver.sqlite`. The
plaintext keys are in `qserver/demo-api-keys.env`:

| Variable | Key | Used by |
| --- | --- | --- |
| `VITE_DEMO_OPERATOR_API_KEY` | operator | the frontend's "Demo operator" button |
| `VITE_DEMO_OBSERVER_API_KEY` | observer | the frontend's "Demo observer" button |
| `QSERVER_HTTP_SERVER_SINGLE_USER_API_KEY` | observer | ophyd-websocket's own queue-safety `/api/status` check |

The last key is needed because the HTTP server ignores single-user keys once
users are configured. Without a minted key, the safety check would fail and
every write would be refused.

> These are **demo credentials, committed on purpose**. Never reuse the
> passwords, keys or database outside this stack.

`qserver-http` copies the seed database to `/data` inside the container at
every start, so the committed file is never modified and every `up` starts
from the same keys.

**Regenerating the database and keys** (e.g. after bumping the pinned
`bluesky-httpserver` in `qserver/Dockerfile`, or changing users in
`config.auth.yml`):

```bash
./qserver/generate_auth_db.sh
```

It builds the qserver image and runs `qserver/generate_auth_db.py` inside it.
The script starts the HTTP server on an empty database, logs in as each user,
and mints never-expiring keys with `"scopes": ["inherit"]`. It checks each key
with `GET /api/auth/scopes`, then writes `bluesky_httpserver.sqlite` and
`demo-api-keys.env` into `qserver/`. Every run mints new keys and invalidates
the old ones, so commit the two files together.

To mint a key by hand inside the running stack:

```bash
docker compose -f docker-compose-auth.yml exec qserver-http python -c "
import json, urllib.request, urllib.parse
B = 'http://localhost:60610/api/auth'
tok = json.load(urllib.request.urlopen(B + '/provider/local/token',
    urllib.parse.urlencode({'username': 'observer', 'password': 'observer-demo-password'}).encode()))
req = urllib.request.Request(B + '/apikey', method='POST',
    data=json.dumps({'scopes': ['inherit'], 'expires_in': None, 'note': 'manual'}).encode(),
    headers={'Authorization': 'Bearer ' + tok['access_token'], 'Content-Type': 'application/json'})
print(json.load(urllib.request.urlopen(req))['secret'])"
```

Keys minted this way live only in the container's `/data` copy and disappear on
the next restart.


# Running each service natively (no docker)

Sometimes it's easier to run the stack straight on your machine — e.g. to watch
the ophyd-websocket traceback live when it crashes under load. Run each of these
in its own terminal from the repo root.

All three caproto IOCs share UDP 5064 for name searches (`SO_REUSEPORT`), so the
default broadcast Channel Access resolution finds all of them at once — no
`EPICS_CA_ADDR_LIST` needed. The one exception is `EPICS_CA_MAX_ARRAY_BYTES`:
the camera's `image1:ArrayData` waveform is larger than CA's legacy default, so
export it before starting ophyd-websocket or camera frames never arrive.

```bash
# 1. Simulated IOC (SIM:* PVs)
uv run python caproto/sim_ioc.py --list-pvs
```
```bash
# 2. Simulated areaDetector (SIMDET1:* PVs) — the camera stream
uv run python caproto/sim_detector_ioc.py --list-pvs
```
```bash
# 3. Simulated TIFF-writer (SIMTIFF1:* PVs) — the /tiff-socket stream
uv run python caproto/sim_tiff_detector_ioc.py --list-pvs
```
```bash
# 4. ophyd-websocket (port 8001). The large-array cap is required for the
#    camera; the TIFF path reads files from disk and does not need it.
EPICS_CA_MAX_ARRAY_BYTES=10000000 uv run ophyd-websocket --startup-dir startup
```
```bash
# 5. Frontend dev server (port 5173) — open http://localhost:5173
cd src/frontend && npm install && npm run dev
```

The TIFF IOC writes absolute paths under `caproto/assets` into
`SIMTIFF1:TIFF1:FullFileName_RBV`, and ophyd-websocket opens those same paths on
the local filesystem, so running natively from the repo root just works. Set
`SIM_TIFF_ASSETS_DIR` only if you move the sample TIFFs elsewhere.


## The simulated IOC

`caproto/sim_ioc.py` is a caproto `PVGroup`. caproto is a dev dependency of
this project, so the IOC image is built from the same `uv.lock`.

| PVs | Behavior |
| --- | --- |
| `SIM:m1`, `SIM:m2`, `SIM:m3` | Full motor records (caproto's `FakeMotor`), different velocities and travel limits |
| `SIM:det:Counts`, `:Acquire`, `:ExposureTime`, `:ImageCounter`, `:PeakCenter`, `:PeakWidth` | Point detector; counts are a noisy gaussian in `m1`'s position, so moving `m1` toward `PeakCenter` (default 5.0) makes counts spike. Free-running by default |
| `SIM:temp:Setpoint`, `:Readback`, `:Heater`, `:AtSetpoint` | Temperature controller; the readback walks toward the setpoint only while `Heater` is `On` |
| `SIM:beam:Current`, `:Energy`, `:Energy_RBV`, `:Shutter`, `:Status` | Ring current decaying with top-up refills, energy readback slewing to its setpoint, a shutter enum, a status string |
| `SIM:sample:Name`, `:Barcode`, `:Filter`, `:StageIn` | String and enum PVs (`Filter` is a 5-choice mbbo) |
| `SIM:spectrum:Data`, `:NPoints` | 256-point waveform: two drifting gaussian peaks plus noise |
| `SIM:Uptime` | Seconds since IOC start |

Run it standalone (outside docker) with:

```bash
uv run python caproto/sim_ioc.py --list-pvs
```

## The simulated areaDetector

`caproto/sim_detector_ioc.py` mimics the slice of ADSimDetector that the camera
path actually touches: a `cam1:` driver group and an `image1:` array plugin
under the `SIMDET1:` prefix.

| PVs | Behavior |
| --- | --- |
| `SIMDET1:image1:ArrayData` | The frame buffer: an 8-bit CA waveform, regenerated live |
| `cam1:Acquire`, `AcquireTime`, `AcquirePeriod` | Start/stop, exposure (scales brightness), and frame interval |
| `cam1:ImageMode`, `NumImages`, `NumImagesCounter_RBV` | `Single` stops after one frame, `Multiple` after `NumImages`, `Continuous` free-runs (the default) |
| `cam1:SimMode` | `Peaks` (drifting gaussian spots inside a breathing ring), `LinearRamp`, `Sine`, `Offset&Noise` |
| `cam1:ColorMode` | `Mono`, `RGB1`, `RGB2`, `RGB3` — all four layouts are genuinely produced, not just advertised |
| `cam1:MinX/MinY/SizeX/SizeY` | ROI crop; see the caveat below |
| `cam1:GainRed/Green/Blue`, `Gain`, `Noise` | Per-channel gain (defaulted to 1.0/0.7/0.4 so RGB modes look tinted) and noise amplitude |
| `cam1:ArrayCounter_RBV`, `ArrayRate_RBV`, `DetectorState_RBV` | Frame count, rolling frame rate, detector state |

Defaults: 256×256 Mono at ~5 Hz, free-running — deliberately modest, see the
platform note below. `MaxSizeX/Y_RBV` is 512, so you can raise `SizeX`/`SizeY`
and lower `AcquirePeriod` at runtime.

Two deliberate departures from real areaDetector:

- **`DataType` is read-only at `UInt8`.** The `ArrayData` channel is an 8-bit CA
  waveform, so the data type is not something the IOC can change at runtime.
  Advertising `UInt16` would just produce garbage frames.
- **`SizeX`/`SizeY` are the ROI's far edge, not its extent.** The camera socket
  computes the frame width as `SizeX - MinX`, so the IOC uses the same
  convention. Otherwise a non-zero `MinX` would desync the array length from
  the decoded dimensions and every frame would be dropped.

Run it standalone with:

```bash
uv run python caproto/sim_detector_ioc.py --list-pvs
```

## The simulated TIFF-writer detector

`caproto/sim_tiff_detector_ioc.py` mimics the one moving part of an
areaDetector TIFF file plugin that ophyd-websocket's `/tiff-socket` consumes: a
`TIFF1:FullFileName_RBV` PV naming the most recently saved file on disk.

| PVs | Behavior |
| --- | --- |
| `SIMTIFF1:TIFF1:FullFileName_RBV` | Absolute path of the "current" TIFF; a CHAR waveform (like real AD) so it can exceed the 40-char EPICS string limit |
| `SIMTIFF1:TIFF1:FileNumber_RBV` | Index (0–2) of the current file |
| `SIMTIFF1:TIFF1:NumCaptured_RBV` | Running count of files "written" |

It cycles `FullFileName_RBV` through a fixed set of sample TIFFs in
`caproto/assets/det_1m_*.tiff` every 2 s. When the PV changes, `/tiff-socket`
loads the file, log-normalizes and JPEG-encodes it, and pushes it to finch's
`TIFFCanvas` in the browser.

**The path in the PV must be readable by the *ophyd-websocket* process**, not
the IOC. Run locally that is the same filesystem, so the default resolves to the
repo's `caproto/assets`. In docker both containers mount the assets at `/assets`
(via `SIM_TIFF_ASSETS_DIR`) so the path string matches on both sides.

Run it standalone with:

```bash
uv run python caproto/sim_tiff_detector_ioc.py --list-pvs
```

## The ophyd devices

`startup/sim_devices.py` is mounted at `/startup` and loaded via
`--startup-dir`. It defines both composite devices (`detector`, `temperature`,
`beamline`, `sample`, `spectrum`), three `EpicsMotor`s (`m1`–`m3`), and a
handful of flat signals (`ring_current`, `photon_energy`, `shutter`,
`detector_counts`, …) — 17 registry entries in total. The PV prefix comes from
`SIM_PV_PREFIX` (default `SIM:`), so the same file works against a locally run
IOC.

`startup/sim_detector_devices.py` adds the detector's control and status
signals (`sim_camera`, `camera_acquire`, `camera_frame_rate`, …). It
deliberately omits `image1:ArrayData` — a 256 kB waveform per frame belongs on
the dedicated camera stream, not on the per-signal `/device-socket` fan-out.

Because the directory is bind-mounted, editing it and calling
`POST /api/v1/load-devices` reloads the registry without a restart.

## The frontend

`src/frontend/` is a Vite + React + Tailwind v4 single-page app. The published
finch hooks and canvases (0.1.x) can't send an API key yet, so the app uses
local, finch-style stand-ins in `src/lib/hooks/`. They take the same arguments
plus an `apiKey` option. Swap them back for finch's once finch supports auth.

- `useOphydDeviceSocket` — registry devices over `/api/v1/device-socket`. The
  sidebar lists whatever `GET /api/v1/devices` returns; click to subscribe.
- `useOphydPVSocket` — arbitrary PV names over `/api/v1/pv-socket`. Type any PV
  into the sidebar form.
- `useImageSocket` (rendered by `components/ImageStreamCanvas.tsx`) — the
  camera and TIFF JPEG streams, replacing finch's `CameraCanvas` and
  `TIFFCanvas`.

With a key, each hook sends `{"action": "auth", "api_key": ...}` first and
subscribes or sends its init message only after the server accepts it. Without
a key it behaves exactly like finch's hooks.

**API key.** The sidebar's "API key" panel saves a key to `localStorage`
(`src/lib/apiKey.ts`). Every hook reads it through `useApiKey()`, so saving or
clearing a key reconnects every socket at once. The panel also shows what the
key may do, from `GET /api/v1/auth/permissions`, and set controls are disabled
unless that reports `write`. The "Demo operator" and "Demo observer" buttons
appear only when `VITE_DEMO_*_API_KEY` are set, which `docker-compose-auth.yml`
does. With auth off on the server, the panel says so and everything is
writable.

The camera section on top streams `/api/v1/camera-socket` for the `SIMDET1`
prefix. It sends the same init message finch's `CameraCanvas` does, and the
server negotiates the frame geometry from `SIMDET1:cam1:{MinX,MinY,SizeX,SizeY,
ColorMode,DataType}`. Alongside it, plain PV cards drive `SimMode`,
`ColorMode`, `Acquire` and `AcquirePeriod`; changing any of them updates the
live image.

Below the camera, the TIFF section streams `/api/v1/tiff-socket`, sending
`{ prefix: 'SIMTIFF1' }`. The server watches `SIMTIFF1:TIFF1:FullFileName_RBV`,
so the viewer updates as the TIFF IOC cycles files. Read-only file-writer
status PVs sit alongside it.

Both render through one `SignalCard`, which shows the live value, units,
control limits, connection state, and a setpoint control (a text input, or
buttons when the signal has `enum_strs`).

The socket URLs are derived from `ophydApiUrl` (`src/lib/config.ts`), which
reads `VITE_OPHYD_API_URL` and otherwise falls back to
`http://<current-host>:8001/api/v1`. Note this URL is resolved in the *browser*,
so it points at the host-published port rather than a docker service name.

Dev server runs with HMR against a read-only bind mount of `src/frontend/src`,
so edits show up live.

## Channel Access across containers

CA name resolution normally relies on UDP broadcast, which does not work across
docker's default bridge network. The `ophyd-websocket` service therefore sends
directed searches instead:

```yaml
EPICS_CA_ADDR_LIST: "caproto-ioc caproto-detector"
EPICS_CA_AUTO_ADDR_LIST: "NO"
```

and both IOCs bind `--interfaces 0.0.0.0`. The list is space separated, so add
or swap hosts to talk to a real IOC instead.

## Why ophyd-websocket is pinned to linux/amd64

`Dockerfile` pins `--platform=linux/amd64` and that pin is required. pyepics
bundles `libca` only for x86_64 (`clibs/linux64`) and 32-bit ARM
(`clibs/linuxarm`); there is no aarch64 build, and Debian does not package
EPICS base. A native arm64 image dies on import with:

```
epics.ca.ChannelAccessException: loading Epics CA DLL failed:
  .../epics/clibs/linux64/libca.so: cannot open shared object file
```

The cost on Apple silicon is that this one container runs under emulation,
which matters because it is the container that JPEG-encodes every camera frame.
That is why the simulated detector defaults to 256×256 at 5 Hz (~0.3 MB/s)
rather than 512×512 at 10 Hz (~2 MB/s). Turn it up at runtime via `SizeX` /
`SizeY` / `AcquirePeriod` when you want to stress the pipeline. Going native
would mean building EPICS base from source for aarch64 and pointing
`PYEPICS_LIBCA` at the result.

**The pin needs BuildKit (`docker buildx`).** Without the buildx plugin,
compose falls back to the legacy builder, which ignores `FROM --platform` when
the base image is already cached locally as arm64 (it is, if any other project
pulled `ghcr.io/astral-sh/uv:python3.12-bookworm-slim` natively). The build
then succeeds but produces an arm64 image that crashes with the `libca.so`
error above. With Docker 29's default containerd image store,
`docker build --platform linux/amd64` fails outright on the legacy builder
too. colima installs neither buildx nor the `docker compose` plugin, so check:

```bash
docker buildx version          # must work
docker image inspect ophyd-websocket-ophyd-websocket --format '{{.Architecture}}'   # must be amd64
```

If buildx is missing, `brew install docker-buildx` and link it as Homebrew
instructs, or drop the release binary for your platform from
<https://github.com/docker/buildx/releases> into `~/.docker/cli-plugins/docker-buildx`
(`chmod +x`). Then rebuild with `--build` and check the architecture again.

## Two frontend gotchas worth knowing

- **The SPA does not use `<React.StrictMode>`.** finch's camera hook opens its
  websocket behind a one-shot "already initialised" ref, so StrictMode's
  simulated unmount closes the socket and the remount refuses to reopen it,
  leaving the canvas permanently disconnected in dev.
- **Tailwind has to scan finch's dist.** finch ships unbundled Tailwind classes,
  so `src/index.css` carries
  `@source '../node_modules/@blueskyproject/finch/dist'`. Without it finch's
  components render completely unstyled.

## If `localhost:5173` hangs (colima on macOS)

Symptom: the browser spins forever on `localhost:5173` or `localhost:8001`,
`docker compose ps` shows everything `Up`, and the containers answer fine from
inside. Compose is not at fault — the failure is in colima's host→VM port
forwarder.

**Cause: colima's experimental gRPC port forwarder deadlocks under sustained
websocket traffic.** colima's own config documents the choice:

```yaml
# Port forwarder for the virtual machine (ssh, grpc, none).
# ssh is more stable but supports only TCP.
# grpc supports both TCP and UDP, but is experimental.
# Default: ssh
portForwarder: grpc
```

With `grpc`, the camera stream reliably wedges it within a couple of minutes.
The tell is in `~/.colima/_lima/colima/ha.stderr.log`: the hostagent simply
**stops logging** — even its 10-second `Time sync` heartbeat — while the process
stays alive holding the host listeners. Connections still complete a TCP
handshake all the way into the container (so you get a hang, not a refusal),
but no payload bytes are ever relayed.

**Fix — switch to the stable SSH forwarder:**

```bash
colima stop && colima start --port-forwarder ssh
```

That persists to `~/.colima/default/colima.yaml`, so it survives reboots. Soak
tested here at 3 concurrent camera streams (848 KB/s) plus 5,394 HTTP requests
over 210 s: zero failures, hostagent heartbeat still ticking.

**The one tradeoff: SSH forwarding is TCP-only, so the published UDP ports
(5064/5065, EPICS Channel Access name search) stop working from the host.**
Nothing inside the compose network is affected — that is how ophyd-websocket
reaches the IOCs. It only matters if you run `caget`/`caput`/ophyd from macOS
itself, and you can still do that by aiming Channel Access at the VM directly
instead of at localhost:

```bash
colima status                                    # e.g. address: 192.168.64.2
EPICS_CA_ADDR_LIST=192.168.64.2 EPICS_CA_AUTO_ADDR_LIST=NO caget SIM:Uptime
```

(`caproto-detector` stays unreachable from the host either way — it does not
publish its ports at all, by design.)

Two smaller notes:

- The SPA derives its API URL from whatever host you loaded the page from, so
  `http://192.168.64.2:5173` works as a forwarder-free fallback with no
  reconfiguration.
- colima ships **2 CPUs / 2 GiB** by default, which is tight for four
  containers plus an emulated one. `colima stop && colima start --cpu 4
  --memory 8` if you want more headroom.