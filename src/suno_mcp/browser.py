"""Browser automation for Suno using Playwright."""

import asyncio
import logging
import re
import time
from pathlib import Path

from playwright.async_api import BrowserContext, Page, async_playwright

from .config import Settings

logger = logging.getLogger(__name__)

SUNO_CREATE = "https://suno.com/create"
SUNO_SIGNIN = "https://suno.com/signin"


class SunoBrowser:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._playwright = None
        self._context: BrowserContext | None = None

    async def start(self):
        import os
        os.environ.setdefault("DISPLAY", self.settings.display)
        profile_dir = str(self.settings.auth_dir / "chrome-profile")
        self._playwright = await async_playwright().start()
        self._context = await self._playwright.chromium.launch_persistent_context(
            profile_dir,
            headless=False,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            ),
        )

    async def stop(self):
        if self._context:
            await self._context.close()
        if self._playwright:
            await self._playwright.stop()

    def _page(self) -> Page:
        pages = self._context.pages
        return pages[0] if pages else None

    async def _get_page(self) -> Page:
        pages = self._context.pages
        for page in list(pages):
            if not page.is_closed():
                return page
        return await self._context.new_page()

    async def is_logged_in(self) -> bool:
        page = await self._get_page()
        await page.goto(SUNO_CREATE, wait_until="domcontentloaded", timeout=30_000)
        await asyncio.sleep(2)
        return "signin" not in page.url and "login" not in page.url

    async def open_for_login(self):
        """Navigate to signin page and leave browser open for user interaction."""
        page = await self._get_page()
        await page.goto(SUNO_SIGNIN, wait_until="domcontentloaded", timeout=30_000)

    async def wait_for_login(self, timeout: float = 300) -> bool:
        """Wait until the user completes login. Returns True on success."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(3)
            pages = self._context.pages
            if not pages:
                continue
            url = pages[0].url
            if "suno.com" in url and "signin" not in url and "login" not in url:
                return True
        return False

    async def generate_song(
        self,
        lyrics: str,
        tags: str = "",
        title: str = "",
        make_instrumental: bool = False,
        negative_prompt: str = "",
    ) -> list[dict]:
        """Drive the Suno Advanced create UI and return list of generated song dicts.

        UI flow: Advanced mode → fill Lyrics (optional) + Styles (required) → Create.
        """
        page = await self._get_page()

        # Always navigate fresh so we start from a known state (Simple mode)
        try:
            await page.goto(SUNO_CREATE, wait_until="domcontentloaded", timeout=30_000)
        except Exception as e:
            if "Page crashed" in str(e) or "closed" in str(e).lower():
                logger.warning("Page crashed/closed, recovering with fresh page: %s", e)
                try:
                    await page.close()
                except Exception:
                    pass
                page = await self._context.new_page()
                await page.goto(SUNO_CREATE, wait_until="domcontentloaded", timeout=30_000)
            else:
                raise
        await asyncio.sleep(3)

        if "signin" in page.url or "login" in page.url:
            raise RuntimeError("Not logged in — call suno_login first.")

        # Dismiss cookie/consent banner via JS (more reliable than role-based click)
        dismissed = await page.evaluate("""() => {
            const labels = ['allow all', 'accept all', 'accept all cookies'];
            const btn = Array.from(document.querySelectorAll('button'))
                .find(b => labels.some(l => b.textContent.trim().toLowerCase().includes(l)));
            if (btn) { btn.click(); return btn.textContent.trim(); }
            return null;
        }""")
        if dismissed:
            await asyncio.sleep(1)

        # Switch to Advanced mode via JS click.
        # Advanced mode has a dedicated Lyrics textarea + a Styles textarea (2 textareas in the panel).
        # Simple mode has fewer or different textareas.
        n_ta = await page.locator('textarea').count()
        logger.info("Textareas before mode switch: %d", n_ta)
        if n_ta < 2:
            clicked = await page.evaluate("""() => {
                const btn = Array.from(document.querySelectorAll('button'))
                    .find(b => b.textContent.trim().toLowerCase() === 'advanced');
                if (btn) { btn.click(); return true; }
                return false;
            }""")
            logger.info("Advanced button JS click: %s", clicked)
            # Poll until 2 textareas appear (up to 5s)
            for _ in range(10):
                await asyncio.sleep(0.5)
                n_ta = await page.locator('textarea').count()
                if n_ta >= 2:
                    break
        logger.info("Textareas after mode switch: %d", n_ta)

        # Log aria-labels of all icon-only buttons to help debug magic wand selector
        wand_labels = await page.evaluate("""() => {
            return Array.from(document.querySelectorAll('button'))
                .filter(b => !b.textContent.trim())
                .map(b => b.getAttribute('aria-label') || b.getAttribute('title') || '(no label)')
                .filter(l => l !== '(no label)');
        }""")
        logger.info("Icon button aria-labels: %s", wand_labels)

        # Suno uses <textarea> elements (not div[contenteditable]).
        # Lyrics: placeholder "Write some lyrics or leave blank for instrumental"
        # Styles: placeholder contains style suggestions (changes dynamically)

        # React-controlled textareas require execCommand('insertText') to fire native
        # input events — plain fill() sets .value directly, bypassing React's event system.
        async def fill_textarea(locator, text: str) -> bool:
            if await locator.count() == 0:
                return False
            # Write text to clipboard, then paste — most reliable way to fill React textareas
            await page.evaluate("(t) => navigator.clipboard.writeText(t)", text)
            await locator.click()
            await page.keyboard.press("Control+a")
            await page.keyboard.press("Control+v")
            await asyncio.sleep(0.3)
            # Verify value was received
            val = await locator.evaluate("el => el.value")
            print(f"[FILL] expected {len(text)} chars, got {len(val)} chars", flush=True)
            return len(val) > 0

        # Screenshot after mode switch for debugging
        await page.screenshot(path="/data/debug_pre_fill.png")

        # Switch Lyrics Mode to Manual — check aria-pressed/data attrs to avoid toggling it off
        manual_state = await page.evaluate("""() => {
            const btns = Array.from(document.querySelectorAll('button'));
            const manual = btns.find(b => b.textContent.trim().toLowerCase() === 'manual');
            const auto   = btns.find(b => b.textContent.trim().toLowerCase() === 'auto');
            if (!manual) return 'not_found';
            const isActive = manual.getAttribute('aria-pressed') === 'true'
                || manual.getAttribute('data-state') === 'active'
                || manual.classList.contains('active')
                || manual.classList.contains('selected')
                || (auto && auto.getAttribute('aria-pressed') !== 'true');
            if (!isActive) manual.click();
            return isActive ? 'already_manual' : 'clicked_manual';
        }""")
        print(f"[LYRICS MODE] {manual_state}", flush=True)
        await asyncio.sleep(0.5)

        # --- Fill Lyrics ---
        if lyrics:
            filled = await fill_textarea(
                page.get_by_placeholder(re.compile(r"lyrics|leave blank", re.I)).first,
                lyrics
            )
            if not filled:
                # Fallback: first textarea
                await fill_textarea(page.locator('textarea').first, lyrics)
            await asyncio.sleep(0.5)

        # --- Fill Styles ---
        if tags:
            ta_count = await page.locator('textarea').count()
            if ta_count >= 2:
                filled = await fill_textarea(page.locator('textarea').nth(1), tags)
            else:
                # Try styles input as fallback
                filled = await fill_textarea(
                    page.get_by_placeholder(re.compile(r"style|genre|sound|describe", re.I)).first,
                    tags
                )
            logger.info("Styles filled (%s): %s", filled, tags[:40])
            await asyncio.sleep(1)
            await page.screenshot(path="/data/debug_post_fill.png")
            # Read back textarea values to confirm React saw them
            ta_values = await page.evaluate("""() =>
                Array.from(document.querySelectorAll('textarea'))
                    .map(t => t.value.slice(0, 60))
            """)
            logger.info("Textarea values after fill: %s", ta_values)
        else:
            # Click the magic wand to auto-generate styles from the lyrics
            wand_clicked = False
            for selector in [
                'button[aria-label="Personalized magic wand"]',
                'button[aria-label*="magic" i]',
                'button[aria-label*="wand" i]',
            ]:
                w = page.locator(selector)
                if await w.count() > 0:
                    await w.first.click()
                    logger.info("Clicked magic wand via: %s", selector)
                    wand_clicked = True
                    break
            if not wand_clicked:
                logger.warning("Magic wand not found — styles may be empty")
            await asyncio.sleep(4)

        await asyncio.sleep(1)

        # --- Optional fields ---
        if title:
            try:
                t = page.get_by_placeholder(re.compile(r"title", re.I)).first
                if await t.count() > 0:
                    await t.fill(title)
            except Exception:
                pass

        if make_instrumental:
            try:
                inst = page.get_by_role("checkbox", name=re.compile(r"instrumental", re.I))
                if await inst.count() > 0 and not await inst.is_checked():
                    await inst.click()
            except Exception:
                pass

        if negative_prompt:
            try:
                neg = page.get_by_placeholder(re.compile(r"exclude|negative|avoid", re.I)).first
                if await neg.count() > 0:
                    await neg.fill(negative_prompt)
            except Exception:
                pass

        # Final verification: confirm lyrics textarea still has content (VNC user might have cleared it)
        if lyrics:
            lyrics_ta = page.get_by_placeholder(re.compile(r"lyrics|leave blank", re.I)).first
            if await lyrics_ta.count() > 0:
                val = await lyrics_ta.evaluate("el => el.value")
                if len(val) < len(lyrics) // 2:
                    logger.warning("Lyrics textarea cleared/corrupted (%d chars), re-filling...", len(val))
                    await fill_textarea(lyrics_ta, lyrics)
                    await asyncio.sleep(0.5)
                else:
                    logger.info("Lyrics intact: %d chars", len(val))

        await page.screenshot(path="/data/debug_pre_create.png")

        # Intercept the generate API call to log what lyrics are actually being submitted
        captured_body: dict = {}
        async def on_request(request):
            if request.method == "POST" and "suno" in request.url:
                try:
                    body = request.post_data or ""
                    print(f"[SUNO POST] {request.url}\n{body[:400]}", flush=True)
                except Exception as e:
                    print(f"[SUNO POST err] {e}", flush=True)
        page.on("request", on_request)

        # Snapshot existing song IDs before clicking Create
        existing_ids: set[str] = set(await self._get_visible_song_ids(page))

        # Click the orange Create button (aria-label="Create song") at the bottom.
        create_loc = page.locator('button[aria-label="Create song"]')
        count = await create_loc.count()
        if count == 0:
            # Fallback: last button matching create/generate text
            create_loc = page.get_by_role("button", name=re.compile(r"create|generate", re.I))
            count = await create_loc.count()

        if count == 0:
            raise RuntimeError("Could not find Create button — make sure Lyrics or Styles have content.")

        create_btn = create_loc.last
        await create_btn.scroll_into_view_if_needed()
        # Poll until button is enabled (styles must finish generating first)
        for _ in range(30):
            if await create_btn.is_enabled():
                break
            await asyncio.sleep(1)
        else:
            raise RuntimeError("Create button never became enabled — styles may not have generated.")
        await create_btn.click()
        logger.info("Clicked Create button")

        # Wait for hCaptcha to be solved if it appears (up to 90s)
        for _ in range(90):
            captcha_present = await page.evaluate("""() =>
                !!document.querySelector('iframe[src*="hcaptcha"]') ||
                !!document.querySelector('[data-hcaptcha-widget-id]') ||
                !!document.querySelector('.hcaptcha-box') ||
                !!document.querySelector('#hcaptcha')
            """)
            if not captcha_present:
                break
            logger.info("hCaptcha detected — waiting for user to solve...")
            await asyncio.sleep(1)

        # Wait for new songs to appear (up to poll_timeout seconds)
        start = time.monotonic()
        new_songs = []
        while (time.monotonic() - start) < self.settings.poll_timeout:
            await asyncio.sleep(self.settings.poll_interval)
            current_ids = set(await self._get_visible_song_ids(page))
            fresh = current_ids - existing_ids
            if fresh:
                logger.info("New song IDs detected: %s", fresh)
                new_songs = [{"id": sid, "status": "streaming"} for sid in fresh]
                # Navigate away so VNC interaction can't corrupt the next run
                await asyncio.sleep(1)
                first_id = next(iter(fresh))
                await page.goto(f"https://suno.com/song/{first_id}", wait_until="domcontentloaded", timeout=30_000)
                break
        else:
            raise RuntimeError("Song generation timed out — no new songs appeared.")

        return new_songs

    async def _get_visible_song_ids(self, page: Page) -> list[str]:
        """Extract suno song IDs visible on the current page."""
        hrefs = await page.eval_on_selector_all(
            "a[href*='/song/']",
            "els => els.map(e => e.href)",
        )
        ids = []
        for href in hrefs:
            m = re.search(r"/song/([a-f0-9-]{36})", href)
            if m:
                ids.append(m.group(1))
        return ids

    async def get_song_url(self, song_id: str) -> str | None:
        """Return direct audio URL for a song by navigating to its page."""
        page = await self._get_page()
        await page.goto(f"https://suno.com/song/{song_id}", wait_until="domcontentloaded", timeout=30_000)
        await asyncio.sleep(3)
        # Try to find audio element src
        src = await page.eval_on_selector("audio", "el => el.src", timeout=5_000).catch(lambda _: None) if False else None
        try:
            src = await page.eval_on_selector("audio", "el => el.src")
        except Exception:
            src = None
        return src

    async def download_song(self, song_id: str, output_dir: Path | None = None) -> str:
        """Download a song's audio file. Polls CDN until full file is available."""
        import httpx

        directory = output_dir or self.settings.download_dir
        directory.mkdir(parents=True, exist_ok=True)

        filename = f"{song_id[:8]}.mp3"
        filepath = directory / filename

        cdn_url = f"https://cdn1.suno.ai/{song_id}.mp3"
        MIN_SIZE = 500_000  # 500KB — stubs are ~4.8KB, real songs are 2-5MB

        async with httpx.AsyncClient(follow_redirects=True, timeout=120.0) as dl:
            for attempt in range(24):  # up to ~2 minutes
                resp = await dl.get(cdn_url)
                if resp.status_code == 200 and len(resp.content) >= MIN_SIZE:
                    filepath.write_bytes(resp.content)
                    logger.info("Downloaded %s: %d bytes", song_id[:8], len(resp.content))
                    return str(filepath)
                logger.info(
                    "CDN returned %d bytes (attempt %d/24) — song still generating, retrying in 5s...",
                    len(resp.content) if resp.status_code == 200 else 0,
                    attempt + 1,
                )
                await asyncio.sleep(5)
            raise RuntimeError(f"Song {song_id} not ready after 2 minutes — try again later.")

