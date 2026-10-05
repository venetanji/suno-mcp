"""Browser automation for Suno using Playwright."""

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from playwright.async_api import BrowserContext, Page, Request, Response, async_playwright
from playwright.async_api import TimeoutError as BrowserTimeoutError

from .config import Settings
from .operations import BrowserOperations

logger = logging.getLogger(__name__)

SUNO_CREATE = "https://suno.com/create"
SUNO_SIGNIN = "https://suno.com/signin"


class SunoBrowser:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._playwright = None
        self._context: BrowserContext | None = None
        self._operations = BrowserOperations(
            settings.browser_queue_capacity, settings.browser_queue_timeout,
            settings.browser_operation_timeout,
        )
        self._clips: dict[str, dict] = {}
        self._generation: dict | None = None
        self._generation_request: Request | None = None
        self._captcha_visible = False
        self._api_events: list[dict] = []
        self._browser_errors: list[str] = []
        self._upload: dict | None = None
        self._inspiration_playlists: dict[tuple[str, ...], str] = {}
        self._audio_urls: dict[str, str] = {}
        self._audio_responses: dict[str, Response] = {}
        self._media_events: list[dict] = []
        self._metrics = {"attempts": 0, "captcha_attempts": 0, "captcha_challenges": 0}

    async def start(self):
        os.environ.setdefault("DISPLAY", self.settings.display)
        self.settings.auth_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = await async_playwright().start()
        self._context = await self._playwright.chromium.launch_persistent_context(
            str(self.settings.auth_dir / "chrome-profile"),
            channel=self.settings.browser_channel,
            accept_downloads=True,
            headless=False,
            args=["--no-sandbox"],
            viewport={"width": 1280, "height": 900},
        )
        self._context.on("response", self._observe_response)
        self._context.on("request", self._observe_request)
        self._context.on("page", self._watch_page)
        for page in self._context.pages:
            self._watch_page(page)

    def _watch_page(self, page: Page):
        page.on("pageerror", self._record_browser_error)

    def _record_browser_error(self, error):
        self._browser_errors.append(str(error)[:500])
        self._browser_errors = self._browser_errors[-10:]

    async def stop(self):
        if self._context:
            await self._context.close()
        if self._playwright:
            await self._playwright.stop()

    async def _get_page(self) -> Page:
        for page in self._context.pages:
            if not page.is_closed() and page.url.rstrip("/") == SUNO_CREATE:
                return page
        for page in self._context.pages:
            if not page.is_closed():
                return page
        return await self._context.new_page()

    async def _create_page(self) -> Page:
        page = await self._get_page()
        location = urlsplit(page.url)
        if location.hostname != "suno.com" or location.path.rstrip("/") != "/create":
            await page.goto(SUNO_CREATE, wait_until="load", timeout=30_000)
        await page.wait_for_load_state("load", timeout=30_000)
        return page

    def _guard_pending(self):
        if self._generation and self._generation["status"] in {"pending", "captcha_required"}:
            raise RuntimeError(
                "A generation is awaiting confirmation. Solve any CAPTCHA via noVNC, "
                "then call get_generation_status; do not submit another song."
            )
        if self._upload and self._upload["status"] == "upload_pending":
            raise RuntimeError("An upload is pending. Call get_upload_status instead of starting another browser operation.")

    async def begin_login(self) -> bool:
        self._guard_pending()
        async with self._operations.run("login"):
            self._guard_pending()
            if await self.is_logged_in():
                return True
            await self.open_for_login()
            return False

    async def is_logged_in(self) -> bool:
        self._guard_pending()
        page = await self._get_page()
        await page.goto(SUNO_CREATE, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(2000)
        return "signin" not in page.url and "login" not in page.url

    async def open_for_login(self):
        page = await self._get_page()
        await page.goto(SUNO_SIGNIN, wait_until="domcontentloaded", timeout=30_000)

    async def wait_for_login(self, timeout: float = 300) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(3)
            page = await self._get_page()
            if "suno.com" in page.url and "signin" not in page.url and "login" not in page.url:
                return True
        return False

    @staticmethod
    def _song_id(song_id: str) -> str:
        try:
            return str(UUID(song_id))
        except ValueError as error:
            raise ValueError("song_id must be a full Suno song UUID.") from error

    def _observe_request(self, request: Request):
        hostname = urlsplit(request.url).hostname or ""
        if (self._generation and self._generation["status"] in {"pending", "captcha_required"}
                and request.method == "POST" and re.search(r"/generate(?:/|$)", urlsplit(request.url).path)
                and (hostname.endswith(".suno.com") or hostname.endswith(".suno.ai"))):
            self._generation_request = request

    async def _observe_response(self, response: Response):
        hostname = urlsplit(response.url).hostname or ""
        path = urlsplit(response.url).path
        if response.request.resource_type == "media" or path.endswith((".mp3", ".wav", ".m4a", ".ogg", ".webm", ".flac", ".aac")):
            headers = await response.all_headers()
            self._media_events.append({
                "url": response.url, "status": response.status,
                "content_type": headers.get("content-type"),
                "content_length": headers.get("content-length"),
                "content_range": headers.get("content-range"),
                "request_range": await response.request.header_value("range"),
            })
            self._media_events = self._media_events[-20:]
            if response.status in {200, 206} and headers.get("content-type", "").lower().startswith("audio/"):
                for song_id in re.findall(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", path):
                    self._audio_urls[song_id] = response.url
                    self._audio_responses.pop(song_id, None)
                    self._audio_responses[song_id] = response
                while len(self._audio_responses) > 16:
                    self._audio_responses.pop(next(iter(self._audio_responses)))
        if not (hostname.endswith(".suno.com") or hostname.endswith(".suno.ai")):
            return
        is_generation = response.request.method == "POST" and bool(
            re.search(r"/generate(?:/|$)", path)
        )
        if not is_generation and not any(part in path for part in ("/feed", "/clip", "/upload", "/playlist")):
            return
        try:
            data = await response.json()
        except Exception:
            if is_generation and self._generation and response.request == self._generation_request:
                self._generation.update(status="error", message=f"Suno HTTP {response.status} returned an unreadable generation response. Inspect noVNC and your library before retrying.")
            return

        def collect(value):
            if isinstance(value, dict):
                if "id" in value and ("audio_url" in value or "status" in value):
                    self._clips[value["id"]] = value
                for child in value.values():
                    collect(child)
            elif isinstance(value, list):
                for child in value:
                    collect(child)

        collect(data)
        if path.endswith("/initialize-clip/") and response.ok and isinstance(data, dict) and data.get("clip_id"):
            self._upload = {"status": "uploaded", "id": data["clip_id"], "preview_url": f"https://suno.com/song/{data['clip_id']}"}
        self._api_events.append({"path": path, "status": response.status, "keys": list(data)[:20] if isinstance(data, dict) else ["list"]})
        self._api_events = self._api_events[-30:]
        if not is_generation or not self._generation or response.request != self._generation_request:
            return
        payload = response.request.post_data_json or {}
        self._api_events[-1]["request_keys"] = list(payload)
        self._api_events[-1]["metadata_keys"] = list(payload.get("metadata") or {})
        self._api_events[-1]["override_keys"] = list(payload.get("override_fields") or {})
        self._generation["submitted_options"] = {
            key: value for key, value in payload.items()
            if key in {"prompt", "tags", "title", "negative_tags", "make_instrumental", "mv",
                       "task", "generation_type", "continue_clip_id", "continue_at", "cover_clip_id", "playlist_id", "metadata", "override_fields"}
        }
        if "override_fields" in self._generation["submitted_options"]:
            overrides = payload.get("override_fields")
            if not isinstance(overrides, dict):
                overrides = {}
            self._generation["submitted_options"]["override_fields"] = {
                key: value for key, value in overrides.items()
                if key in {"duration", "duration_seconds", "custom_duration", "bpm", "sound_type"}
            }
        if "metadata" in self._generation["submitted_options"]:
            self._generation["submitted_options"]["metadata"] = {
                key: value for key, value in (payload.get("metadata") or {}).items()
                if key in {"control_sliders", "is_max_mode", "is_mumble", "create_mode", "vocal_gender", "duration", "tts", "sfx", "background_music", "is_speech", "backing_music", "sfx_type", "bpm", "key", "inspiration_playlist_id"}
            }
        clips = data.get("clips", []) if isinstance(data, dict) else []
        if response.ok and clips:
            self._generation.update(status="submitted", songs=[self._clip_summary(clip) for clip in clips],
                                    message="Generation accepted. Use get_song to check completion and download_song to save audio.")
        else:
            detail = data.get("detail", data.get("message", "No clips returned")) if isinstance(data, dict) else str(data)
            self._generation.update(status="error", message=f"Suno HTTP {response.status}: {str(detail)[:500]}")

    def _clip_summary(self, clip: dict) -> dict:
        result = {key: clip.get(key) for key in ("id", "title", "status", "audio_url", "entity_type", "model_name")}
        result["preview_url"] = f"https://suno.com/song/{clip['id']}"
        result["duration"] = (clip.get("metadata") or {}).get("duration")
        if not result["audio_url"] or "/api/forbidden" in result["audio_url"]:
            result["audio_url"] = self._audio_urls.get(clip.get("id"))
        return result

    async def _has_captcha(self, page: Page) -> bool:
        return await page.locator(
            'iframe[src*="hcaptcha"], iframe[src*="recaptcha"], '
            '[data-hcaptcha-widget-id], .hcaptcha-box, #hcaptcha'
        ).evaluate_all("""elements => elements.some(element => {
            const rect = element.getBoundingClientRect();
            const style = getComputedStyle(element);
            return rect.width >= 100 && rect.height >= 60 && rect.bottom > 0
                && rect.right > 0 && rect.top < innerHeight && rect.left < innerWidth
                && style.visibility !== 'hidden' && style.display !== 'none';
        })""")

    async def get_generation_status(self) -> dict:
        if not self._generation:
            return {"status": "idle", "metrics": dict(self._metrics), "operations": self._operations.snapshot()}
        if self._generation["status"] == "submitted":
            self._generation["songs"] = [self._clip_summary(self._clips.get(clip["id"], clip)) for clip in self._generation["songs"]]
            if all(clip["status"] == "complete" for clip in self._generation["songs"]):
                self._generation.update(status="complete", message="Generation complete. Tracks are ready for preview and playback-audio downloads.")
            elif any(clip["status"] == "error" for clip in self._generation["songs"]):
                self._generation.update(status="error", message="Suno failed to generate one or more tracks. Inspect the returned song IDs with get_song.")
        if self._generation["status"] in {"pending", "captcha_required"}:
            visible = await self._has_captcha(await self._get_page())
            if visible:
                if not self._captcha_visible:
                    self._metrics["captcha_challenges"] += 1
                    if not self._generation["captcha_detected"]:
                        self._metrics["captcha_attempts"] += 1
                    self._generation["captcha_detected"] = True
                    logger.warning("CAPTCHA required for attempt %s", self._generation["attempt_id"])
                self._generation.update(
                    status="captcha_required",
                    novnc_url=f"{self.settings.base_url}/vnc.html?autoconnect=1&resize=scale",
                    message="Solve the CAPTCHA via noVNC, then call get_generation_status. Do not retry Create.",
                )
            elif self._generation["status"] == "captcha_required":
                self._generation.update(status="pending", message="Waiting for Suno's generation response; do not retry Create.")
            self._captcha_visible = visible
        return {**self._generation, "metrics": dict(self._metrics), "operations": self._operations.snapshot()}

    async def _select_tab(self, page: Page, name: str):
        tab = page.get_by_role("tab", name=name, exact=True)
        await tab.wait_for(timeout=15_000)
        await tab.click(timeout=15_000)

    async def abandon_generation_wait(self, attempt_id: str) -> dict:
        async with self._operations.run("abandon_generation_wait"):
            if not self._generation or self._generation["attempt_id"] != attempt_id:
                raise ValueError("attempt_id must match the current generation returned by get_generation_status.")
            if self._generation["status"] not in {"pending", "captcha_required"}:
                raise ValueError("Only an unconfirmed generation wait can be abandoned.")
            self._generation_request = None
            self._generation.update(
                status="abandoned",
                message="Local wait abandoned. This cannot undo an accepted Suno request or refund credits. Check your Suno library before repeating the same generation.",
            )
            await (await self._get_page()).goto(SUNO_CREATE, wait_until="domcontentloaded", timeout=30_000)
            self._captcha_visible = False
            return {**self._generation, "metrics": dict(self._metrics)}

    async def _songs_form(self, page: Page):
        advanced = page.get_by_role("tab", name="Advanced", exact=True)
        deadline = time.monotonic() + 15
        while True:
            await self._select_tab(page, "Songs")
            try:
                await advanced.wait_for(timeout=1000)
                break
            except BrowserTimeoutError:
                if time.monotonic() >= deadline:
                    raise
        await self._select_tab(page, "Advanced")
        await page.get_by_role("textbox", name="Lyrics editor", exact=True).wait_for(timeout=10_000)

    async def _reset_form(self, page: Page):
        await page.keyboard.press("Escape")
        clear = page.get_by_role("button", name="Clear all form inputs", exact=True)
        if await clear.count() and await clear.is_enabled():
            await clear.click(timeout=10_000)
            confirmation = page.get_by_role("alertdialog", name=re.compile(r"clear.*form", re.I))
            await confirmation.wait_for(state="visible", timeout=10_000)
            await confirmation.get_by_role("button", name="Confirm", exact=True).click(timeout=10_000)
            await confirmation.wait_for(state="hidden", timeout=10_000)

    async def _submit(self, page: Page, button_name: str, dry_run: bool) -> dict:
        await page.screenshot(path=str(self.settings.auth_dir / "debug_pre_create.png"))
        if dry_run:
            fields = await page.get_by_role("textbox").evaluate_all("""elements => elements.map(element => ({
                label: element.getAttribute('aria-label') || element.placeholder || '',
                value: element.isContentEditable ? element.innerText : element.value
            }))""")
            sliders = await page.get_by_role("slider").evaluate_all("""elements => elements.map(element => ({
                label: element.getAttribute('aria-label'), value: element.getAttribute('aria-valuenow')
            }))""")
            return {"status": "prepared", "message": "Form filled; Create was not clicked and no generation credits were spent.", "fields": fields, "sliders": sliders}
        button = page.get_by_role("button", name=button_name, exact=True)
        await button.wait_for(state="visible", timeout=10_000)
        if not await button.is_enabled():
            raise RuntimeError("Create is disabled. Inspect noVNC; no generation was submitted.")
        self._generation = {"attempt_id": str(uuid4()), "status": "pending", "songs": [], "captcha_detected": False,
                            "message": "Waiting for Suno's generation response. Call get_generation_status; do not retry Create."}
        self._generation_request = None
        self._captcha_visible = False
        self._metrics["attempts"] += 1
        await button.click(timeout=15_000)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            state = await self.get_generation_status()
            if state["status"] != "pending":
                return state
            await asyncio.sleep(0.25)
        return await self.get_generation_status()

    async def _fill(self, locator, value: str):
        await locator.fill(value, timeout=10_000)
        await self._verify_value(locator, value)

    async def _verify_value(self, locator, value: str):
        deadline = time.monotonic() + 2
        actual = await locator.evaluate("element => element.isContentEditable ? element.innerText : element.value")
        while actual.strip() != value.strip() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
            actual = await locator.evaluate("element => element.isContentEditable ? element.innerText : element.value")
        if actual.strip() != value.strip():
            label = await locator.evaluate("element => element.getAttribute('aria-label') || element.placeholder || element.tagName")
            raise RuntimeError(
                f"Suno did not accept {label}: expected {len(value)} characters, read {len(actual)}. "
                "Generation was not submitted."
            )

    async def _set_slider(self, page: Page, name: str, value: int):
        slider = page.get_by_role("slider", name=name, exact=True).first
        await slider.scroll_into_view_if_needed(timeout=10_000)
        current = int(await slider.get_attribute("aria-valuenow"))
        direction = "ArrowRight" if value > current else "ArrowLeft"
        for step in range(101):
            if current == value:
                return
            await slider.press(direction)
            updated = int(await slider.get_attribute("aria-valuenow"))
            if updated == current:
                break
            current = updated
        raise RuntimeError(f"Could not set {name} to {value}; generation was not submitted.")

    async def _choose(self, page: Page, label: str, button_name: str):
        if label == "Vocal Gender":
            button = page.get_by_role("button", name=button_name, exact=True)
        else:
            group = page.get_by_text(label, exact=True).first.locator("xpath=ancestor::*[.//button][1]")
            button = group.get_by_role("button", name=button_name, exact=True)
        if await button.get_attribute("aria-pressed") != "true":
            await button.click(timeout=10_000)

    async def generate_speech(
        self, script: str, tone: str = "", background_music: bool = False,
        vocal_gender: str = "", variety: int | None = None, dry_run: bool = False,
    ) -> dict:
        if not script.strip() or len(script) > 5000 or len(tone) > 1000:
            raise ValueError("Provide a nonempty script (up to 5000 characters) and tone up to 1000 characters.")
        if variety is not None and not 0 <= variety <= 4:
            raise ValueError("variety must be between 0 and 4.")
        self._guard_pending()
        async with self._operations.run("generate_speech"):
            self._guard_pending()
            page = await self._create_page()
            await self._select_tab(page, "Speech")
            await self._reset_form(page)
            await self._select_tab(page, "Speech")
            await self._fill(page.get_by_role("textbox", name="Script", exact=True), script)
            await self._fill(page.get_by_role("textbox", name="Tone", exact=True), tone)
            advanced = page.get_by_role("button", name="Advanced", exact=True)
            if await advanced.get_attribute("aria-expanded") == "false":
                await advanced.click()
            await self._choose(page, "Background music", "On" if background_music else "Off")
            if vocal_gender:
                await self._choose(page, "Vocal Gender", vocal_gender.title())
            if variety is not None:
                await self._set_slider(page, "Variety", variety)
            return await self._submit(page, "Create speech", dry_run)

    async def generate_sound(self, prompt: str, sound_type: str = "one_shot", bpm: int | None = None, dry_run: bool = False) -> dict:
        if not prompt.strip() or len(prompt) > 500:
            raise ValueError("Provide a nonempty sound prompt of up to 500 characters.")
        if bpm is not None and not 1 <= bpm <= 300:
            raise ValueError("bpm must be between 1 and 300.")
        self._guard_pending()
        async with self._operations.run("generate_sound"):
            self._guard_pending()
            page = await self._create_page()
            await self._select_tab(page, "Sounds")
            await self._reset_form(page)
            await self._select_tab(page, "Sounds")
            await self._fill(page.get_by_placeholder("Describe the sound you want", exact=True), prompt)
            await page.get_by_role("button", name="Loop" if sound_type == "loop" else "One-Shot", exact=True).click()
            await self._fill(page.get_by_role("spinbutton", name="Auto", exact=True), str(bpm) if bpm is not None else "")
            return await self._submit(page, "Create song", dry_run)

    async def generate_song(
        self,
        lyrics: str = "",
        tags: str = "",
        title: str = "",
        make_instrumental: bool = False,
        negative_prompt: str = "",
        model: str = "",
        weirdness: int | None = None,
        style_influence: int | None = None,
        variety: int | None = None,
        vocal_gender: str = "",
        duration_seconds: int | None = None,
        reference_audio_id: str = "",
        audio_mode: str = "cover",
        audio_influence: int | None = None,
        inspiration_ids: list[str] | None = None,
        inspiration_playlist: str = "",
        dry_run: bool = False,
    ) -> dict:
        if not lyrics.strip() and not tags.strip():
            raise ValueError("Provide lyrics or tags before generating a song.")
        if make_instrumental and lyrics.strip():
            raise ValueError("Instrumental generation requires empty lyrics.")
        if len(lyrics) > 5000 or len(tags) > 1000 or len(negative_prompt) > 1000:
            raise ValueError("Maximum lengths: lyrics 5000; tags and negative_prompt 1000 characters.")
        for name, value, maximum in (("weirdness", weirdness, 100), ("style_influence", style_influence, 100),
                                     ("audio_influence", audio_influence, 100), ("variety", variety, 4)):
            if value is not None and not 0 <= value <= maximum:
                raise ValueError(f"{name} must be between 0 and {maximum}.")
        if duration_seconds is not None and not 10 <= duration_seconds <= 360:
            raise ValueError("duration_seconds must be between 10 and 360; availability depends on the selected model.")
        if audio_influence is not None and not reference_audio_id:
            raise ValueError("audio_influence requires reference_audio_id.")
        if inspiration_ids:
            if len(inspiration_ids) > 4:
                raise ValueError("At most four inspiration tracks are supported.")
            inspiration_ids = [self._song_id(song_id) for song_id in inspiration_ids]
            if len(set(inspiration_ids)) != len(inspiration_ids):
                raise ValueError("Inspiration track IDs must be unique.")
        if reference_audio_id and (inspiration_ids or inspiration_playlist):
            raise ValueError("Choose either reference audio or inspiration tracks, not both.")
        if inspiration_ids and inspiration_playlist:
            raise ValueError("Choose track IDs or an existing inspiration playlist, not both.")
        self._guard_pending()
        async with self._operations.run("generate_song"):
            self._guard_pending()
            page = await self._get_page()
            if reference_audio_id:
                await self._use_reference(page, self._song_id(reference_audio_id), audio_mode)
            else:
                page = await self._create_page()
            if "signin" in page.url or "login" in page.url:
                raise RuntimeError("Not logged in; call suno_login first.")
            consent = page.get_by_role("button", name=re.compile(r"^(allow all|accept all)( cookies)?$", re.I))
            if await consent.count():
                await consent.first.click()
            await self._songs_form(page)
            if not reference_audio_id:
                await self._reset_form(page)
                await self._songs_form(page)
            if inspiration_ids or inspiration_playlist:
                await self._set_inspiration(page, inspiration_ids or [], inspiration_playlist)
            if model:
                await page.get_by_role("button", name=re.compile(r"^v\d")).first.click()
                await page.get_by_text(model, exact=True).last.click(timeout=10_000)
            lyrics_field = page.get_by_role("textbox", name="Lyrics editor", exact=True)
            await self._fill(lyrics_field, lyrics)
            styles = page.get_by_text("Styles", exact=True).first.locator("xpath=ancestor::*[.//textarea][1]").locator("textarea").first
            await self._fill(styles, tags)
            title_field = page.get_by_role("textbox", name="Song Title (Optional)", exact=True)
            await self._fill(title_field, title)
            options = page.get_by_role("button", name="More Options", exact=True)
            if not await page.get_by_role("textbox", name="Exclude styles", exact=True).count():
                await options.click(timeout=10_000)
            excluded_styles = page.get_by_role("textbox", name="Exclude styles", exact=True)
            await self._fill(excluded_styles, negative_prompt)
            for name, value in (("Weirdness", weirdness), ("Style Influence", style_influence),
                                ("Variety", variety), ("Audio Influence", audio_influence)):
                if value is not None:
                    await self._set_slider(page, name, value)
            if vocal_gender:
                group = page.get_by_text("Vocal Gender", exact=True).locator("xpath=ancestor::*[.//button][1]")
                await group.get_by_role("button", name=vocal_gender.title(), exact=True).click(timeout=10_000)
            if duration_seconds is not None:
                group = page.get_by_text("Duration", exact=True).locator("xpath=ancestor::*[.//button][1]")
                await group.get_by_role("button", name="Custom", exact=True).click(timeout=10_000)
                duration_slider = page.get_by_role("slider", name="Duration", exact=True)
                minimum = int(await duration_slider.get_attribute("aria-valuemin"))
                maximum = int(await duration_slider.get_attribute("aria-valuemax"))
                if not minimum <= duration_seconds <= maximum:
                    raise ValueError(f"This model supports duration_seconds between {minimum} and {maximum}.")
                duration_field = page.get_by_role("textbox", name="Duration", exact=True)
                await self._fill(duration_field, f"{duration_seconds // 60}:{duration_seconds % 60:02d}")
                await duration_field.press("Tab")
                if int(await duration_slider.get_attribute("aria-valuenow")) != duration_seconds:
                    raise RuntimeError("Suno did not accept the duration; generation was not submitted.")
            for field, value in ((lyrics_field, lyrics), (styles, tags), (title_field, title), (excluded_styles, negative_prompt)):
                await self._verify_value(field, value)
            return await self._submit(page, "Create song", dry_run)

    async def _set_inspiration(self, page: Page, song_ids: list[str], playlist_name: str = ""):
        if song_ids:
            track_key = tuple(song_ids)
            playlist_name = self._inspiration_playlists.get(track_key, "")
            if not playlist_name:
                digest = hashlib.sha256(",".join(song_ids).encode("ascii")).hexdigest()[:10]
                playlist_name = f"MCP Inspo {digest} {str(uuid4())[:4]}"
                for index, song_id in enumerate(song_ids):
                    await page.goto(f"https://suno.com/song/{song_id}", wait_until="domcontentloaded", timeout=30_000)
                    await page.get_by_role("button", name="Add to Playlist", exact=True).click(timeout=15_000)
                    dialog = page.get_by_role("dialog", name="Add to Playlist", exact=True)
                    if index == 0:
                        await self._fill(dialog.get_by_role("textbox", name="Playlist name", exact=True), playlist_name)
                        await dialog.get_by_role("button", name="Create playlist", exact=True).click(timeout=15_000)
                    else:
                        await dialog.get_by_text(playlist_name, exact=True).click(timeout=15_000)
                    await dialog.wait_for(state="hidden", timeout=15_000)
                self._inspiration_playlists[track_key] = playlist_name
            await page.goto(SUNO_CREATE, wait_until="domcontentloaded", timeout=30_000)
            await self._songs_form(page)
            await self._reset_form(page)
        await page.get_by_role("button", name="Add inspiration from a playlist", exact=True).click(timeout=15_000)
        dialog = page.get_by_role("dialog")
        await dialog.get_by_role("textbox", name="Search", exact=True).fill(playlist_name)
        await dialog.get_by_text(playlist_name, exact=True).click(timeout=15_000)
        await dialog.wait_for(state="hidden", timeout=15_000)

    async def _use_reference(self, page: Page, song_id: str, audio_mode: str):
        await page.goto(f"https://suno.com/song/{song_id}", wait_until="domcontentloaded", timeout=30_000)
        await page.get_by_role("button", name=re.compile(r"^Remix", re.I)).last.click(timeout=10_000)
        choice = page.get_by_role("menuitem", name=re.compile(rf"^{re.escape(audio_mode)}", re.I))
        if not await choice.count():
            choice = page.get_by_role("button", name=re.compile(rf"^{re.escape(audio_mode)}", re.I))
        await choice.first.click(timeout=10_000)
        await page.wait_for_url("**/create**", timeout=15_000)
        await page.get_by_role("button", name=f"Change condition type from {audio_mode.title()}", exact=True).wait_for(timeout=15_000)
        await page.get_by_role("textbox", name="Lyrics editor", exact=True).wait_for(timeout=15_000)

    async def upload_audio(self, audio_path: str, confirm_rights: bool, description: str = "", audio_type: str = "Song Demo") -> dict:
        if not confirm_rights:
            raise ValueError("Set confirm_rights=true only for audio you have permission to upload.")
        path = Path(audio_path).resolve()
        directories = (self.settings.auth_dir / "uploads", self.settings.download_dir)
        if not any(path.is_relative_to(directory.resolve()) for directory in directories):
            raise ValueError("Audio must be in /data/uploads or the configured downloads directory.")
        if not path.is_file() or path.suffix.lower() not in {".wav", ".flac", ".mp3", ".ogg", ".opus", ".webm", ".m4a", ".aac", ".mp4"}:
            raise ValueError("Provide an existing supported audio file.")
        self._guard_pending()
        async with self._operations.run("upload_audio"):
            self._guard_pending()
            if self._upload and self._upload["status"] == "upload_pending":
                raise RuntimeError("An upload is pending. Call get_upload_status instead of uploading again.")
            page = await self._get_page()
            await page.goto(SUNO_CREATE, wait_until="domcontentloaded", timeout=30_000)
            await self._songs_form(page)
            await self._reset_form(page)
            self._upload = {"status": "upload_pending", "filename": path.name}
            await page.locator('input[type="file"][accept*="audio"]').set_input_files(str(path))
            await page.wait_for_timeout(1500)
            agree = page.get_by_role("button", name="Agree to Terms", exact=True)
            if await agree.count():
                await agree.click(timeout=10_000)
                await page.wait_for_timeout(1500)
            dialog = page.get_by_role("dialog", name="Upload", exact=True)
            description_field = dialog.get_by_role("textbox")
            if await description_field.count():
                await self._fill(description_field, description)
            await dialog.get_by_role("button", name=audio_type, exact=True).click(timeout=15_000)
            await dialog.get_by_role("button", name="Continue", exact=True).click(timeout=60_000)
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                if self._upload["status"] == "uploaded":
                    return dict(self._upload)
                await asyncio.sleep(0.5)
            return {**self._upload, "message": "Upload is still processing. Call get_upload_status; do not upload again."}

    def get_upload_status(self) -> dict:
        return dict(self._upload) if self._upload else {"status": "idle"}

    async def get_song(self, song_id: str) -> dict:
        song_id = self._song_id(song_id)
        cached = self._clips.get(song_id)
        if cached and cached.get("status") in {"complete", "error"}:
            return self._clip_summary(cached)
        self._guard_pending()
        async with self._operations.run("get_song"):
            cached = self._clips.get(song_id)
            if cached and cached.get("status") in {"complete", "error"}:
                return self._clip_summary(cached)
            self._guard_pending()
            page = await self._get_page()
            await page.goto(f"https://suno.com/song/{song_id}", wait_until="domcontentloaded", timeout=30_000)

            def find_clip(value):
                if isinstance(value, dict):
                    if value.get("id") == song_id and "status" in value:
                        return value
                    children = value.values()
                elif isinstance(value, list):
                    children = value
                elif isinstance(value, str) and value.lstrip().startswith(("{", "[")):
                    try:
                        children = [json.loads(value)]
                    except ValueError:
                        return None
                else:
                    return None
                for child in children:
                    found = find_clip(child)
                    if found:
                        return found
                return None

            decoder = json.JSONDecoder()
            for attempt in range(20):
                state = await page.evaluate("""() => ({
                    initial: window.__NEXT_DATA__ || null,
                    flight: (window.__next_f || []).filter(entry => typeof entry[1] === 'string').map(entry => entry[1]).join(''),
                    scripts: Array.from(document.scripts).map(script => script.textContent).filter(text => text.includes('__next_f'))
                })""")
                clip = find_clip(state["initial"])
                if not clip:
                    chunks = []
                    for script in state["scripts"]:
                        for match in re.finditer(r'\.push\((\[)', script):
                            try:
                                entry, position = decoder.raw_decode(script, match.start(1))
                            except ValueError:
                                continue
                            if isinstance(entry, list) and len(entry) > 1 and isinstance(entry[1], str):
                                chunks.append(entry[1])
                    flight = state["flight"] + "\n" + "".join(chunks)
                    for match in re.finditer(r'\{\s*"', flight):
                        try:
                            value, position = decoder.raw_decode(flight, match.start())
                        except ValueError:
                            continue
                        clip = find_clip(value)
                        if clip:
                            break
                if clip:
                    self._clips[song_id] = clip
                    return self._clip_summary(clip)
                cached = self._clips.get(song_id)
                if cached and cached.get("status") in {"complete", "error"}:
                    return self._clip_summary(cached)
                await asyncio.sleep(0.5)
            raise RuntimeError("Could not read this track's status from Suno's native page data.")

    @staticmethod
    def _playback_extension(body: bytes, headers: dict[str, str], status: int) -> str:
        if status not in {200, 206}:
            raise RuntimeError(f"The player audio request returned HTTP {status}.")
        if not body:
            raise RuntimeError("The player returned an empty audio file.")
        content_range = headers.get("content-range", "")
        if status == 206 or content_range:
            matched_range = re.fullmatch(r"bytes 0-(\d+)/(\d+)", content_range)
            if (not matched_range or int(matched_range[1]) + 1 != int(matched_range[2])
                    or len(body) != int(matched_range[2])):
                raise RuntimeError("The player returned only part of the audio file.")
        content_length = headers.get("content-length")
        if content_length and headers.get("content-encoding", "identity").lower() == "identity":
            if not content_length.isdigit() or len(body) != int(content_length):
                raise RuntimeError("The player audio file was truncated.")
        content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type and not (content_type.startswith("audio/") or content_type in {"application/ogg", "application/octet-stream"}):
            raise RuntimeError(f"The player returned non-audio content (Content-Type: {content_type}).")
        if body[4:8] == b"ftyp":
            return ".m4a"
        if body[:4] in {b"RIFF", b"RF64"} and body[8:12] == b"WAVE":
            return ".wav"
        if body.startswith(b"fLaC"):
            return ".flac"
        if body.startswith(b"OggS"):
            return ".ogg"
        if body.startswith(b"\x1a\x45\xdf\xa3"):
            return ".webm"
        if (
            body.startswith(b"ADIF") or len(body) >= 2 and body[0] == 0xff and body[1] & 0xf6 == 0xf0
        ):
            return ".aac"
        if body.startswith(b"ID3") or len(body) >= 2 and body[0] == 0xff and body[1] & 0xe0 == 0xe0:
            return ".mp3"
        raise RuntimeError(f"The player did not return a recognized audio file (Content-Type: {content_type or 'missing'}).")

    async def _read_player_audio(self, page: Page, song_id: str) -> tuple[bytes, str]:
        await page.wait_for_function("""songId => Array.from(document.querySelectorAll('audio')).some(audio => {
            const source = audio.currentSrc || audio.src;
            return (source.startsWith('blob:') || source.includes(songId)) && audio.readyState >= 1 && !audio.error;
        })""", arg=song_id, timeout=30_000)
        await page.evaluate("""songId => {
            const audio = Array.from(document.querySelectorAll('audio')).find(element => {
                const source = element.currentSrc || element.src;
                return (source.startsWith('blob:') || source.includes(songId)) && element.readyState >= 1 && !element.error;
            });
            audio.muted = true;
            audio.playbackRate = 16;
        }""", song_id)
        await page.wait_for_function("""songId => {
            const audio = Array.from(document.querySelectorAll('audio')).find(element => {
                const source = element.currentSrc || element.src;
                return (source.startsWith('blob:') || source.includes(songId)) && element.readyState >= 1 && !element.error;
            });
            if (!audio) return false;
            const source = window.__sunoMcpPlaybackSources.get(audio.currentSrc || audio.src);
            const streams = window.__sunoMcpPlaybackStreams.filter(stream => stream.source === source && stream.type.startsWith('audio/'));
            if (!streams.length) return true;
            return streams.some(stream => stream.chunks.length && !stream.buffer.updating && (
                stream.source.readyState === 'ended' ||
                Number.isFinite(audio.duration) && stream.buffer.buffered.length &&
                stream.buffer.buffered.end(stream.buffer.buffered.length - 1) >= audio.duration - 0.001
            ));
        }""", arg=song_id, timeout=60_000)
        payload = await asyncio.wait_for(page.evaluate("""async songId => {
            const audio = Array.from(document.querySelectorAll('audio')).find(element => {
                const source = element.currentSrc || element.src;
                return (source.startsWith('blob:') || source.includes(songId)) && element.readyState >= 1 && !element.error;
            });
            const source = window.__sunoMcpPlaybackSources.get(audio.currentSrc || audio.src);
            const stream = window.__sunoMcpPlaybackStreams.find(entry => entry.source === source && entry.type.startsWith('audio/') && entry.chunks.length);
            let bytes, headers, status;
            if (stream) {
                const size = stream.chunks.reduce((total, chunk) => total + chunk.length, 0);
                bytes = new Uint8Array(size);
                let offset = 0;
                for (const chunk of stream.chunks) {
                    bytes.set(chunk, offset);
                    offset += chunk.length;
                }
                headers = {'content-type': stream.type, 'content-length': String(size)};
                status = 200;
            } else {
                const response = await fetch(audio.currentSrc || audio.src);
                if (!response.ok) throw new Error(`Player audio returned HTTP ${response.status}`);
                bytes = new Uint8Array(await response.arrayBuffer());
                headers = Object.fromEntries(response.headers);
                status = response.status;
            }
            let binary = '';
            for (let offset = 0; offset < bytes.length; offset += 32768) {
                binary += String.fromCharCode(...bytes.subarray(offset, offset + 32768));
            }
            return {data: btoa(binary), status, headers};
        }""", song_id), timeout=30)
        body = base64.b64decode(payload["data"], validate=True)
        return body, self._playback_extension(body, payload["headers"], payload["status"])

    async def _read_playback_audio(self, song_id: str, response: Response | None) -> tuple[bytes, str]:
        if response is not None:
            try:
                body = await asyncio.wait_for(response.body(), timeout=30)
                extension = self._playback_extension(body, await response.all_headers(), response.status)
                return body, extension
            except Exception:
                logger.debug("Reading the full observed playback resource for %s", song_id)
        audio_url = response.url if response is not None else self._audio_urls.get(song_id)
        if not audio_url:
            raise RuntimeError("No successful player audio response has been observed for this track.")
        fetched = await self._context.request.get(
            audio_url, headers={"Referer": f"https://suno.com/song/{song_id}", "Accept": "audio/*"}, timeout=30_000,
        )
        try:
            body = await fetched.body()
            extension = self._playback_extension(body, fetched.headers, fetched.status)
            return body, extension
        finally:
            await fetched.dispose()

    async def download_song(self, song_id: str, output_dir: Path | None = None) -> str:
        song_id = self._song_id(song_id)
        self._guard_pending()
        async with self._operations.run("download_song"):
            self._guard_pending()
            clip = await self.get_song(song_id)
            if clip["status"] != "complete":
                raise RuntimeError(f"Track status is {clip['status']}; check get_song before downloading.")
            page = await self._context.new_page()
            try:
                await page.add_init_script("""(() => {
                    window.__sunoMcpPlaybackStreams = [];
                    window.__sunoMcpPlaybackSources = new Map();
                    const createObjectURL = URL.createObjectURL;
                    URL.createObjectURL = function(source) {
                        const url = createObjectURL.call(this, source);
                        window.__sunoMcpPlaybackSources.set(url, source);
                        return url;
                    };
                    const addSourceBuffer = MediaSource.prototype.addSourceBuffer;
                    MediaSource.prototype.addSourceBuffer = function(type) {
                        const buffer = addSourceBuffer.call(this, type);
                        const stream = {source: this, buffer, type, chunks: []};
                        window.__sunoMcpPlaybackStreams.push(stream);
                        const appendBuffer = buffer.appendBuffer;
                        buffer.appendBuffer = function(data) {
                            const bytes = ArrayBuffer.isView(data)
                                ? new Uint8Array(data.buffer, data.byteOffset, data.byteLength).slice()
                                : new Uint8Array(data).slice();
                            const result = appendBuffer.call(this, data);
                            stream.chunks.push(bytes);
                            return result;
                        };
                        return buffer;
                    };
                })()""")
                await page.goto(f"https://suno.com/song/{song_id}", wait_until="domcontentloaded", timeout=30_000)
                play = page.get_by_role("button", name="Play", exact=True).first
                await play.wait_for(state="visible", timeout=20_000)
                await play.click(timeout=15_000)
                try:
                    audio = await self._read_player_audio(page, song_id)
                except Exception as player_error:
                    try:
                        audio = await self._read_playback_audio(song_id, self._audio_responses.get(song_id))
                    except Exception as response_error:
                        raise RuntimeError(f"Player capture: {player_error}; network fallback: {response_error}") from player_error
            except Exception as error:
                raise RuntimeError(
                    f"Could not save this track's playable audio: {error}. "
                    "Inspect the track in noVNC and check that playback is available. No export or purchase was requested."
                ) from error
            finally:
                await page.close()
            body, extension = audio
            directory = output_dir or self.settings.download_dir
            directory.mkdir(parents=True, exist_ok=True)
            filepath = directory / f"{song_id}{extension}"
            temporary = directory / f".{song_id}.{uuid4().hex}.part"
            try:
                temporary.write_bytes(body)
                temporary.replace(filepath)
            finally:
                temporary.unlink(missing_ok=True)
            return str(filepath)
