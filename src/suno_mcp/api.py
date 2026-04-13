"""Suno API client - async HTTP wrapper for Suno's internal API."""

import asyncio
import logging
import random
import re
from pathlib import Path

import httpx

from .auth import AuthenticationError, SunoAuth
from .config import Settings

logger = logging.getLogger(__name__)

BASE_URL = "https://studio-api.suno.ai"


class SunoAPIError(Exception):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class SunoAPI:
    def __init__(self, auth: SunoAuth, settings: Settings) -> None:
        self.auth = auth
        self.settings = settings
        self.http = httpx.AsyncClient(
            base_url=BASE_URL,
            timeout=30.0,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
                ),
                "Referer": "https://suno.com/",
                "Origin": "https://suno.com",
            },
        )

    async def _request(
        self,
        method: str,
        path: str,
        retry_auth: bool = True,
        **kwargs,
    ) -> dict:
        headers = await self.auth.get_auth_headers()
        resp = await self.http.request(method, path, headers=headers, **kwargs)

        if resp.status_code == 401 and retry_auth:
            await self.auth._refresh_jwt()
            headers = await self.auth.get_auth_headers()
            resp = await self.http.request(method, path, headers=headers, **kwargs)

        if resp.status_code >= 400:
            detail = ""
            try:
                detail = resp.json().get("detail", resp.text)
            except Exception:
                detail = resp.text
            raise SunoAPIError(
                f"Suno API error ({resp.status_code}): {detail}",
                status_code=resp.status_code,
            )

        return resp.json()

    async def generate(
        self,
        prompt: str,
        is_custom: bool = False,
        tags: str = "",
        title: str = "",
        make_instrumental: bool = False,
    ) -> list[dict]:
        """Start song generation. Returns list of clip dicts with IDs."""
        if is_custom:
            payload = {
                "prompt": prompt,
                "tags": tags,
                "title": title,
                "make_instrumental": make_instrumental,
                "mv": "chirp-v4",
            }
        else:
            payload = {
                "gpt_description_prompt": prompt,
                "make_instrumental": make_instrumental,
                "mv": "chirp-v4",
            }

        data = await self._request("POST", "/api/generate/v2/", json=payload)
        clips = data.get("clips", [])
        if not clips:
            raise SunoAPIError("No clips returned from generation request.")
        return clips

    async def get_songs(self, song_ids: list[str]) -> list[dict]:
        """Get song details by IDs."""
        ids_param = ",".join(song_ids)
        data = await self._request("GET", f"/api/feed/?ids={ids_param}")
        if isinstance(data, list):
            return data
        return data.get("clips", data.get("results", []))

    async def get_feed(self, page: int = 0) -> list[dict]:
        """Get recent songs feed."""
        data = await self._request("GET", f"/api/feed/?page={page}")
        if isinstance(data, list):
            return data
        return data.get("clips", data.get("results", []))

    async def get_credits(self) -> dict:
        """Get billing/credits info."""
        return await self._request("GET", "/api/billing/info/")

    async def wait_for_songs(
        self,
        song_ids: list[str],
        on_progress=None,
    ) -> list[dict]:
        """Poll until all songs are complete or errored."""
        start = asyncio.get_event_loop().time()
        timeout = self.settings.poll_timeout
        interval = self.settings.poll_interval

        while (asyncio.get_event_loop().time() - start) < timeout:
            clips = await self.get_songs(song_ids)

            statuses = [c.get("status", "") for c in clips]
            all_done = all(s in ("streaming", "complete") for s in statuses)
            any_error = any(s == "error" for s in statuses)

            if any_error:
                raise SunoAPIError("One or more songs failed to generate.")

            if all_done:
                return clips

            elapsed = asyncio.get_event_loop().time() - start
            if on_progress:
                await on_progress(elapsed, timeout)

            await asyncio.sleep(interval + random.uniform(0, 2))

        raise SunoAPIError(
            f"Song generation timed out after {timeout}s. "
            f"Song IDs: {', '.join(song_ids)} - check status with get_song."
        )

    async def download_mp3(self, song_id: str, output_dir: Path | None = None) -> str:
        """Download a song's MP3 to the filesystem. Returns the file path."""
        clips = await self.get_songs([song_id])
        if not clips:
            raise SunoAPIError(f"Song {song_id} not found.")

        clip = clips[0]
        audio_url = clip.get("audio_url")
        if not audio_url:
            raise SunoAPIError(
                f"Song {song_id} has no audio URL. Status: {clip.get('status')}"
            )

        directory = output_dir or self.settings.download_dir
        directory.mkdir(parents=True, exist_ok=True)

        title = clip.get("title", "untitled")
        safe_title = re.sub(r'[^\w\s-]', '', title).strip().replace(' ', '_')
        filename = f"{safe_title}_{song_id[:8]}.mp3"
        filepath = directory / filename

        async with httpx.AsyncClient(follow_redirects=True, timeout=120.0) as dl:
            resp = await dl.get(audio_url)
            resp.raise_for_status()
            filepath.write_bytes(resp.content)

        return str(filepath)

    async def close(self) -> None:
        await self.http.aclose()
