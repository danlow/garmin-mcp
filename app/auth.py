import base64
import binascii
import hashlib
import hmac
import json
import time
from collections import defaultdict, deque

OPEN_PATHS = ("/health",)
DASHBOARD_PREFIX = "/dashboard"
LOGIN_PATH = "/dashboard/login"
LOGOUT_PATH = "/dashboard/logout"
SESSION_COOKIE = "garmin_session"
SESSION_DAYS = 30


class Sessions:
    """Dashboard sign-in: username/password check, signed session cookies, and a failed-attempt limit."""

    MAX_FAILURES = 5  # per client address, per FAILURE_WINDOW_S
    FAILURE_WINDOW_S = 60

    def __init__(self, username: str, password: str, secret: str):
        self.username = username.encode()
        self.password = password.encode()
        # Changing the password (or MCP_TOKEN) invalidates every existing session.
        self._key = hashlib.sha256(b"garmin-dashboard-session\0" + secret.encode() + b"\0" + self.password).digest()
        self._failures: dict[str, deque] = defaultdict(deque)

    def check(self, username: str, password: str) -> bool:
        if not self.password:
            return False
        user_ok = hmac.compare_digest(username.encode(), self.username)
        pass_ok = hmac.compare_digest(password.encode(), self.password)
        return user_ok and pass_ok

    def locked_out(self, client: str) -> bool:
        attempts = self._failures[client]
        while attempts and attempts[0] < time.time() - self.FAILURE_WINDOW_S:
            attempts.popleft()
        return len(attempts) >= self.MAX_FAILURES

    def record_failure(self, client: str) -> None:
        self._failures[client].append(time.time())

    def _sign(self, expires: str) -> str:
        return hmac.new(self._key, expires.encode(), hashlib.sha256).hexdigest()

    def issue(self) -> str:
        expires = str(int(time.time()) + SESSION_DAYS * 86400)
        return f"{expires}.{self._sign(expires)}"

    def valid(self, value: str) -> bool:
        expires, _, sig = value.partition(".")
        return (expires.isdigit() and int(expires) > time.time()
                and hmac.compare_digest(sig, self._sign(expires)))

    @staticmethod
    def set_cookie(value: str, max_age: int) -> str:
        # No Secure flag: the dashboard is served over plain HTTP on the LAN / Tailscale.
        return f"{SESSION_COOKIE}={value}; Path=/dashboard; HttpOnly; SameSite=Strict; Max-Age={max_age}"


def _cookie(headers: dict, name: str) -> str:
    for part in headers.get(b"cookie", b"").decode("latin-1").split(";"):
        k, _, v = part.strip().partition("=")
        if k == name:
            return v
    return ""


class Auth:
    """ASGI middleware.

    /health                      open (for Uptime Kuma)
    /dashboard/login, /logout    open (the sign-in form)
    /dashboard...                session cookie from the sign-in form; HTTP Basic also accepted for scripts
    everything else              `Authorization: Bearer <MCP_TOKEN>`, for Claude
    """

    def __init__(self, app, token: str, sessions: Sessions):
        self.app = app
        self.bearer = f"Bearer {token}".encode()
        self.sessions = sessions

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            path = scope["path"]
            headers = dict(scope.get("headers") or [])
            given = headers.get(b"authorization", b"")
            if path.startswith(DASHBOARD_PREFIX):
                if path not in (LOGIN_PATH, LOGOUT_PATH) and not (
                        self.sessions.valid(_cookie(headers, SESSION_COOKIE)) or self._basic_ok(given)):
                    if path.rstrip("/") == DASHBOARD_PREFIX:
                        return await _redirect(send, LOGIN_PATH)
                    return await _deny(send, None)  # no challenge header, so browsers don't pop up a prompt
            elif path not in OPEN_PATHS and not hmac.compare_digest(given, self.bearer):
                return await _deny(send, b"Bearer")
        await self.app(scope, receive, send)

    def _basic_ok(self, header: bytes) -> bool:
        if not header.startswith(b"Basic "):
            return False
        try:
            user, _, password = base64.b64decode(header[6:], validate=True).decode().partition(":")
        except (binascii.Error, ValueError):
            return False
        return self.sessions.check(user, password)


async def _redirect(send, location: str) -> None:
    await send({"type": "http.response.start", "status": 303,
                "headers": [(b"location", location.encode()), (b"content-length", b"0")]})
    await send({"type": "http.response.body", "body": b""})


async def _deny(send, challenge: bytes | None) -> None:
    body = json.dumps({"error": "unauthorized"}).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
    if challenge:
        headers.append((b"www-authenticate", challenge))
    await send({"type": "http.response.start", "status": 401, "headers": headers})
    await send({"type": "http.response.body", "body": body})
