"""Local browser bootstrap and same-origin controls; no credentials in URLs."""
from __future__ import annotations

import hashlib
import hmac
import secrets

from fastapi import Request
from fastapi.responses import JSONResponse

from ofp_playground.status.server import is_loopback

COOKIE = "ofp_canvas_session"


class CanvasAccess:
    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self.token = secrets.token_urlsafe(32)

    @staticmethod
    def cookie_name(headers) -> str:
        # Cookies are shared across ports. Distinguish local app instances so
        # opening another Canvas port cannot continuously replace this token.
        host = headers.get("host", "")
        return f"{COOKIE}_{hashlib.sha256(host.encode()).hexdigest()[:16]}"

    def allowed_origin(self, origin: str | None, host: str, scheme: str = "http") -> bool:
        if origin is None:
            return True  # non-browser caller still requires a credential
        return origin in {f"{scheme}://{host}", "http://localhost:5173", "http://127.0.0.1:5173"}

    def authorized(self, headers, cookies) -> bool:
        bearer = headers.get("authorization", "")
        value = bearer[7:] if bearer.startswith("Bearer ") else cookies.get(self.cookie_name(headers), "")
        return isinstance(value, str) and hmac.compare_digest(value, self.token)

    def can_bootstrap(self, request: Request) -> bool:
        return (is_loopback(request.url.hostname or "")
                and request.client is not None and is_loopback(request.client.host)
                and self.allowed_origin(request.headers.get("origin"), request.headers.get("host", ""), request.url.scheme))

    async def middleware(self, request: Request, call_next):
        path = request.url.path
        bootstrap = path == "/auth/bootstrap" or path == "/"
        protected = path.startswith(("/session/", "/status/", "/trace/", "/media/")) or path == "/agents/preview"
        if self.enabled and (protected or path == "/auth/bootstrap"):
            origin_ok = self.allowed_origin(request.headers.get("origin"), request.headers.get("host", ""), request.url.scheme)
            if not origin_ok:
                return JSONResponse({"detail": "Canvas requests must use an authorized origin"}, status_code=403)
            if not (bootstrap and self.can_bootstrap(request)) and not self.authorized(request.headers, request.cookies):
                return JSONResponse({"detail": "Open the local Canvas page to authorize this browser"}, status_code=401)
        response = await call_next(request)
        if self.enabled and bootstrap and self.can_bootstrap(request):
            response.set_cookie(self.cookie_name(request.headers), self.token, httponly=True, samesite="strict", secure=request.url.scheme == "https")
        return response
