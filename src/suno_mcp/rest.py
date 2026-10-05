"""REST endpoints sharing the MCP server's authenticated browser."""

import asyncio
import logging
import secrets
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from playwright.async_api import TimeoutError as BrowserTimeoutError
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.staticfiles import NotModifiedResponse, StaticFiles

from .browser import SunoBrowser
from .config import Settings
from .operations import OperationError

logger = logging.getLogger(__name__)

AUDIO_EXTENSIONS = {".wav", ".flac", ".mp3", ".ogg", ".opus", ".webm", ".m4a", ".aac", ".mp4"}
AudioType = Literal[
    "Full Song", "Song Demo", "Voice", "Instrument Stem", "Loop", "Rhythm / Percussion",
    "Sample", "Spoken Word", "Sound Effect", "Field Recording / Ambience",
]
VocalGender = Literal["", "male", "female"]
bearer = HTTPBearer(auto_error=False)


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ErrorDetail(BaseModel):
    model_config = ConfigDict(extra="allow")
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorDetail


class MusicRequest(RequestModel):
    lyrics: str = Field(default="", max_length=5000)
    tags: str = Field(default="", max_length=1000)
    title: str = ""
    make_instrumental: bool = False
    negative_prompt: str = Field(default="", max_length=1000)
    model: str = ""
    weirdness: int | None = Field(default=None, ge=0, le=100)
    style_influence: int | None = Field(default=None, ge=0, le=100)
    variety: int | None = Field(default=None, ge=0, le=4)
    vocal_gender: VocalGender = ""
    duration_seconds: int | None = Field(default=None, ge=10, le=360)
    reference_audio_id: UUID | Literal[""] = ""
    audio_mode: Literal["cover", "extend"] = "cover"
    audio_influence: int | None = Field(default=None, ge=0, le=100)
    inspiration_ids: list[UUID] | None = Field(default=None, max_length=4)
    inspiration_playlist: str = ""
    dry_run: bool = False


class SpeechRequest(RequestModel):
    script: str = Field(min_length=1, max_length=5000)
    tone: str = Field(default="", max_length=1000)
    background_music: bool = False
    vocal_gender: VocalGender = ""
    variety: int | None = Field(default=None, ge=0, le=4)
    dry_run: bool = False


class SoundRequest(RequestModel):
    prompt: str = Field(min_length=1, max_length=500)
    sound_type: Literal["one_shot", "loop"] = "one_shot"
    bpm: int | None = Field(default=None, ge=1, le=300)
    dry_run: bool = False


class AbandonRequest(RequestModel):
    attempt_id: UUID


class UploadPathRequest(RequestModel):
    audio_path: str
    confirm_rights: bool = False
    description: str = Field(default="", max_length=1000)
    audio_type: AudioType = "Song Demo"


def _error(status: int, code: str, message: str, **details) -> HTTPException:
    return HTTPException(status, detail={"code": code, "message": message, **details})


def _authenticated(settings: Settings, authorization: str) -> bool:
    if not settings.rest_api_token:
        return True
    scheme, _, token = authorization.partition(" ")
    return scheme.lower() == "bearer" and secrets.compare_digest(
        token.encode(), settings.rest_api_token.encode(),
    )


def _authenticate(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
):
    if not _authenticated(request.app.state.settings, request.headers.get("authorization", "")):
        raise HTTPException(
            401, detail={"code": "unauthorized", "message": "A valid REST bearer token is required."},
            headers={"WWW-Authenticate": "Bearer"},
        )


def _browser(request: Request) -> SunoBrowser:
    browser = request.app.state.browser
    if browser is None:
        raise _error(503, "browser_unavailable", "The browser is not ready yet.")
    return browser


async def _while_connected(request: Request | None, operation):
    if request is None:
        return await operation

    async def disconnected():
        while True:
            if (await request.receive())["type"] == "http.disconnect":
                return

    watcher = asyncio.create_task(disconnected())
    worker = asyncio.create_task(operation)
    try:
        done, _pending = await asyncio.wait((worker, watcher), return_when=asyncio.FIRST_COMPLETED)
        if worker in done:
            return await worker
        await watcher
        raise _error(499, "client_disconnected", "The caller disconnected; check generation status before retrying.")
    finally:
        worker.cancel()
        watcher.cancel()
        await asyncio.gather(worker, watcher, return_exceptions=True)


async def _call(browser: SunoBrowser, operation: str, request: Request | None = None, **arguments):
    try:
        return await _while_connected(request, getattr(browser, operation)(**arguments))
    except OperationError as error:
        details = {"operation_started": error.started, "operations": browser._operations.snapshot()}
        if error.started:
            details.update(generation=await browser.get_generation_status(), upload=browser.get_upload_status())
        failure = _error(error.status, error.code, str(error), **details)
        if not error.started:
            failure.headers = {"Retry-After": "5"}
        raise failure from error
    except ValueError as error:
        raise _error(422, "invalid_argument", str(error)) from error
    except BrowserTimeoutError as error:
        raise _error(
            504, "browser_timeout", "The browser operation timed out. Check generation/upload status before retrying.",
        ) from error
    except RuntimeError as error:
        message = str(error)
        if "awaiting confirmation" in message:
            raise _error(
                409, "generation_pending", message, generation=await browser.get_generation_status(),
            ) from error
        if "An upload is pending" in message:
            raise _error(409, "upload_pending", message, upload=browser.get_upload_status()) from error
        if message.startswith("Track status is "):
            raise _error(409, "track_not_ready", message) from error
        raise _error(502, "browser_error", message) from error


def _result(result: dict, success_status: int = 200) -> JSONResponse:
    if result.get("status") == "error":
        raise _error(502, "suno_error", result.get("message", "Suno rejected the operation."), result=result)
    status = 202 if result.get("status") in {"pending", "captcha_required", "submitted", "upload_pending"} else success_status
    return JSONResponse(result, status_code=status)


def create_rest_app() -> FastAPI:
    app = FastAPI(
        title="Suno REST API", version="1.0.0",
        description=(
            "Generate music, speech, and sound effects using the same browser session as MCP. "
            "Generation returns 202 while pending, submitted, or awaiting a CAPTCHA. "
            "Poll /v1/generation/status; solve CAPTCHAs manually via the returned noVNC URL. "
            "Never automatically retry a generation POST. Bearer authentication is optional unless configured."
        ),
    )
    app.state.settings = Settings()
    app.state.browser = None

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, error: StarletteHTTPException):
        detail = error.detail if isinstance(error.detail, dict) else {"code": "http_error", "message": str(error.detail)}
        return JSONResponse({"error": detail}, status_code=error.status_code, headers=error.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError):
        details = [
            {"location": list(item["loc"]), "message": item["msg"], "type": item["type"]}
            for item in error.errors()
        ]
        return JSONResponse(
            {"error": {"code": "validation_error", "message": "Invalid request.", "details": details}}, status_code=422,
        )

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, error: Exception):
        logger.exception("Unexpected REST error on %s", request.url.path)
        return JSONResponse({"error": {"code": "internal_error", "message": "An unexpected server error occurred."}}, status_code=500)

    @app.middleware("http")
    async def protect_requests(request: Request, call_next):
        path = request.scope["path"].removeprefix(request.scope.get("root_path", ""))
        if path.startswith("/v1/") and path != "/v1/health":
            settings = request.app.state.settings
            if not _authenticated(settings, request.headers.get("authorization", "")):
                return JSONResponse(
                    {"error": {"code": "unauthorized", "message": "A valid REST bearer token is required."}},
                    status_code=401, headers={"WWW-Authenticate": "Bearer"},
                )
            if path == "/v1/uploads" and request.method == "POST":
                content_length = request.headers.get("content-length", "")
                if content_length.isdigit() and int(content_length) > settings.rest_upload_max_bytes + 1024 * 1024:
                    return JSONResponse(
                        {"error": {"code": "upload_too_large", "message": "The upload exceeds the configured size limit."}},
                        status_code=413,
                    )
        return await call_next(request)

    router = APIRouter(
        prefix="/v1", dependencies=[Depends(_authenticate)],
        responses={
            status: {"model": ErrorResponse, "description": description}
            for status, description in {
                401: "REST authentication required.", 404: "Resource not found.",
                409: "Generation or upload pending, or track not ready.", 413: "Upload too large.",
                422: "Invalid request or options.", 429: "Browser waiting queue is full.",
                500: "Unexpected server error.", 502: "Suno or browser failure.",
                503: "Browser not ready or queue wait expired.", 504: "Browser operation timed out.",
            }.items()
        },
    )

    @app.get("/v1/health", tags=["Status"])
    async def health():
        ready = app.state.browser is not None
        return JSONResponse({"status": "ok" if ready else "starting", "browser_ready": ready}, status_code=200 if ready else 503)

    @router.post("/login", tags=["Session"], response_model=dict[str, Any], responses={202: {"description": "Complete login through noVNC."}})
    async def login(request: Request, browser: Annotated[SunoBrowser, Depends(_browser)]):
        if await _call(browser, "begin_login", request=request):
            return {"status": "authenticated"}
        return JSONResponse(
            {"status": "login_required", "novnc_url": f"{request.app.state.settings.base_url}/vnc.html?autoconnect=1&resize=scale"},
            status_code=202,
        )

    @router.post("/generate/music", tags=["Generation"], responses={202: {"description": "Generation accepted, pending, or CAPTCHA required."}})
    async def music(body: MusicRequest, request: Request, browser: Annotated[SunoBrowser, Depends(_browser)]):
        return _result(await _call(browser, "generate_song", request=request, **body.model_dump(mode="json")))

    @router.post("/generate/speech", tags=["Generation"], responses={202: {"description": "Generation accepted, pending, or CAPTCHA required."}})
    async def speech(body: SpeechRequest, request: Request, browser: Annotated[SunoBrowser, Depends(_browser)]):
        return _result(await _call(browser, "generate_speech", request=request, **body.model_dump()))

    @router.post("/generate/sound", tags=["Generation"], responses={202: {"description": "Generation accepted, pending, or CAPTCHA required."}})
    async def sound(body: SoundRequest, request: Request, browser: Annotated[SunoBrowser, Depends(_browser)]):
        return _result(await _call(browser, "generate_sound", request=request, **body.model_dump()))

    @router.get("/generation/status", tags=["Status"], response_model=dict[str, Any])
    async def generation_status(browser: Annotated[SunoBrowser, Depends(_browser)]):
        return await _call(browser, "get_generation_status")

    @router.post("/generation/abandon", tags=["Generation"], response_model=dict[str, Any])
    async def abandon(body: AbandonRequest, request: Request, browser: Annotated[SunoBrowser, Depends(_browser)]):
        return await _call(browser, "abandon_generation_wait", request=request, attempt_id=str(body.attempt_id))

    @router.get("/songs/{song_id}", tags=["Tracks"], response_model=dict[str, Any])
    async def song(song_id: UUID, request: Request, browser: Annotated[SunoBrowser, Depends(_browser)]):
        return await _call(browser, "get_song", request=request, song_id=str(song_id))

    @router.post("/songs/{song_id}/download", tags=["Tracks"], response_model=dict[str, Any])
    async def download(song_id: UUID, request: Request, browser: Annotated[SunoBrowser, Depends(_browser)]):
        path = Path(await _call(browser, "download_song", request=request, song_id=str(song_id)))
        return {
            "song_id": str(song_id), "path": str(path), "filename": path.name,
            "size": path.stat().st_size, "format": path.suffix.lstrip("."),
            "stream_url": f"{request.app.state.settings.base_url}/audio/{path.name}",
            "audio_url": f"{request.app.state.settings.base_url}/api/v1/songs/{song_id}/audio",
        }

    @router.get("/songs/{song_id}/audio", tags=["Tracks"], response_class=FileResponse)
    async def audio(song_id: UUID, request: Request, download: bool = False):
        directory = request.app.state.settings.download_dir.resolve()
        for path in sorted(directory.glob(f"{song_id}.*")):
            if path.suffix.lower() in AUDIO_EXTENSIONS and path.is_file() and path.resolve().is_relative_to(directory):
                response = FileResponse(
                    path, filename=path.name, stat_result=path.stat(),
                    content_disposition_type="attachment" if download else "inline",
                )
                if StaticFiles(directory=str(directory)).is_not_modified(response.headers, request.headers):
                    return NotModifiedResponse(response.headers)
                return response
        raise _error(404, "audio_not_downloaded", "Call the track's download endpoint before requesting its saved audio.")

    @router.post("/uploads/from-path", tags=["Uploads"], status_code=201, responses={202: {"description": "Upload still processing."}})
    async def upload_path(body: UploadPathRequest, request: Request, browser: Annotated[SunoBrowser, Depends(_browser)]):
        return _result(await _call(browser, "upload_audio", request=request, **body.model_dump()), success_status=201)

    @router.post("/uploads", tags=["Uploads"], status_code=201, responses={202: {"description": "Upload still processing."}})
    async def upload(
        request: Request,
        browser: Annotated[SunoBrowser, Depends(_browser)],
        file: Annotated[UploadFile, File()],
        confirm_rights: Annotated[bool, Form()] = False,
        description: Annotated[str, Form(max_length=1000)] = "",
        audio_type: Annotated[AudioType, Form()] = "Song Demo",
    ):
        settings = request.app.state.settings
        destination = None
        try:
            if not confirm_rights:
                raise _error(422, "rights_required", "Set confirm_rights=true only for audio you have permission to upload.")
            extension = Path(file.filename or "").suffix.lower()
            if extension not in AUDIO_EXTENSIONS:
                raise _error(422, "unsupported_audio", "Provide a supported audio file extension.")
            if file.size is not None and file.size > settings.rest_upload_max_bytes:
                raise _error(413, "upload_too_large", "The upload exceeds the configured size limit.")
            directory = settings.auth_dir / "uploads"
            directory.mkdir(parents=True, exist_ok=True)
            destination = directory / f"{uuid4().hex}{extension}"
            size = 0
            with destination.open("xb") as output:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > settings.rest_upload_max_bytes:
                        raise _error(413, "upload_too_large", "The upload exceeds the configured size limit.")
                    output.write(chunk)
            if not size:
                raise _error(422, "empty_audio", "The audio file is empty.")
            result = await _call(
                browser, "upload_audio", request=request, audio_path=str(destination), confirm_rights=True,
                description=description, audio_type=audio_type,
            )
            return _result(result, success_status=201)
        except BaseException:
            pending = browser.get_upload_status()
            if destination and not (pending.get("status") == "upload_pending" and pending.get("filename") == destination.name):
                destination.unlink(missing_ok=True)
            raise
        finally:
            await file.close()

    @router.get("/uploads/status", tags=["Status"], response_model=dict[str, Any])
    async def upload_status(browser: Annotated[SunoBrowser, Depends(_browser)]):
        return browser.get_upload_status()

    app.include_router(router)
    return app
