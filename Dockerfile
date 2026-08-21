FROM --platform=linux/amd64 ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /code

# README/LICENSE are referenced by pyproject.toml metadata, and the package
# itself must exist for uv to build the project into the environment.
COPY pyproject.toml uv.lock* README.md LICENSE ./
COPY src/ophyd_websocket ./src/ophyd_websocket

RUN uv sync --frozen --no-dev

COPY . /code

CMD ["uv", "run", "python", "src/ophyd_websocket/server.py"]
