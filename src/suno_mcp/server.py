"""Suno MCP Server - Generate AI music via the Model Context Protocol."""

import logging

from fastmcp import Context, FastMCP
from fastmcp.server.lifespan import lifespan

from .api import SunoAPI, SunoAPIError
from .auth import AuthenticationError, SunoAuth
from .config import Settings

logger = logging.getLogger(__name__)


@lifespan
async def app_lifespan(server):
    settings = Settings()
    auth = SunoAuth(settings)
    api = SunoAPI(auth, settings)
    try:
        yield {"settings": settings, "auth": auth, "api": api}
    finally:
        await api.close()


mcp = FastMCP(
    name="Suno",
    instructions=(
        "MCP server for generating AI music with Suno. "
        "Use the suno_login tool first to authenticate with your Google account. "
        "Then use generate_song to create music from text prompts."
    ),
    lifespan=app_lifespan,
)


def _get_deps(ctx: Context) -> tuple[Settings, SunoAuth, SunoAPI]:
    lc = ctx.lifespan_context
    return lc["settings"], lc["auth"], lc["api"]


def _format_clip(clip: dict) -> str:
    song_id = clip.get("id", "unknown")
    title = clip.get("title", "Untitled")
    status = clip.get("status", "unknown")
    audio_url = clip.get("audio_url", "")
    tags = clip.get("metadata", {}).get("tags", "") if isinstance(clip.get("metadata"), dict) else ""
    created = clip.get("created_at", "")

    lines = [
        f"Title: {title}",
        f"ID: {song_id}",
        f"Status: {status}",
        f"Suno URL: https://suno.com/song/{song_id}",
    ]
    if audio_url:
        lines.append(f"Audio URL: {audio_url}")
    if tags:
        lines.append(f"Style: {tags}")
    if created:
        lines.append(f"Created: {created}")
    return "\n".join(lines)


@mcp.tool(
    description=(
        "Log in to Suno with your Google account. Opens a Chromium browser "
        "on the container's virtual display. Connect to noVNC at "
        "http://<host>:6080 to complete the Google OAuth login. "
        "Only needed once per session - credentials are persisted."
    ),
)
async def suno_login(ctx: Context) -> str:
    """Authenticate with Suno via Google OAuth in a browser."""
    _, auth, _ = _get_deps(ctx)

    if auth.is_authenticated():
        try:
            await auth.get_token()
            return "Already authenticated with Suno. Session is valid."
        except AuthenticationError:
            pass

    try:
        await ctx.info("Opening browser for Google login...")
        await ctx.info(
            "Please connect to noVNC at http://<host>:6080 "
            "and complete the Google login in the browser window."
        )
        await auth.login_with_browser()
        return (
            "Login successful! Session has been saved. "
            "You can now use generate_song, get_song, list_songs, and download_song."
        )
    except AuthenticationError as exc:
        return f"Login failed: {exc}"
    except Exception as exc:
        logger.exception("Unexpected error during login")
        return f"Login failed with unexpected error: {exc}"


@mcp.tool(
    description=(
        "Generate a new song with Suno AI. Provide a text description of the "
        "song you want, or set is_custom=true to provide your own lyrics. "
        "Returns song details with Suno links once generation is complete. "
        "Each request generates 2 song variations."
    ),
)
async def generate_song(
    prompt: str,
    is_custom: bool = False,
    tags: str = "",
    title: str = "",
    make_instrumental: bool = False,
    wait_for_completion: bool = True,
    ctx: Context = None,
) -> str:
    """Generate a song from a text prompt."""
    _, auth, api = _get_deps(ctx)

    if not auth.is_authenticated():
        return "Error: Not authenticated. Please use suno_login first."

    try:
        await ctx.info(f"Starting song generation: {prompt[:80]}...")
        clips = await api.generate(
            prompt=prompt,
            is_custom=is_custom,
            tags=tags,
            title=title,
            make_instrumental=make_instrumental,
        )

        song_ids = [c["id"] for c in clips]
        await ctx.info(f"Generation started. Song IDs: {', '.join(song_ids)}")

        if not wait_for_completion:
            lines = ["Song generation started (not waiting for completion):\n"]
            for clip in clips:
                lines.append(_format_clip(clip))
                lines.append("")
            return "\n".join(lines)

        async def on_progress(elapsed, total):
            await ctx.info(f"Generating... ({int(elapsed)}s elapsed)")

        completed = await api.wait_for_songs(song_ids, on_progress=on_progress)

        lines = ["Song generation complete!\n"]
        for clip in completed:
            lines.append(_format_clip(clip))
            lines.append("")
        return "\n".join(lines)

    except AuthenticationError as exc:
        return f"Authentication error: {exc}. Try suno_login again."
    except SunoAPIError as exc:
        return f"Generation error: {exc.message}"
    except Exception as exc:
        logger.exception("Unexpected error during generation")
        return f"Unexpected error: {exc}"


@mcp.tool(
    description="Get details and status of a song by its ID.",
    annotations={"readOnlyHint": True},
)
async def get_song(song_id: str, ctx: Context = None) -> str:
    """Retrieve song details including status, URLs, and metadata."""
    _, auth, api = _get_deps(ctx)

    if not auth.is_authenticated():
        return "Error: Not authenticated. Please use suno_login first."

    try:
        clips = await api.get_songs([song_id])
        if not clips:
            return f"Song {song_id} not found."
        return _format_clip(clips[0])
    except AuthenticationError as exc:
        return f"Authentication error: {exc}. Try suno_login again."
    except SunoAPIError as exc:
        return f"Error fetching song: {exc.message}"


@mcp.tool(
    description="List your recent songs from Suno.",
    annotations={"readOnlyHint": True},
)
async def list_songs(page: int = 0, ctx: Context = None) -> str:
    """List recent songs with their IDs, titles, and statuses."""
    _, auth, api = _get_deps(ctx)

    if not auth.is_authenticated():
        return "Error: Not authenticated. Please use suno_login first."

    try:
        clips = await api.get_feed(page=page)
        if not clips:
            return "No songs found."

        lines = [f"Recent songs (page {page}):\n"]
        for clip in clips:
            lines.append(_format_clip(clip))
            lines.append("---")
        return "\n".join(lines)
    except AuthenticationError as exc:
        return f"Authentication error: {exc}. Try suno_login again."
    except SunoAPIError as exc:
        return f"Error listing songs: {exc.message}"


@mcp.tool(
    description=(
        "Download a song's MP3 file to the configured download directory. "
        "The song must have finished generating (status: complete)."
    ),
)
async def download_song(
    song_id: str,
    ctx: Context = None,
) -> str:
    """Download a completed song as an MP3 file."""
    settings, auth, api = _get_deps(ctx)

    if not auth.is_authenticated():
        return "Error: Not authenticated. Please use suno_login first."

    try:
        await ctx.info(f"Downloading song {song_id}...")
        filepath = await api.download_mp3(song_id)
        return f"Song downloaded successfully to: {filepath}"
    except AuthenticationError as exc:
        return f"Authentication error: {exc}. Try suno_login again."
    except SunoAPIError as exc:
        return f"Download error: {exc.message}"
    except Exception as exc:
        logger.exception("Unexpected error during download")
        return f"Download failed: {exc}"


if __name__ == "__main__":
    mcp.run()
