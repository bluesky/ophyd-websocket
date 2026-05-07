FROM --platform=linux/amd64 ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /code

COPY pyproject.toml uv.lock* ./

RUN uv sync --frozen --no-dev

COPY . /code

CMD ["uv", "run", "python", "src/ophyd_websocket/server.py"]
