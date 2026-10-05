"""Configuration management via environment variables."""

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_prefix": "SUNO_"}

    host: str = "0.0.0.0"
    port: int = 8085
    # Public base URL returned in tool responses (e.g. https://my-host.ts.net).
    # Falls back to http://<host>:<port> if not set.
    public_url: str = ""
    download_dir: Path = Path("/downloads")
    auth_dir: Path = Path("/data")
    poll_interval: float = 5.0
    poll_timeout: float = 300.0
    display: str = ":99"
    browser_channel: str = "chrome"
    clerk_js_version: str = "5.61.0"
    rest_api_token: str = ""
    rest_upload_max_bytes: int = Field(default=100 * 1024 * 1024, gt=0)
    browser_queue_capacity: int = Field(default=8, ge=0)
    browser_queue_timeout: float = Field(default=15.0, gt=0)
    browser_operation_timeout: float = Field(default=120.0, gt=0)

    @property
    def base_url(self) -> str:
        if self.public_url:
            return self.public_url.rstrip("/")
        return f"http://{self.host}:{self.port}"

    @property
    def auth_file(self) -> Path:
        return self.auth_dir / "auth.json"
