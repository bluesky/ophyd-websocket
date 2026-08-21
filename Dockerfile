# The linux/amd64 pin is REQUIRED, do not remove it. pyepics bundles libca
# only for x86_64 (clibs/linux64) and 32-bit ARM (clibs/linuxarm) -- there is
# no aarch64 build, so a native arm64 image dies at import with
# "loading Epics CA DLL failed: .../clibs/linux64/libca.so: cannot open
# shared object file".
#
# The cost is that on Apple silicon this container runs under QEMU emulation,
# which makes the camera path's per-frame JPEG encoding materially slower.
# That is why the simulated detector defaults to a modest 256x256 @ 5 Hz.
# To get a native arm64 image you would have to build EPICS base from source
# and point PYEPICS_LIBCA at the result.
FROM --platform=linux/amd64 ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /code

# README/LICENSE are referenced by pyproject.toml metadata, and the package
# itself must exist for uv to build the project into the environment.
COPY pyproject.toml uv.lock* README.md LICENSE ./
COPY src/ophyd_websocket ./src/ophyd_websocket

RUN uv sync --frozen --no-dev

COPY . /code

CMD ["uv", "run", "python", "src/ophyd_websocket/server.py"]
