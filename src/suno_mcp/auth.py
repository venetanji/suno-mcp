"""Suno authentication via Clerk - browser login and token management."""

import asyncio
import json
import logging
import os
import time

import httpx
from playwright.async_api import async_playwright

from .config import Settings

logger = logging.getLogger(__name__)

CLERK_BASE = "https://clerk.suno.com"


class AuthenticationError(Exception):
    pass


class SunoAuth:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._cookie: str | None = None
        self._session_id: str | None = None
        self._jwt: str | None = None
        self._jwt_timestamp: float = 0.0
        self._load_session()

    def _load_session(self) -> None:
        path = self.settings.auth_file
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text())
            self._cookie = data.get("cookie")
            self._session_id = data.get("session_id")
            self._jwt = data.get("jwt")
            self._jwt_timestamp = data.get("jwt_timestamp", 0.0)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load session file: %s", exc)

    def _save_session(self) -> None:
        self.settings.auth_dir.mkdir(parents=True, exist_ok=True)
        data = {
            "cookie": self._cookie,
            "session_id": self._session_id,
            "jwt": self._jwt,
            "jwt_timestamp": self._jwt_timestamp,
        }
        self.settings.auth_file.write_text(json.dumps(data, indent=2))

    def is_authenticated(self) -> bool:
        return bool(self._jwt and (self._session_id or self._cookie))

    async def login_with_browser(self) -> None:
        """Open a headed Chromium browser for Google OAuth login via Suno.

        Uses network interception to capture the Clerk JWT token directly
        from the browser's API calls rather than extracting cookies.
        Uses a persistent profile so the session survives container restarts.
        """
        os.environ.setdefault("DISPLAY", self.settings.display)
        profile_dir = str(self.settings.auth_dir / "chrome-profile")

        captured: dict = {}

        async with async_playwright() as pw:
            context = await pw.chromium.launch_persistent_context(
                profile_dir,
                headless=False,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                ],
                viewport={"width": 1280, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
                ),
            )

            # Intercept Clerk token responses to capture JWT directly.
            async def on_response(response):
                if "/v1/client/sessions/" in response.url and "/tokens" in response.url:
                    try:
                        data = await response.json()
                        jwt = data.get("jwt") or data.get("token")
                        if jwt:
                            captured["jwt"] = jwt
                            logger.info("Captured Clerk JWT from network response")
                            # Also grab session_id from URL
                            parts = response.url.split("/")
                            if "sessions" in parts:
                                idx = parts.index("sessions")
                                if idx + 1 < len(parts):
                                    captured["session_id"] = parts[idx + 1]
                    except Exception as e:
                        logger.warning("Failed to parse Clerk token response: %s", e)

            context.on("response", on_response)

            page = await context.new_page()
            await page.goto("https://suno.com/signin")

            logger.info(
                "Browser opened at suno.com/signin. "
                "Complete Google login via noVNC at http://<host>:6080"
            )

            # Wait up to 5 minutes for the JWT to be captured via interception.
            deadline = time.monotonic() + 300
            while time.monotonic() < deadline:
                await asyncio.sleep(2)
                if captured.get("jwt"):
                    break
                # Also grab cookies as fallback for is_authenticated check
                cookies = await context.cookies("https://suno.com")
                cookie_map = {c["name"]: c["value"] for c in cookies}
                uat = cookie_map.get("__client_uat", "0")
                logger.info("Waiting for Clerk JWT capture — uat=%s jwt=%s", uat, bool(captured.get("jwt")))
            else:
                await context.close()
                raise AuthenticationError(
                    "Login timed out. Please complete the Google login "
                    "within 5 minutes via noVNC."
                )

            # Grab final cookies for the cookie header
            cookies = await context.cookies("https://suno.com")
            await context.close()

        cookie_map = {c["name"]: c["value"] for c in cookies}
        uat = cookie_map.get("__client_uat", "")
        session = cookie_map.get("__session", "")

        # Build cookie string from whatever we have
        parts = []
        if uat:
            parts.append(f"__client_uat={uat}")
        if session:
            parts.append(f"__session={session}")
        self._cookie = "; ".join(parts) if parts else "__client_uat=1"

        # JWT captured directly from network — skip Clerk API roundtrip
        self._jwt = captured["jwt"]
        self._jwt_timestamp = time.time()
        if captured.get("session_id"):
            self._session_id = captured["session_id"]

        if not self._session_id:
            await self._fetch_session_id()
        self._save_session()

    async def _fetch_session_id(self) -> None:
        """Get the Clerk session ID from the client endpoint."""
        url = f"{CLERK_BASE}/v1/client"
        params = {"_clerk_js_version": self.settings.clerk_js_version}
        headers = {"Cookie": self._cookie}

        async with httpx.AsyncClient() as client:
            resp = await client.get(url, params=params, headers=headers)
            if resp.status_code != 200:
                raise AuthenticationError(
                    f"Clerk /v1/client returned {resp.status_code}: {resp.text}"
                )
            data = resp.json()

        sessions = data.get("response", {}).get("sessions", [])
        logger.info("Clerk /v1/client response keys: %s, sessions: %d", list(data.keys()), len(sessions))
        if not sessions:
            raise AuthenticationError(
                f"No active Clerk sessions found. Response: {str(data)[:500]}"
            )

        active = [s for s in sessions if s.get("status") == "active"]
        if not active:
            statuses = [s.get("status") for s in sessions]
            raise AuthenticationError(
                f"No active Clerk sessions found. Session statuses: {statuses}"
            )

        self._session_id = active[0]["id"]

    async def _refresh_jwt(self) -> None:
        """Refresh the Clerk JWT using the stored session."""
        if not self._cookie or not self._session_id:
            raise AuthenticationError("No session to refresh. Please login first.")

        url = f"{CLERK_BASE}/v1/client/sessions/{self._session_id}/tokens"
        params = {"_clerk_js_version": self.settings.clerk_js_version}
        headers = {"Cookie": self._cookie}

        async with httpx.AsyncClient() as client:
            resp = await client.post(url, params=params, headers=headers)
            if resp.status_code != 200:
                raise AuthenticationError(
                    f"Clerk token refresh failed ({resp.status_code}): {resp.text}"
                )
            data = resp.json()

        self._jwt = data.get("jwt")
        if not self._jwt:
            raise AuthenticationError("No JWT in Clerk token response.")
        self._jwt_timestamp = time.time()

    async def get_token(self) -> str:
        """Return a valid JWT, refreshing if older than 50 seconds."""
        if not self.is_authenticated():
            raise AuthenticationError("Not authenticated. Please login first.")

        if not self._jwt or (time.time() - self._jwt_timestamp) > 50:
            await self._refresh_jwt()
            self._save_session()

        return self._jwt

    async def get_auth_headers(self) -> dict[str, str]:
        token = await self.get_token()
        return {"Authorization": f"Bearer {token}"}
