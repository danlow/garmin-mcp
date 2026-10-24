import base64
import binascii
import hmac
import json

OPEN_PATHS = ("/health",)
DASHBOARD_PREFIX = "/dashboard"


class Auth:
    """ASGI middleware.

    /health         open (for Uptime Kuma)
    /dashboard...   HTTP Basic auth with DASHBOARD_PASSWORD (any username), so a browser can log in
    everything else `Authorization: Bearer <MCP_TOKEN>`, for Claude
    """

    def __init__(self, app, token: str, dashboard_password: str):
        self.app = app
        self.bearer = f"Bearer {token}".encode()
        self.dashboard_password = dashboard_password.encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            path = scope["path"]
            given = dict(scope.get("headers") or []).get(b"authorization", b"")
            if path.startswith(DASHBOARD_PREFIX):
                if not self._basic_ok(given):
                    return await _deny(send, b'Basic realm="Garmin dashboard", charset="UTF-8"')
            elif path not in OPEN_PATHS and not hmac.compare_digest(given, self.bearer):
                return await _deny(send, b"Bearer")
        await self.app(scope, receive, send)

    def _basic_ok(self, header: bytes) -> bool:
        if not self.dashboard_password or not header.startswith(b"Basic "):
            return False
        try:
            _, _, password = base64.b64decode(header[6:], validate=True).partition(b":")
        except (binascii.Error, ValueError):
            return False
        return hmac.compare_digest(password, self.dashboard_password)


async def _deny(send, challenge: bytes) -> None:
    body = json.dumps({"error": "unauthorized"}).encode()
    await send({
        "type": "http.response.start",
        "status": 401,
        "headers": [(b"content-type", b"application/json"),
                    (b"www-authenticate", challenge),
                    (b"content-length", str(len(body)).encode())],
    })
    await send({"type": "http.response.body", "body": body})
