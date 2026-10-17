"""API-key authentication that piggybacks on the Bluesky Queue Server HTTP server.

ophyd-websocket keeps no users or keys of its own. A client presents the same
API key it uses for the Queue Server HTTP server, and we ask that server what
the key is allowed to do (``GET /api/auth/scopes``). The answer is the key's
*effective* scopes: the key's own scopes intersected with its owner's role
scopes, so a restricted operator key correctly reports fewer scopes.

We then enforce our own mapping of scopes to operations:

=====================================  =========================
Operation                              Required scope (default)
=====================================  =========================
read / subscribe / stream / REST GET   ``read:monitor``
set PV or device, load devices         ``write:queue:control``
Queue Server console socket            ``read:console``
=====================================  =========================

Auth is opt-in (``OAS_AUTH_ENABLED=true``). When enabled it fails closed: if
the Queue Server can't be reached or answers unexpectedly, the request is
refused (HTTP 503 / WebSocket close 1013), never allowed.

REST callers send ``Authorization: ApiKey <key>``. Browsers can't set headers
on a WebSocket, so socket clients send ``{"action": "auth", "api_key": "..."}``
as their first message instead. Keys are never accepted in a URL, never
logged and never echoed back; logs carry a short sha256 fingerprint instead.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Literal

import httpx
from fastapi import HTTPException, Request, WebSocket

logger = logging.getLogger(__name__)

Need = Literal["read", "write", "console"]

AUTH_ACTION = "auth"
# A key is ~80 hex chars; anything much bigger isn't an auth message.
MAX_AUTH_MESSAGE_BYTES = 4096

# WebSocket close codes (RFC 6455).
WS_POLICY_VIOLATION = 1008
WS_TRY_AGAIN_LATER = 1013


# --------------------------------------------------------------------------
# Configuration (read on every call so tests and operators can change it)
# --------------------------------------------------------------------------

def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("true", "1", "yes", "on")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        logger.warning("Ignoring non-numeric %s=%r; using %s", name, os.getenv(name), default)
        return default


def auth_enabled() -> bool:
    return _env_bool("OAS_AUTH_ENABLED", False)


def qserver_url() -> str:
    explicit = os.getenv("OAS_AUTH_QSERVER_URL")
    if explicit:
        return explicit.rstrip("/")
    host = os.getenv("QSERVER_HTTP_SERVER_HOST", "localhost")
    port = os.getenv("QSERVER_HTTP_SERVER_PORT", "60610")
    return f"http://{host}:{port}"


DEFAULT_SCOPES: dict[str, str] = {
    "read": "read:monitor",
    "write": "write:queue:control",
    "console": "read:console",
}


def required_scope(need: Need) -> str:
    return os.getenv(f"OAS_AUTH_{need.upper()}_SCOPE", DEFAULT_SCOPES[need])


def request_timeout() -> float:
    return _env_float("OAS_AUTH_TIMEOUT", 3.0)


def handshake_timeout() -> float:
    return _env_float("OAS_AUTH_WS_HANDSHAKE_TIMEOUT", 5.0)


def revalidate_seconds() -> float:
    return _env_float("OAS_AUTH_REVALIDATE_SECONDS", 30.0)


def read_cache_seconds() -> float:
    return _env_float("OAS_AUTH_READ_CACHE_SECONDS", 10.0)


def parse_allowed_origins() -> list[str]:
    """``OAS_ALLOWED_ORIGINS`` as a list: comma separated or a JSON array."""
    raw = os.getenv("OAS_ALLOWED_ORIGINS", "")
    if not raw:
        return []
    if raw.startswith("[") and raw.endswith("]"):
        try:
            return [str(o).strip() for o in json.loads(raw) if str(o).strip()]
        except json.JSONDecodeError:
            logger.warning("Invalid JSON format in OAS_ALLOWED_ORIGINS: %s", raw)
            return []
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


def describe_config() -> list[str]:
    """Human-readable auth settings for the startup banner (no secrets)."""
    if not auth_enabled():
        return ["OAS_AUTH_ENABLED: false (NO AUTHENTICATION - anyone who can reach this server can read and write)"]
    return [
        "OAS_AUTH_ENABLED: true",
        f"OAS_AUTH_QSERVER_URL: {qserver_url()}",
        f"Read scope: {required_scope('read')}",
        f"Write scope: {required_scope('write')}",
        f"Console scope: {required_scope('console')}",
        f"WebSocket revalidation: every {revalidate_seconds():g}s; read cache {read_cache_seconds():g}s",
        f"WebSocket Origin allow-list: {parse_allowed_origins() or 'not set (Origin not checked)'}",
    ]


# --------------------------------------------------------------------------
# Errors and principals
# --------------------------------------------------------------------------

class AuthError(Exception):
    """Base class. ``message`` is safe to show to the client."""
    message = "authentication failed"
    code = "unauthorized"
    http_status = 401
    ws_close_code = WS_POLICY_VIOLATION

    def __init__(self, message: str | None = None):
        super().__init__(message or self.message)
        if message:
            self.message = message


class InvalidCredentials(AuthError):
    message = "invalid or missing API key"
    code = "unauthorized"
    http_status = 401


class InsufficientScope(AuthError):
    message = "insufficient permissions"
    code = "forbidden"
    http_status = 403


class AuthUnavailable(AuthError):
    message = "auth service unavailable"
    code = "auth_unavailable"
    http_status = 503
    ws_close_code = WS_TRY_AGAIN_LATER


def fingerprint(api_key: str | None) -> str:
    """Short, non-reversible label for a key, safe to log."""
    if not api_key:
        return "none"
    return hashlib.sha256(api_key.encode()).hexdigest()[:8]


@dataclass(frozen=True)
class Principal:
    scopes: frozenset[str] = frozenset()
    roles: frozenset[str] = frozenset()
    key_fingerprint: str = "none"
    auth_enabled: bool = True

    def allows(self, need: Need) -> bool:
        if not self.auth_enabled:
            return True
        return required_scope(need) in self.scopes

    def permissions(self) -> dict[str, bool]:
        return {need: self.allows(need) for need in ("read", "write", "console")}


ANONYMOUS = Principal(auth_enabled=False)


# --------------------------------------------------------------------------
# Talking to the Queue Server
# --------------------------------------------------------------------------

async def fetch_principal(api_key: str | None) -> Principal:
    """Ask the Queue Server HTTP server for ``api_key``'s effective scopes."""
    if not api_key or not isinstance(api_key, str) or not api_key.strip():
        # Never call upstream without a key: an anonymous request gets
        # 200 {"scopes": []}, which must not look like a valid login.
        raise InvalidCredentials()

    url = f"{qserver_url()}/api/auth/scopes"
    headers = {"Authorization": f"ApiKey {api_key.strip()}", "Accept": "application/json"}
    try:
        # One short-lived client per check (like queue_safety): a shared
        # client binds to the event loop that created it.
        async with httpx.AsyncClient(timeout=request_timeout()) as client:
            response = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        logger.warning("Auth service unreachable at %s: %s", url, type(exc).__name__)
        raise AuthUnavailable() from exc

    if response.status_code in (401, 403):
        raise InvalidCredentials()
    if response.status_code != 200:
        logger.warning("Auth service returned HTTP %s for key %s", response.status_code, fingerprint(api_key))
        raise AuthUnavailable()

    try:
        body = response.json()
        scopes = body["scopes"]
        roles = body.get("roles", [])
        if not isinstance(scopes, list) or not all(isinstance(s, str) for s in scopes):
            raise ValueError("scopes is not a list of strings")
        if not isinstance(roles, list):
            roles = []
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        logger.warning("Auth service returned an unexpected body: %s", exc)
        raise AuthUnavailable() from exc

    return Principal(
        scopes=frozenset(scopes),
        roles=frozenset(str(r) for r in roles),
        key_fingerprint=fingerprint(api_key),
    )


# sha256(key) -> (principal, fetched_at). Only successful lookups are cached,
# and never the raw key.
_principal_cache: dict[str, tuple[Principal, float]] = {}


def clear_cache():
    _principal_cache.clear()


async def authorize(api_key: str | None, need: Need, fresh: bool | None = None) -> Principal:
    """Validate ``api_key`` and require the scope for ``need``.

    Writes always go to the Queue Server (``fresh``); reads may reuse a
    lookup up to ``OAS_AUTH_READ_CACHE_SECONDS`` old.
    """
    if not auth_enabled():
        return ANONYMOUS
    if fresh is None:
        fresh = need == "write"

    cache_key = hashlib.sha256(api_key.encode()).hexdigest() if api_key else None
    principal = None
    if cache_key and not fresh:
        cached = _principal_cache.get(cache_key)
        if cached and time.monotonic() - cached[1] < read_cache_seconds():
            principal = cached[0]

    if principal is None:
        try:
            principal = await fetch_principal(api_key)
        except InvalidCredentials:
            if cache_key:
                _principal_cache.pop(cache_key, None)
            raise
        _principal_cache[cache_key] = (principal, time.monotonic())

    if not principal.allows(need):
        logger.info("Key %s lacks scope %s for %s", principal.key_fingerprint, required_scope(need), need)
        raise InsufficientScope()
    return principal


# --------------------------------------------------------------------------
# REST
# --------------------------------------------------------------------------

def api_key_from_request(request: Request) -> str | None:
    """The key from ``Authorization: ApiKey|Bearer <key>`` (never the URL)."""
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() in ("apikey", "bearer") and value.strip():
        return value.strip()
    return None


async def _principal_for_request(request: Request, need: Need) -> Principal:
    try:
        return await authorize(api_key_from_request(request), need)
    except AuthError as exc:
        headers = {"WWW-Authenticate": "ApiKey"} if exc.http_status == 401 else None
        raise HTTPException(status_code=exc.http_status, detail=exc.message, headers=headers) from None


async def require_read(request: Request) -> Principal:
    return await _principal_for_request(request, "read")


async def require_write(request: Request) -> Principal:
    return await _principal_for_request(request, "write")


async def require_console(request: Request) -> Principal:
    return await _principal_for_request(request, "console")


# --------------------------------------------------------------------------
# WebSocket
# --------------------------------------------------------------------------

def is_auth_message(data) -> bool:
    return isinstance(data, dict) and data.get("action") == AUTH_ACTION


def error_payload(exc: AuthError) -> dict:
    return {"error": exc.message, "code": exc.code}


@dataclass
class WsAuthSession:
    """Auth state for one WebSocket connection.

    The key stays in memory for the life of the socket so writes can be
    re-validated and long-lived streams revalidated periodically.
    """
    need: Need
    principal: Principal = ANONYMOUS
    api_key: str | None = field(default=None, repr=False)

    @property
    def enabled(self) -> bool:
        return self.principal.auth_enabled

    def ack(self) -> dict:
        return {"action": AUTH_ACTION, "ok": True, "permissions": self.principal.permissions()}

    async def require_write(self) -> Principal:
        """Fresh check before every write; raises an :class:`AuthError`."""
        if not self.enabled:
            return self.principal
        self.principal = await authorize(self.api_key, "write", fresh=True)
        return self.principal

    async def handle_auth_message(self, websocket: WebSocket, data: dict):
        """An ``auth`` message after the handshake: re-auth (e.g. new key) or just ack."""
        if self.enabled:
            key = data.get("api_key")
            if isinstance(key, str) and key and key != self.api_key:
                try:
                    principal = await authorize(key, self.need, fresh=True)
                except AuthError as exc:
                    await websocket.send_json({"action": AUTH_ACTION, "ok": False, **error_payload(exc)})
                    return
                self.api_key, self.principal = key, principal
        await websocket.send_json(self.ack())

    async def _revalidate_forever(self, websocket: WebSocket):
        interval = revalidate_seconds()
        while True:
            await asyncio.sleep(interval)
            try:
                self.principal = await authorize(self.api_key, self.need, fresh=True)
            except AuthError as exc:
                logger.info("Closing socket for key %s: %s", fingerprint(self.api_key), exc.message)
                with contextlib.suppress(Exception):
                    await websocket.send_json({"action": AUTH_ACTION, "ok": False, **error_payload(exc)})
                with contextlib.suppress(Exception):
                    await websocket.close(code=exc.ws_close_code, reason=exc.message)
                return

    @contextlib.asynccontextmanager
    async def guard(self, websocket: WebSocket):
        """Revalidate in the background while the handler runs; close the
        socket if the key is revoked, loses its scope or can't be checked."""
        if not self.enabled:
            yield
            return
        task = asyncio.create_task(self._revalidate_forever(websocket))
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


async def _reject(websocket: WebSocket, exc: AuthError):
    with contextlib.suppress(Exception):
        await websocket.send_json({"action": AUTH_ACTION, "ok": False, **error_payload(exc)})
    with contextlib.suppress(Exception):
        await websocket.close(code=exc.ws_close_code, reason=exc.message)


async def authenticate_websocket(websocket: WebSocket, need: Need = "read") -> WsAuthSession | None:
    """Run the first-message auth handshake on an accepted socket.

    Returns the session, or ``None`` after replying with the reason and
    closing the socket. With auth disabled this reads nothing and returns a
    full-permission session at once.
    """
    if not auth_enabled():
        return WsAuthSession(need=need)

    allowed = parse_allowed_origins()
    origin = websocket.headers.get("origin")
    # Non-browser clients send no Origin; browsers always do.
    if allowed and "*" not in allowed and origin is not None and origin not in allowed:
        logger.warning("Rejected WebSocket from disallowed origin %s", origin)
        await _reject(websocket, InvalidCredentials("origin not allowed"))
        return None

    try:
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=handshake_timeout())
    except asyncio.TimeoutError:
        await _reject(websocket, InvalidCredentials("auth message not received in time"))
        return None
    except Exception:
        # Disconnected, or a binary frame instead of text.
        await _reject(websocket, InvalidCredentials("expected an auth message"))
        return None

    if len(raw.encode()) > MAX_AUTH_MESSAGE_BYTES:
        await _reject(websocket, InvalidCredentials("auth message too large"))
        return None

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = None
    if not is_auth_message(data) or not isinstance(data.get("api_key"), str):
        await _reject(websocket, InvalidCredentials(
            'first message must be {"action": "auth", "api_key": "<Queue Server API key>"}'
        ))
        return None

    api_key = data["api_key"]
    try:
        principal = await authorize(api_key, need, fresh=True)
    except AuthError as exc:
        logger.info("WebSocket auth failed for key %s: %s", fingerprint(api_key), exc.message)
        await _reject(websocket, exc)
        return None

    session = WsAuthSession(need=need, principal=principal, api_key=api_key)
    logger.info("WebSocket authenticated: key %s roles=%s permissions=%s",
                principal.key_fingerprint, sorted(principal.roles), principal.permissions())
    await websocket.send_json(session.ack())
    return session


async def ws_queue_safety_check():
    """Same interlock the REST writes use. Separate name so tests can stub it."""
    from . import queue_safety

    await queue_safety.check_queue_server_safety()


async def authorize_ws_write(websocket: WebSocket, session: WsAuthSession) -> bool:
    """Gate one WebSocket write: fresh permission check, then queue safety.

    Sends ``{"error", "code"}`` and returns False if the write must not run.
    """
    try:
        await session.require_write()
    except AuthError as exc:
        logger.info("Write denied for key %s: %s", fingerprint(session.api_key), exc.message)
        await websocket.send_json(error_payload(exc))
        return False

    try:
        await ws_queue_safety_check()
    except HTTPException as exc:
        await websocket.send_json({"error": f"Queue server safety check failed: {exc.detail}", "code": "queue_busy"})
        return False
    return True


async def receive_non_auth_text(websocket: WebSocket, session: WsAuthSession | None = None) -> str:
    """Next text message that isn't an ``auth`` message (those get acked).

    Lets a client always send auth first, whether or not auth is enabled,
    before the init message a camera/file socket expects.
    """
    session = session or WsAuthSession(need="read")
    while True:
        text = await websocket.receive_text()
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return text
        if not is_auth_message(data):
            return text
        await session.handle_auth_message(websocket, data)
