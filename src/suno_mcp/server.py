"""Suno MCP Server - Generate AI music via the Model Context Protocol."""

import json
import logging
import re
from typing import Literal

from starlette.staticfiles import StaticFiles

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.lifespan import lifespan

from .browser import SunoBrowser
from .config import Settings
from .rest import create_rest_app

logger = logging.getLogger(__name__)
rest_app = create_rest_app()


@lifespan
async def app_lifespan(server):
    settings = Settings()
    browser = SunoBrowser(settings)
    await browser.start()
    rest_app.state.settings = settings
    rest_app.state.browser = browser
    try:
        yield {"settings": settings, "browser": browser}
    finally:
        rest_app.state.browser = None
        await browser.stop()


mcp = FastMCP(
    name="Suno",
    instructions=(
        "MCP server for generating AI music with Suno via browser automation. "
        "Use suno_login first to authenticate (only needed once — session persists). "
        "Use generate_song, generate_speech, or generate_sound. "
        "If a CAPTCHA appears, solve it via noVNC and call get_generation_status, "
        "not generate_song again. Use upload_audio for audio you have permission to upload. "
        "Downloaded songs are served at /audio/{filename} on this server."
    ),
    lifespan=app_lifespan,
)


def _get_browser(ctx: Context) -> SunoBrowser:
    return ctx.lifespan_context["browser"]


@mcp.tool(
    description=(
        "Check login status and open browser for Google login if needed. "
        "Connect to the server's noVNC URL to complete login. "
        "Session is saved in the Chrome profile — only needed once."
    ),
)
async def suno_login(ctx: Context) -> str:
    browser = _get_browser(ctx)

    if await browser.begin_login():
        return "Already logged in to Suno."

    settings = ctx.lifespan_context["settings"]
    await ctx.info(f"Complete login via {settings.base_url}/vnc.html?autoconnect=1&resize=scale")

    if await browser.wait_for_login(timeout=300):
        return "Login successful! Session saved. You can now use generate_song."
    return "Login timed out. Please try again and complete login within 5 minutes."


@mcp.tool(
    description=(
        "Generate a new song with Suno AI by automating the create page. "
        "lyrics: the full song text (verses, chorus, bridge). "
        "tags: style/genre/mood descriptors e.g. 'trip hop, minor key, cinematic'. "
        "Returns confirmed song IDs, or captcha_required with a noVNC URL. "
        "reference_audio_id reuses an uploaded or existing song as a Cover or Extend. "
        "inspiration_ids accepts up to four existing tracks. "
        "dry_run fills the form without clicking Create or spending generation credits."
    ),
)
async def generate_song(
    lyrics: str = "",
    tags: str = "",
    title: str = "",
    make_instrumental: bool = False,
    negative_prompt: str = "",
    model: str = "",
    weirdness: int | None = None,
    style_influence: int | None = None,
    variety: int | None = None,
    vocal_gender: Literal["", "male", "female"] = "",
    duration_seconds: int | None = None,
    reference_audio_id: str = "",
    audio_mode: Literal["cover", "extend"] = "cover",
    audio_influence: int | None = None,
    inspiration_ids: list[str] | None = None,
    inspiration_playlist: str = "",
    dry_run: bool = False,
    ctx: Context = None,
) -> dict:
    browser = _get_browser(ctx)

    await ctx.info(f"Starting song generation: {(lyrics or tags)[:80]}...")
    try:
        result = await browser.generate_song(
            lyrics=lyrics,
            tags=tags,
            title=title,
            make_instrumental=make_instrumental,
            negative_prompt=negative_prompt,
            model=model,
            weirdness=weirdness,
            style_influence=style_influence,
            variety=variety,
            vocal_gender=vocal_gender,
            duration_seconds=duration_seconds,
            reference_audio_id=reference_audio_id,
            audio_mode=audio_mode,
            audio_influence=audio_influence,
            inspiration_ids=inspiration_ids,
            inspiration_playlist=inspiration_playlist,
            dry_run=dry_run,
        )
        if result["status"] == "captcha_required":
            await ctx.warning(result["message"] + " " + result["novnc_url"])
        if result["status"] == "error":
            raise ToolError(result["message"])
        return result
    except (RuntimeError, ValueError) as e:
        raise ToolError(str(e)) from e
    except Exception as e:
        logger.exception("Unexpected error during generation")
        raise ToolError(str(e)) from e


@mcp.tool(description="Check the current generation after solving a CAPTCHA; never submits another Create. Includes CAPTCHA frequency counters since server startup.")
async def get_generation_status(ctx: Context) -> dict:
    return await _get_browser(ctx).get_generation_status()


@mcp.tool(description="Abandon an unconfirmed browser wait only after a CAPTCHA was canceled or the attempt is stuck. Requires its exact attempt_id. This cannot cancel an accepted Suno request or refund credits; check your library before retrying.")
async def abandon_generation_wait(attempt_id: str, ctx: Context) -> dict:
    try:
        return await _get_browser(ctx).abandon_generation_wait(attempt_id)
    except (RuntimeError, ValueError) as error:
        raise ToolError(str(error)) from error


@mcp.tool(description="Generate spoken audio in Suno's Speech tab. Configure the script, delivery tone, background music, vocal gender, and variety (0-4). dry_run prepares without spending generation credits.")
async def generate_speech(
    script: str, tone: str = "", background_music: bool = False,
    vocal_gender: Literal["", "male", "female"] = "", variety: int | None = None,
    dry_run: bool = False, ctx: Context = None,
) -> dict:
    try:
        result = await _get_browser(ctx).generate_speech(script, tone, background_music, vocal_gender, variety, dry_run)
        if result["status"] == "captcha_required":
            await ctx.warning(result["message"] + " " + result["novnc_url"])
        if result["status"] == "error":
            raise ToolError(result["message"])
        return result
    except Exception as error:
        raise ToolError(str(error)) from error


@mcp.tool(description="Generate a sound effect or loop in Suno's Sounds tab. prompt: up to 500 characters. sound_type: one_shot or loop. bpm: optional tempo (1-300). dry_run prepares without spending generation credits.")
async def generate_sound(prompt: str, sound_type: Literal["one_shot", "loop"] = "one_shot", bpm: int | None = None, dry_run: bool = False, ctx: Context = None) -> dict:
    try:
        result = await _get_browser(ctx).generate_sound(prompt, sound_type, bpm, dry_run)
        if result["status"] == "captcha_required":
            await ctx.warning(result["message"] + " " + result["novnc_url"])
        if result["status"] == "error":
            raise ToolError(result["message"])
        return result
    except Exception as error:
        raise ToolError(str(error)) from error


@mcp.tool(description="Inspect a song's actual status and audio URL without guessing a CDN path.")
async def get_song(song_id: str, ctx: Context) -> dict:
    try:
        return await _get_browser(ctx).get_song(song_id)
    except (RuntimeError, ValueError) as error:
        raise ToolError(str(error)) from error


@mcp.tool(description="Upload an audio file from /data/uploads or /downloads for reuse as a reference. confirm_rights must explicitly be true.")
async def upload_audio(
    audio_path: str, confirm_rights: bool = False, description: str = "",
    audio_type: Literal["Full Song", "Song Demo", "Voice", "Instrument Stem", "Loop", "Rhythm / Percussion", "Sample", "Spoken Word", "Sound Effect", "Field Recording / Ambience"] = "Song Demo",
    ctx: Context = None,
) -> dict:
    try:
        return await _get_browser(ctx).upload_audio(audio_path, confirm_rights, description, audio_type)
    except (RuntimeError, ValueError) as error:
        raise ToolError(str(error)) from error


@mcp.tool(
    description=(
        "Save a completed track's decoded audio from the browser player. "
        "Preserves the served format (for example M4A); does not use Suno's export or purchase dialogs."
    ),
)
async def download_song(song_id: str, ctx: Context = None) -> str:
    browser = _get_browser(ctx)
    await ctx.info(f"Downloading song {song_id}...")
    try:
        path = await browser.download_song(song_id)
        filename = path.split("/")[-1]
        settings = ctx.lifespan_context["settings"]
        return f"Downloaded to: {path}\nStream URL: {settings.base_url}/audio/{filename}"
    except RuntimeError as e:
        raise ToolError(str(e)) from e
    except Exception as e:
        logger.exception("Unexpected error during download")
        raise ToolError(str(e)) from e


@mcp.tool(description="Check a pending audio upload without uploading the file again.")
async def get_upload_status(ctx: Context) -> dict:
    return _get_browser(ctx).get_upload_status()


@mcp.tool(description="Take a screenshot and return detailed page state for debugging.")
async def debug_browser(
    ctx: Context,
    panel: Literal["current", "music", "speech", "sound", "audio", "inspiration", "models", "library"] = "current",
    selection: str = "",
    song_id: str = "",
) -> str:
    browser = _get_browser(ctx)
    if panel != "current" or selection or song_id:
        browser._guard_pending()
    async with browser._operations.run("debug_browser"):
        if panel != "current" or selection or song_id:
            browser._guard_pending()
        return await _debug_browser(browser, panel, selection, song_id)


async def _debug_browser(browser: SunoBrowser, panel: str, selection: str, song_id: str = "") -> str:
    page = await browser._get_page()
    if song_id:
        await page.goto(f"https://suno.com/song/{browser._song_id(song_id)}", wait_until="domcontentloaded", timeout=30_000)
    if panel != "current":
        browser._guard_pending()
        await page.keyboard.press("Escape")
    if panel == "library":
        await page.goto("https://suno.com/me", wait_until="domcontentloaded")
        await page.wait_for_timeout(1500)
    elif panel != "current":
        if "/create" not in page.url:
            await page.goto("https://suno.com/create", wait_until="domcontentloaded")
        await browser._select_tab(page, {"speech": "Speech", "sound": "Sounds"}.get(panel, "Songs"))
        if panel not in {"speech", "sound"}:
            await browser._select_tab(page, "Advanced")
        if panel in {"audio", "inspiration"}:
            await browser._reset_form(page)
    if panel == "audio":
        await page.get_by_role("button", name=re.compile("audio", re.I)).first.click()
        await page.wait_for_timeout(500)
    elif panel == "inspiration":
        await page.get_by_role("button", name=re.compile("inspiration", re.I)).first.click(timeout=10_000)
        await page.wait_for_timeout(500)
    elif panel == "models":
        await page.get_by_role("button", name=re.compile(r"^v\d")).first.click()
        await page.wait_for_timeout(500)
    if selection:
        if panel == "audio":
            await page.get_by_role("button", name=selection, exact=True).click(timeout=10_000)
        elif panel == "inspiration":
            await page.get_by_role("dialog").get_by_text(selection, exact=False).last.click(timeout=10_000)
        elif panel == "library":
            await page.get_by_role("tab", name=selection, exact=True).click(timeout=10_000)
        elif panel == "current" and selection in {"Add to Playlist", "Remix", "New Playlist", "Create Playlist", "Play", "Pause", "Playbar: Play button", "Playbar: Pause button", "More options", "Download"}:
            button = page.get_by_role("button", name=selection, exact=True)
            if not await button.count():
                button = page.get_by_role("menuitem", name=selection, exact=True)
            await button.first.click(timeout=10_000)
        await page.wait_for_timeout(500)
    url = page.url
    await page.screenshot(path="/data/debug.png")

    btn_texts = await page.eval_on_selector_all(
        "button", "els => els.map(e => e.innerText.trim()).filter(t => t)"
    )
    icon_labels = await page.evaluate("""() =>
        Array.from(document.querySelectorAll('button'))
            .filter(b => !b.textContent.trim())
            .map(b => b.getAttribute('aria-label') || b.getAttribute('title') || '(none)')
    """)
    form_info = await page.evaluate("""() => {
        const ce = Array.from(document.querySelectorAll('[contenteditable]'));
        const ta = Array.from(document.querySelectorAll('textarea'));
        const inp = Array.from(document.querySelectorAll('input:not([type=hidden])'));
        return {
            contenteditable: ce.map(e => ({html: e.outerHTML.slice(0, 600), text: e.innerText, visible: !!e.getClientRects().length})),
            textareas: ta.map(e => ({placeholder: e.placeholder, value: e.value, visible: !!e.getClientRects().length})),
            inputs: inp.map(e => ({html: e.outerHTML.slice(0, 600), visible: !!e.getClientRects().length})),
            controls: Array.from(document.querySelectorAll('[role=slider], [role=checkbox], [role=switch], [role=combobox]')).map(e => e.outerHTML.slice(0, 800)),
            frames: Array.from(document.querySelectorAll('iframe')).map(e => ({src: e.src, visible: !!e.getClientRects().length})),
            text: document.body.innerText.slice(0, 18000),
            fieldParents: ta.concat(inp).filter(e => e.placeholder).map(e => ({placeholder: e.placeholder, rect: e.getBoundingClientRect().toJSON(), parent: e.parentElement.outerHTML.slice(0, 1800)})),
            textParents: Array.from(document.querySelectorAll('div, span, a, p')).filter(e => e.childElementCount === 0 && e.textContent.includes('Create New Playlist')).map(e => e.parentElement.outerHTML.slice(0, 4000)),
            audio: Array.from(document.querySelectorAll('audio')).map(element => ({
                src: element.src, currentSrc: element.currentSrc,
                currentTime: element.currentTime, duration: Number.isFinite(element.duration) ? element.duration : null,
                paused: element.paused, readyState: element.readyState, networkState: element.networkState,
                error: element.error ? {code: element.error.code, message: element.error.message} : null,
                buffered: Array.from({length: element.buffered.length}, (_, index) => [element.buffered.start(index), element.buffered.end(index)])
            })),
            pageData: (() => {
                const flight = (window.__next_f || []).filter(entry => typeof entry[1] === 'string').map(entry => entry[1]).join('');
                return {initialKeys: Object.keys(window.__NEXT_DATA__ || {}), flightLength: flight.length,
                    flightScriptCount: Array.from(document.scripts).filter(script => script.textContent.includes('__next_f')).length};
            })(),
        };
    }""")
    return (
        f"Browser: {browser._context.browser.version}\n"
        f"User agent: {await page.evaluate('navigator.userAgent')}\n"
        f"API events: {json.dumps(browser._api_events)}\n"
        f"Media events: {json.dumps(browser._media_events)}\n"
        f"Browser errors: {json.dumps(browser._browser_errors)}\n"
        f"Cached tracks: {json.dumps([browser._clip_summary(clip) for clip in list(browser._clips.values())[:12]])}\n"
        f"URL: {url}\n"
        f"Form elements: {json.dumps(form_info)}\n"
        f"Icon button labels: {icon_labels}\n"
        f"Button texts: {btn_texts}"
        f"\nAccessible page: {(await page.locator('body').aria_snapshot())[:14000]}"
        f"\nAccessible overlays: {(await page.locator('body').aria_snapshot())[-6000:]}"
    )


def create_http_app():
    settings = Settings()
    http_app = mcp.http_app(path="/")
    settings.download_dir.mkdir(parents=True, exist_ok=True)
    http_app.mount("/api", rest_app, name="rest")
    http_app.mount("/audio", StaticFiles(directory=str(settings.download_dir)), name="audio")
    http_app.mount("/", StaticFiles(directory=str(settings.download_dir)), name="audio_legacy")
    return http_app


if __name__ == "__main__":
    settings = Settings()
    import uvicorn
    uvicorn.run(create_http_app(), host=settings.host, port=settings.port)
