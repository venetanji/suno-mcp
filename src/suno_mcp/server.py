"""Suno MCP Server - Generate AI music via the Model Context Protocol."""

import logging

from starlette.staticfiles import StaticFiles

from fastmcp import Context, FastMCP
from fastmcp.server.lifespan import lifespan

from .browser import SunoBrowser
from .config import Settings

logger = logging.getLogger(__name__)


@lifespan
async def app_lifespan(server):
    settings = Settings()
    browser = SunoBrowser(settings)
    await browser.start()
    try:
        yield {"settings": settings, "browser": browser}
    finally:
        await browser.stop()


mcp = FastMCP(
    name="Suno",
    instructions=(
        "MCP server for generating AI music with Suno via browser automation. "
        "Use suno_login first to authenticate (only needed once — session persists). "
        "Then use generate_song to create music from text prompts. "
        "Downloaded songs are served at /audio/{filename} on this server."
    ),
    lifespan=app_lifespan,
)


def _get_browser(ctx: Context) -> SunoBrowser:
    return ctx.lifespan_context["browser"]


@mcp.tool(
    description=(
        "Check login status and open browser for Google login if needed. "
        "Connect to noVNC at http://<host>:6080 to complete login. "
        "Session is saved in the Chrome profile — only needed once."
    ),
)
async def suno_login(ctx: Context) -> str:
    browser = _get_browser(ctx)

    if await browser.is_logged_in():
        return "Already logged in to Suno."

    await browser.open_for_login()
    await ctx.info("Browser opened. Please complete Google login via noVNC at http://<host>:6080")

    if await browser.wait_for_login(timeout=300):
        return "Login successful! Session saved. You can now use generate_song."
    return "Login timed out. Please try again and complete login within 5 minutes."


@mcp.tool(
    description=(
        "Generate a new song with Suno AI by automating the create page. "
        "lyrics: the full song text (verses, chorus, bridge). "
        "tags: style/genre/mood descriptors e.g. 'trip hop, minor key, cinematic'. "
        "Returns song IDs once generation starts."
    ),
)
async def generate_song(
    lyrics: str = "",
    tags: str = "",
    title: str = "",
    make_instrumental: bool = False,
    negative_prompt: str = "",
    ctx: Context = None,
) -> str:
    browser = _get_browser(ctx)

    await ctx.info(f"Starting song generation: {(lyrics or tags)[:80]}...")
    try:
        songs = await browser.generate_song(
            lyrics=lyrics,
            tags=tags,
            title=title,
            make_instrumental=make_instrumental,
            negative_prompt=negative_prompt,
        )
        lines = ["Song generation started!\n"]
        for s in songs:
            sid = s["id"]
            lines.append(f"ID: {sid}")
            lines.append(f"Preview: https://suno.com/song/{sid}")
            lines.append(f"Download: https://cdn1.suno.ai/{sid}.mp3")
            lines.append("")
        return "\n".join(lines)
    except RuntimeError as e:
        return f"Error: {e}"
    except Exception as e:
        logger.exception("Unexpected error during generation")
        return f"Unexpected error: {e}"


@mcp.tool(
    description="Download a song's audio file by its ID.",
)
async def download_song(song_id: str, ctx: Context = None) -> str:
    browser = _get_browser(ctx)
    await ctx.info(f"Downloading song {song_id}...")
    try:
        path = await browser.download_song(song_id)
        filename = path.split("/")[-1]
        settings = ctx.lifespan_context["settings"]
        base = f"http://{settings.host}:{settings.port}"
        return f"Downloaded to: {path}\nLocal URL: {base}/audio/{filename}"
    except RuntimeError as e:
        return f"Error: {e}"
    except Exception as e:
        logger.exception("Unexpected error during download")
        return f"Download failed: {e}"


@mcp.tool(description="Take a screenshot and return detailed page state for debugging.")
async def debug_browser(ctx: Context) -> str:
    browser = _get_browser(ctx)
    page = await browser._get_page()
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
            contenteditable: ce.map(e => ({tag: e.tagName, val: e.getAttribute('contenteditable'), text: e.innerText.slice(0,40)})),
            textareas: ta.map(e => e.value.slice(0,40) || e.placeholder),
            inputs: inp.map(e => ({type: e.type, placeholder: e.placeholder, val: e.value.slice(0,20)})),
        };
    }""")
    return (
        f"URL: {url}\n"
        f"Form elements: {form_info}\n"
        f"Icon button labels: {icon_labels}\n"
        f"Button texts: {btn_texts}"
    )


if __name__ == "__main__":
    settings = Settings()
    # Mount /downloads as a static file server at /audio/
    http_app = mcp.http_app(path="/mcp")
    settings.download_dir.mkdir(parents=True, exist_ok=True)
    http_app.mount("/audio", StaticFiles(directory=str(settings.download_dir)), name="audio")
    import uvicorn
    uvicorn.run(http_app, host=settings.host, port=settings.port)
