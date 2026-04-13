"""FastMCP server for Suno music generation."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Annotated, Any, AsyncIterator

from fastmcp import Context, FastMCP

from suno_mcp.browser import SunoBrowser

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lifespan – start / stop the browser once per server run
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[dict[str, Any]]:  # type: ignore[type-arg]
    """Manage the Playwright browser lifecycle for the duration of the server."""
    browser = SunoBrowser(headless=True)
    await browser.start()
    logger.info("Suno browser started")
    try:
        yield {"browser": browser}
    finally:
        await browser.close()
        logger.info("Suno browser stopped")


# ---------------------------------------------------------------------------
# Server definition
# ---------------------------------------------------------------------------

mcp = FastMCP(
    name="suno-mcp",
    instructions=(
        "A server for generating music with Suno. "
        "Use `authenticate` first if you haven't logged in yet, "
        "then use `generate_song` to create music."
    ),
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool
async def authenticate(
    ctx: Context,
    timeout_seconds: Annotated[int, "Maximum seconds to wait for the user to log in"] = 300,
) -> str:
    """
    Open a browser window so the user can log in to Suno.

    This must be called before generating songs if no valid session exists.
    The browser will open for the user to complete the login flow (supports
    Google, Discord, Apple and email). The session is then saved locally so
    subsequent calls don't require re-authentication.
    """
    browser: SunoBrowser = ctx.lifespan_context["browser"]
    await ctx.info("Opening browser for authentication …")
    result = await browser.authenticate(timeout_seconds=timeout_seconds)
    await ctx.info(result)
    return result


@mcp.tool
async def check_auth_status(ctx: Context) -> dict[str, Any]:
    """
    Check whether the current session is authenticated with Suno.

    Returns a dict with `authenticated` (bool) and an optional `message`.
    """
    browser: SunoBrowser = ctx.lifespan_context["browser"]
    authenticated = await browser.is_authenticated()
    return {
        "authenticated": authenticated,
        "message": (
            "Logged in to Suno."
            if authenticated
            else "Not logged in. Call `authenticate` to start the login flow."
        ),
    }


@mcp.tool
async def generate_song(
    ctx: Context,
    prompt: Annotated[str, "A description of the song to generate"],
    custom_mode: Annotated[
        bool,
        "Enable custom mode to provide explicit lyrics, style, and title",
    ] = False,
    lyrics: Annotated[
        str | None,
        "Song lyrics (used in custom mode; falls back to prompt if omitted)",
    ] = None,
    style: Annotated[
        str | None,
        "Musical style or genre (e.g. 'lo-fi hip hop', 'classical piano')",
    ] = None,
    title: Annotated[str | None, "Song title (optional, custom mode only)"] = None,
    instrumental: Annotated[bool, "Generate an instrumental track (no vocals)"] = False,
) -> list[dict[str, Any]]:
    """
    Generate one or more songs on Suno using the provided description or lyrics.

    In normal mode a short description (`prompt`) is enough; Suno will create
    the lyrics and style automatically.  In custom mode you can control every
    aspect of the generation.

    Returns a list of generated song objects.  Each object contains at least:
    - `id`: unique song identifier
    - `title`: song title
    - `status`: current status (`complete`, `streaming`, `error`, …)
    - `audio_url`: direct URL to the audio file (may be empty while generating)
    - `image_url`: URL of the song's cover art
    """
    browser: SunoBrowser = ctx.lifespan_context["browser"]

    await ctx.info(f"Generating song: {prompt!r} …")
    songs = await browser.generate_song(
        prompt=prompt,
        custom_mode=custom_mode,
        lyrics=lyrics,
        style=style,
        title=title,
        instrumental=instrumental,
    )
    await ctx.info(f"Generated {len(songs)} song(s).")
    return songs


@mcp.tool
async def get_song_info(
    ctx: Context,
    song_id: Annotated[str, "The Suno song ID"],
) -> dict[str, Any]:
    """
    Retrieve metadata for a specific Suno song.

    Returns a dict with available metadata (title, audio_url, image_url,
    status, tags, etc.).  Some fields may be absent depending on whether the
    song has finished generating.
    """
    browser: SunoBrowser = ctx.lifespan_context["browser"]
    await ctx.info(f"Fetching info for song {song_id} …")
    return await browser.get_song_page_info(song_id)


@mcp.tool
async def get_songs(
    ctx: Context,
    page_size: Annotated[int, "Maximum number of songs to return (1–50)"] = 20,
) -> list[dict[str, Any]]:
    """
    Return the most recently generated songs from your Suno feed.

    Requires an authenticated session.  Returns up to `page_size` song objects
    with id, title, audio_url, image_url, and status fields.
    """
    if not 1 <= page_size <= 50:
        raise ValueError("page_size must be between 1 and 50")

    browser: SunoBrowser = ctx.lifespan_context["browser"]
    await ctx.info(f"Fetching up to {page_size} songs from feed …")
    return await browser.get_feed(page_size=page_size)


@mcp.tool
async def get_songs_by_ids(
    ctx: Context,
    song_ids: Annotated[list[str], "List of Suno song IDs to fetch"],
) -> list[dict[str, Any]]:
    """
    Fetch metadata for a specific list of song IDs.

    Useful for polling the status of songs returned by `generate_song`.
    """
    if not song_ids:
        raise ValueError("song_ids must not be empty")

    browser: SunoBrowser = ctx.lifespan_context["browser"]
    await ctx.info(f"Fetching info for {len(song_ids)} song(s) …")
    return await browser.get_songs_by_ids(song_ids)


@mcp.tool
async def clear_session(ctx: Context) -> str:
    """
    Delete the saved Suno authentication session.

    After calling this you will need to authenticate again before generating
    music.
    """
    browser: SunoBrowser = ctx.lifespan_context["browser"]
    browser.clear_session()
    return "Session cleared. Call `authenticate` to log in again."


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the server using stdio transport (default for MCP clients)."""
    logging.basicConfig(level=logging.INFO)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
