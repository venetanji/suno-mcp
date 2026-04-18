"""Configuration management via environment variables."""

from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_prefix": "SUNO_"}

    host: str = "0.0.0.0"
    port: int = 8085
    download_dir: Path = Path("/downloads")
    auth_dir: Path = Path("/data")
    poll_interval: float = 5.0
    poll_timeout: float = 300.0
    display: str = ":99"
    clerk_js_version: str = "5.61.0"

    @property
    def auth_file(self) -> Path:
        return self.auth_dir / "auth.json"
