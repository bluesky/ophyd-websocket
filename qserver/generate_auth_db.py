"""Build the demo bluesky_httpserver.sqlite and its API keys.

Runs inside the qserver image (see generate_auth_db.sh), so the database is
created by the exact bluesky-httpserver version that later serves it. It:

1. starts the HTTP server on a fresh, empty database (no RE Manager needed:
   logging in and minting keys never touch the queue),
2. logs in as 'operator' and 'observer' with the demo passwords,
3. mints never-expiring API keys that inherit each user's scopes,
4. checks each key's effective scopes with GET /api/auth/scopes,
5. writes the database and a plaintext env file of the keys to OUT_DIR.

Only the keys' sha256 hashes are stored in the database, so the env file is the
only place the plaintext keys exist. They are DEMO keys, committed on purpose.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

OUT_DIR = os.environ.get("OUT_DIR", "/out")
PORT = 60610
BASE = f"http://127.0.0.1:{PORT}"
DB_NAME = "bluesky_httpserver.sqlite"
KEYS_NAME = "demo-api-keys.env"

WRITE_SCOPE = "write:queue:control"


def request(method: str, path: str, *, token: str | None = None, scheme: str = "Bearer",
            form: dict | None = None, body: dict | None = None) -> tuple[int, dict]:
    headers = {"Accept": "application/json"}
    data = None
    if token:
        headers["Authorization"] = f"{scheme} {token}"
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def wait_for_server(proc: subprocess.Popen, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            sys.exit(f"HTTP server exited early with code {proc.returncode}")
        try:
            request("GET", "/api/auth/scopes")  # any HTTP answer means it's up
            return
        except (urllib.error.URLError, ConnectionError, OSError):
            time.sleep(0.5)
    sys.exit("HTTP server did not start in time")


def mint_key(username: str, password: str, note: str) -> str:
    status, body = request("POST", "/api/auth/provider/local/token",
                           form={"username": username, "password": password})
    if status != 200:
        sys.exit(f"Login as {username!r} failed: HTTP {status} {body}")
    status, body = request("POST", "/api/auth/apikey", token=body["access_token"],
                           body={"scopes": ["inherit"], "expires_in": None, "note": note})
    if status != 200:
        sys.exit(f"Minting a key for {username!r} failed: HTTP {status} {body}")
    return body["secret"]


def scopes_of(key: str) -> set[str]:
    status, body = request("GET", "/api/auth/scopes", token=key, scheme="ApiKey")
    if status != 200:
        sys.exit(f"Key check failed: HTTP {status} {body}")
    return set(body["scopes"])


def main() -> None:
    operator_pw = os.environ["QSERVER_OPERATOR_PASSWORD"]
    observer_pw = os.environ["QSERVER_OBSERVER_PASSWORD"]

    workdir = tempfile.mkdtemp()
    db_path = os.path.join(workdir, DB_NAME)
    env = dict(os.environ,
               QSERVER_HTTP_SERVER_DATABASE_URI=f"sqlite:///{db_path}",
               QSERVER_ZMQ_HOST=os.environ.get("QSERVER_ZMQ_HOST", "localhost"))
    proc = subprocess.Popen(
        ["start-bluesky-httpserver", "--host", "127.0.0.1", "--port", str(PORT)], env=env)
    try:
        wait_for_server(proc)
        keys = {
            "operator": mint_key("operator", operator_pw, "demo operator key"),
            "observer": mint_key("observer", observer_pw, "demo observer key"),
            "service": mint_key("observer", observer_pw,
                                "ophyd-websocket queue-safety status checks"),
        }
        operator_scopes = scopes_of(keys["operator"])
        observer_scopes = scopes_of(keys["observer"])
        if WRITE_SCOPE not in operator_scopes:
            sys.exit(f"operator key lacks {WRITE_SCOPE}: {sorted(operator_scopes)}")
        if WRITE_SCOPE in observer_scopes or "read:monitor" not in observer_scopes:
            sys.exit(f"observer key has unexpected scopes: {sorted(observer_scopes)}")
        if "read:status" not in scopes_of(keys["service"]):
            sys.exit("service key lacks read:status")
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=30)

    shutil.copyfile(db_path, os.path.join(OUT_DIR, DB_NAME))
    with open(os.path.join(OUT_DIR, KEYS_NAME), "w") as f:
        f.write(
            "# DEMO API keys for docker-compose-auth.yml. NOT SECRET: committed on\n"
            "# purpose and only valid against qserver/bluesky_httpserver.sqlite.\n"
            "# Regenerate both with qserver/generate_auth_db.sh; never reuse in production.\n"
            "#\n"
            "# Offered by the frontend as one-click 'use demo key' buttons.\n"
            f"VITE_DEMO_OPERATOR_API_KEY={keys['operator']}\n"
            f"VITE_DEMO_OBSERVER_API_KEY={keys['observer']}\n"
            "# ophyd-websocket's own (observer) key for the queue-safety /api/status check.\n"
            f"QSERVER_HTTP_SERVER_SINGLE_USER_API_KEY={keys['service']}\n"
        )
    print(f"Wrote {DB_NAME} and {KEYS_NAME} to {OUT_DIR}")
    print(f"operator scopes: {sorted(operator_scopes)}")
    print(f"observer scopes: {sorted(observer_scopes)}")


if __name__ == "__main__":
    main()
