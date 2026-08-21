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

Running tests:
```bash
uv run pytest
```

# Local test stack (docker compose)

A batteries-included stack for developing against ophyd-websocket without a
real beamline. Three services:

| Service | What it is | Where |
| --- | --- | --- |
| `caproto-ioc` | Simulated EPICS IOC serving `SIM:*` PVs | `caproto/sim_ioc.py` |
| `caproto-detector` | Simulated areaDetector serving `SIMDET1:*` PVs | `caproto/sim_detector_ioc.py` |
| `caproto-tiff-detector` | Simulated TIFF-writer serving `SIMTIFF1:*` PVs | `caproto/sim_tiff_detector_ioc.py` |
| `ophyd-websocket` | This server, with ophyd devices wrapping those PVs | `startup/` |
| `frontend` | Minimal React/Tailwind SPA driven by the finch hooks | `src/frontend/` |

```bash
docker compose up --build
open http://localhost:5173
```

Ports: frontend `5173`, ophyd-websocket `8001`, IOC Channel Access `5064`.
`caproto-detector` is reachable only from inside the compose network — Channel
Access is port-sensitive and `caproto-ioc` already owns 5064 on the host.


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

`src/frontend/` is a Vite + React + Tailwind v4 single-page app. It uses two
finch hooks:

- `useOphydDeviceSocket` — registry devices over `/api/v1/device-socket`. The
  sidebar lists whatever `GET /api/v1/devices` returns; click to subscribe.
- `useOphydPVSocket` — arbitrary PV names over `/api/v1/pv-socket`. Type any PV
  into the sidebar form.

The camera section on top renders finch's `CameraCanvas` pointed at the
`SIMDET1` prefix. The canvas opens its own `/api/v1/camera-socket` connection
and negotiates the frame geometry from `SIMDET1:cam1:{MinX,MinY,SizeX,SizeY,
ColorMode,DataType}`, so the only prop it needs is the prefix. Alongside it,
plain PV cards drive `SimMode`, `ColorMode`, `Acquire` and `AcquirePeriod` —
changing any of them updates the live image.

Below the camera, the TIFF section renders finch's `TIFFCanvas` pointed at the
`SIMTIFF1` prefix. The canvas opens its own `/api/v1/tiff-socket` connection,
sends `{ prefix }`, and the server watches `SIMTIFF1:TIFF1:FullFileName_RBV` —
so as the TIFF IOC cycles files, the viewer updates. Read-only file-writer
status PVs sit alongside it.

Both render through one `SignalCard`, which shows the live value, units,
control limits, connection state, and a setpoint control (a text input, or
buttons when the signal has `enum_strs`).

The socket URLs are derived from `ophydApiUrl` on finch's `FinchConfigProvider`,
which reads `VITE_OPHYD_API_URL` and otherwise falls back to
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