"""Playwright browser session management for Suno."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    Route,
    async_playwright,
)

logger = logging.getLogger(__name__)

SUNO_URL = "https://suno.com"
SUNO_CREATE_URL = "https://suno.com/create"
SUNO_API_BASE = "https://studio-api.suno.ai"

DEFAULT_SESSION_DIR = Path.home() / ".suno-mcp"
SESSION_FILE = DEFAULT_SESSION_DIR / "session.json"

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Maximum time (seconds) to wait for song generation polling
GENERATION_TIMEOUT = 300
POLL_INTERVAL = 3


class SunoBrowser:
    """Manages a persistent Playwright browser session for Suno interactions."""

    def __init__(
        self,
        headless: bool = True,
        session_file: Path = SESSION_FILE,
    ) -> None:
        self.headless = headless
        self.session_file = session_file
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._auth_token: str | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the browser and restore any saved session."""
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=self.headless)
        await self._create_context()

    async def _create_context(self) -> None:
        """Create a browser context, restoring session if available."""
        assert self._browser is not None

        if self.session_file.exists():
            logger.info("Restoring saved browser session from %s", self.session_file)
            try:
                self._context = await self._browser.new_context(
                    storage_state=str(self.session_file),
                    user_agent=DEFAULT_USER_AGENT,
                )
                return
            except Exception as exc:
                logger.warning("Failed to restore session: %s. Starting fresh.", exc)

        self._context = await self._browser.new_context(
            user_agent=DEFAULT_USER_AGENT,
        )

    async def close(self) -> None:
        """Close the browser and Playwright instance."""
        if self._context:
            try:
                await self._context.close()
            except Exception:
                pass
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Session persistence
    # ------------------------------------------------------------------

    async def save_session(self) -> None:
        """Persist current browser cookies and localStorage to disk."""
        assert self._context is not None
        self.session_file.parent.mkdir(parents=True, exist_ok=True)
        state = await self._context.storage_state()
        self.session_file.write_text(json.dumps(state))
        logger.info("Session saved to %s", self.session_file)

    def clear_session(self) -> None:
        """Delete the stored session file."""
        if self.session_file.exists():
            self.session_file.unlink()
            logger.info("Session cleared")

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    async def is_authenticated(self) -> bool:
        """Return True if the current session appears to be logged in."""
        assert self._context is not None
        page = await self._context.new_page()
        try:
            await page.goto(SUNO_URL, wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_load_state("networkidle", timeout=15_000)
            # Suno renders a user avatar / account menu when logged in.
            # We check for elements that only appear when authenticated.
            logged_in = await page.locator("[data-testid='user-avatar'], .clerk-user-button, [aria-label='User menu'], button[data-testid='create-button']").count() > 0
            return logged_in
        except Exception as exc:
            logger.debug("is_authenticated check failed: %s", exc)
            return False
        finally:
            await page.close()

    async def authenticate(self, timeout_seconds: int = 300) -> str:
        """
        Open a visible browser window so the user can log in to Suno.

        This relaunches the browser in non-headless mode if necessary,
        waits for the user to complete authentication, then saves the session.

        Returns a status message.
        """
        # Relaunch non-headless so the user can interact with the login UI.
        if self.headless:
            await self.close()
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(headless=False)
            await self._create_context()

        assert self._context is not None
        page = await self._context.new_page()
        try:
            await page.goto(f"{SUNO_URL}/sign-in", wait_until="domcontentloaded", timeout=30_000)

            # Wait until we land on a non-sign-in page, indicating successful login.
            deadline = time.time() + timeout_seconds
            while time.time() < deadline:
                current_url = page.url
                if "sign-in" not in current_url and "sign-up" not in current_url:
                    break
                await asyncio.sleep(1)
            else:
                raise TimeoutError(
                    f"Authentication did not complete within {timeout_seconds} seconds."
                )

            # Give the page time to finish loading post-login state.
            await page.wait_for_load_state("networkidle", timeout=15_000)
            await self.save_session()
            return "Authentication successful. Session saved."
        finally:
            await page.close()

    async def get_auth_token(self) -> str | None:
        """
        Retrieve the Clerk JWT session token from the browser's local storage.

        Returns None if not authenticated.
        """
        if self._auth_token:
            return self._auth_token

        assert self._context is not None
        page = await self._context.new_page()
        try:
            await page.goto(SUNO_URL, wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_load_state("networkidle", timeout=15_000)

            # Clerk stores the session token in a specific localStorage key or
            # sets it via a cookie; we intercept a real API request instead.
            token = await page.evaluate(
                """() => {
                    const keys = Object.keys(localStorage);
                    for (const k of keys) {
                        if (k.includes('clerk') || k.includes('session')) {
                            try {
                                const v = JSON.parse(localStorage.getItem(k));
                                if (v && v.tokens && v.tokens.userToken) {
                                    return v.tokens.userToken;
                                }
                            } catch {}
                        }
                    }
                    return null;
                }"""
            )
            if token:
                self._auth_token = token
            return token
        except Exception as exc:
            logger.debug("Failed to get auth token: %s", exc)
            return None
        finally:
            await page.close()

    # ------------------------------------------------------------------
    # Song generation
    # ------------------------------------------------------------------

    async def generate_song(
        self,
        prompt: str,
        *,
        custom_mode: bool = False,
        lyrics: str | None = None,
        style: str | None = None,
        title: str | None = None,
        instrumental: bool = False,
    ) -> list[dict[str, Any]]:
        """
        Generate songs on Suno by driving the web UI via Playwright.

        In *normal mode* (custom_mode=False), `prompt` is used as the song
        description.  In *custom mode* (custom_mode=True), you may also supply
        explicit `lyrics`, `style`, and `title`.

        Returns a list of song clip dicts with at least ``id``, ``title``, and
        ``audio_url`` keys once available.
        """
        assert self._context is not None
        generated_ids: list[str] = []
        response_lock = asyncio.Lock()

        async def handle_route(route: Route) -> None:
            """Intercept the generation API response to grab song IDs early."""
            request = route.request
            if "/api/generate" in request.url and request.method == "POST":
                response = await route.fetch()
                try:
                    body = await response.json()
                    clips = body.get("clips") or []
                    async with response_lock:
                        for clip in clips:
                            if clip.get("id"):
                                generated_ids.append(clip["id"])
                except Exception:
                    pass
                await route.fulfill(response=response)
            else:
                await route.continue_()

        page = await self._context.new_page()
        await page.route("**/*", handle_route)
        try:
            await page.goto(SUNO_CREATE_URL, wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_load_state("networkidle", timeout=15_000)

            if custom_mode:
                await self._fill_custom_mode(page, prompt, lyrics, style, title, instrumental)
            else:
                await self._fill_normal_mode(page, prompt, instrumental)

            # Click the Create / Generate button.
            create_btn = page.locator(
                "button:has-text('Create'), button:has-text('Generate'), [data-testid='create-button']"
            ).first
            await create_btn.click(timeout=10_000)

            # Wait for the generation API to respond (IDs captured via route intercept).
            deadline = time.time() + 30
            while time.time() < deadline:
                async with response_lock:
                    if generated_ids:
                        break
                await asyncio.sleep(0.5)

            if not generated_ids:
                # Fallback: scrape IDs from the page DOM.
                generated_ids = await self._scrape_generated_ids(page)

            if not generated_ids:
                raise RuntimeError(
                    "Song generation initiated but no song IDs were captured. "
                    "The UI may have changed. Please check https://suno.com/create manually."
                )

            # Save session in case any new auth cookies were set.
            await self.save_session()
        finally:
            await page.unroute("**/*")
            await page.close()

        # Poll until all songs are ready or timeout.
        return await self._poll_songs(generated_ids)

    async def _fill_normal_mode(
        self, page: Page, prompt: str, instrumental: bool
    ) -> None:
        """Fill in the normal (description-based) creation form."""
        # Dismiss any modal / onboarding dialogs first.
        await self._dismiss_dialogs(page)

        # The main prompt textarea.
        textarea = page.locator(
            "textarea[placeholder*='describe'], textarea[placeholder*='song'], textarea[data-testid='prompt-input']"
        ).first
        await textarea.click(timeout=10_000)
        await textarea.fill(prompt)

        if instrumental:
            inst_toggle = page.locator(
                "button:has-text('Instrumental'), [data-testid='instrumental-toggle']"
            ).first
            with suppress(Exception):
                await inst_toggle.click(timeout=5_000)

    async def _fill_custom_mode(
        self,
        page: Page,
        prompt: str,
        lyrics: str | None,
        style: str | None,
        title: str | None,
        instrumental: bool,
    ) -> None:
        """Enable custom mode and fill in the extended creation form."""
        await self._dismiss_dialogs(page)

        # Enable custom mode.
        custom_toggle = page.locator(
            "button:has-text('Custom'), [data-testid='custom-mode-toggle'], label:has-text('Custom')"
        ).first
        await custom_toggle.click(timeout=10_000)
        await page.wait_for_timeout(500)

        # Lyrics.
        lyrics_text = lyrics if lyrics else prompt
        lyrics_area = page.locator(
            "textarea[placeholder*='lyric'], textarea[placeholder*='Enter lyrics'], [data-testid='lyrics-input']"
        ).first
        await lyrics_area.fill(lyrics_text, timeout=10_000)

        # Style.
        if style:
            style_input = page.locator(
                "input[placeholder*='style'], textarea[placeholder*='style'], [data-testid='style-input']"
            ).first
            with suppress(Exception):
                await style_input.fill(style, timeout=5_000)

        # Title.
        if title:
            title_input = page.locator(
                "input[placeholder*='title'], [data-testid='title-input']"
            ).first
            with suppress(Exception):
                await title_input.fill(title, timeout=5_000)

        if instrumental:
            inst_toggle = page.locator(
                "button:has-text('Instrumental'), [data-testid='instrumental-toggle']"
            ).first
            with suppress(Exception):
                await inst_toggle.click(timeout=5_000)

    async def _dismiss_dialogs(self, page: Page) -> None:
        """Close any modal dialogs or cookie banners that may block interaction."""
        for selector in [
            "button:has-text('Accept')",
            "button:has-text('Dismiss')",
            "button:has-text('Close')",
            "button[aria-label='Close']",
        ]:
            try:
                locator = page.locator(selector).first
                if await locator.is_visible(timeout=1_000):
                    await locator.click(timeout=2_000)
            except Exception:
                pass

    async def _scrape_generated_ids(self, page: Page) -> list[str]:
        """Attempt to scrape song IDs from the page DOM as a fallback."""
        try:
            links = await page.locator("a[href*='/song/']").all()
            ids: list[str] = []
            for link in links:
                href = await link.get_attribute("href")
                if href:
                    parts = href.split("/song/")
                    if len(parts) > 1:
                        song_id = parts[1].strip("/")
                        if song_id and song_id not in ids:
                            ids.append(song_id)
            return ids
        except Exception:
            return []

    # ------------------------------------------------------------------
    # Song status / feed
    # ------------------------------------------------------------------

    async def _poll_songs(self, song_ids: list[str]) -> list[dict[str, Any]]:
        """Poll the Suno feed API until all songs are complete or timeout."""
        assert self._context is not None

        deadline = time.time() + GENERATION_TIMEOUT
        result_map: dict[str, dict[str, Any]] = {}

        while time.time() < deadline:
            songs = await self.get_songs_by_ids(song_ids)
            result_map = {s["id"]: s for s in songs if s.get("id")}

            all_done = all(
                result_map.get(sid, {}).get("status") in ("complete", "streaming", "error")
                for sid in song_ids
            )
            if all_done and len(result_map) == len(song_ids):
                break
            await asyncio.sleep(POLL_INTERVAL)

        return [result_map[sid] for sid in song_ids if sid in result_map]

    async def get_songs_by_ids(self, song_ids: list[str]) -> list[dict[str, Any]]:
        """Fetch song metadata for the given IDs from the Suno API."""
        assert self._context is not None
        ids_param = ",".join(song_ids)
        page = await self._context.new_page()
        try:
            result: list[dict[str, Any]] = []

            async def capture(route: Route) -> None:
                response = await route.fetch()
                try:
                    body = await response.json()
                    clips = body.get("clips") or (body if isinstance(body, list) else [])
                    result.extend(clips)
                except Exception:
                    pass
                await route.fulfill(response=response)

            await page.route(f"{SUNO_API_BASE}/api/feed/**", capture)
            await page.goto(
                f"{SUNO_API_BASE}/api/feed/?ids={ids_param}",
                wait_until="domcontentloaded",
                timeout=15_000,
            )
            await page.wait_for_load_state("networkidle", timeout=10_000)
            return result
        except Exception as exc:
            logger.debug("get_songs_by_ids failed: %s", exc)
            return []
        finally:
            await page.close()

    async def get_feed(self, page_size: int = 20) -> list[dict[str, Any]]:
        """
        Return the most recent songs from the user's feed.

        Navigates to the Suno home page and intercepts the feed API response.
        """
        assert self._context is not None
        songs: list[dict[str, Any]] = []

        browser_page = await self._context.new_page()
        try:
            async def capture(route: Route) -> None:
                response = await route.fetch()
                try:
                    body = await response.json()
                    clips = body.get("clips") or (body if isinstance(body, list) else [])
                    songs.extend(clips[:page_size])
                except Exception:
                    pass
                await route.fulfill(response=response)

            await browser_page.route(f"{SUNO_API_BASE}/api/feed/**", capture)
            await browser_page.goto(SUNO_URL, wait_until="domcontentloaded", timeout=30_000)
            await browser_page.wait_for_load_state("networkidle", timeout=15_000)

            # If nothing intercepted, try navigating to the profile page.
            if not songs:
                await browser_page.goto(
                    f"{SUNO_URL}/me", wait_until="domcontentloaded", timeout=30_000
                )
                await browser_page.wait_for_load_state("networkidle", timeout=15_000)

            return songs[:page_size]
        except Exception as exc:
            logger.debug("get_feed failed: %s", exc)
            return []
        finally:
            await browser_page.close()

    async def get_song_page_info(self, song_id: str) -> dict[str, Any]:
        """
        Navigate to a Suno song page and extract metadata from the DOM.

        Returns a dict with title, audio_url, image_url, and other available fields.
        """
        assert self._context is not None
        page = await self._context.new_page()
        try:
            song_data: dict[str, Any] = {"id": song_id}

            async def capture(route: Route) -> None:
                response = await route.fetch()
                if f"/song/{song_id}" in route.request.url or "feed" in route.request.url:
                    try:
                        body = await response.json()
                        if isinstance(body, dict):
                            song_data.update(body)
                        elif isinstance(body, list):
                            for item in body:
                                if item.get("id") == song_id:
                                    song_data.update(item)
                    except Exception:
                        pass
                await route.fulfill(response=response)

            await page.route("**/*", capture)
            await page.goto(
                f"{SUNO_URL}/song/{song_id}",
                wait_until="domcontentloaded",
                timeout=30_000,
            )
            await page.wait_for_load_state("networkidle", timeout=15_000)

            # Supplement with DOM scraping if API data wasn't captured.
            if not song_data.get("title"):
                title_el = page.locator("h1, h2, [data-testid='song-title']").first
                try:
                    song_data["title"] = await title_el.text_content(timeout=5_000)
                except Exception:
                    pass

            if not song_data.get("audio_url"):
                audio_el = page.locator("audio").first
                try:
                    song_data["audio_url"] = await audio_el.get_attribute("src", timeout=5_000)
                except Exception:
                    pass

            return song_data
        except Exception as exc:
            logger.debug("get_song_page_info(%s) failed: %s", song_id, exc)
            return {"id": song_id, "error": str(exc)}
        finally:
            await page.close()

