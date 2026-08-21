# Simulated EPICS IOC (caproto).
# Build context is the repository root so we can reuse pyproject.toml/uv.lock;
# caproto lives in the project's dev dependency group.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /code

COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src/ophyd_websocket ./src/ophyd_websocket

RUN uv sync --frozen

COPY caproto /code/caproto

ENV SIM_PV_PREFIX=SIM:

# 5064/udp: CA name search, 5064/tcp: CA circuits, 5065/udp: CA beacons
EXPOSE 5064/udp 5064/tcp 5065/udp

CMD ["sh", "-c", "uv run python -u caproto/sim_ioc.py --prefix \"$SIM_PV_PREFIX\" --list-pvs --interfaces 0.0.0.0"]
