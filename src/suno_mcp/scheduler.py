"""Opt-in instrumental scheduling through the existing authenticated MCP server."""

import argparse
import asyncio
import fcntl
import json
import logging
import os
import re
import signal
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastmcp import Client
from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)

INSTRUMENTALS = (
    ("Lanterns After Rain", "instrumental, no vocals, intimate felt piano, warm chamber strings, "
     "soft brushed percussion, reflective cinematic melody, gentle development, spacious natural production"),
    ("Paper Satellites", "instrumental, no vocals, warm analog synthesizers, Rhodes piano, "
     "melodic downtempo electronica, rounded bass, delicate broken beats, hopeful nocturnal atmosphere"),
    ("Copper Morning", "instrumental, no vocals, relaxed acoustic jazz, expressive upright piano, "
     "double bass, brushed drums, muted trumpet, memorable melodic conversation, warm live-room sound"),
    ("The Long Way Home", "instrumental, no vocals, fingerpicked acoustic guitar, cello, soft piano, "
     "organic folk chamber music, bittersweet recurring theme, unhurried arrangement, warm detailed recording"),
    ("Glass Garden", "instrumental, no vocals, marimba, celesta, pizzicato strings, soft woodwinds, "
     "playful chamber soundtrack, elegant repeating motifs, gentle rhythmic variations, clear airy production"),
    ("Blue Hour Transit", "instrumental, no vocals, smoky trip-hop, restrained breakbeat, Rhodes chords, "
     "upright bass, muted guitar, tape textures, wistful strong melodic hook, spacious late-night mix"),
    ("A Small Bright Place", "instrumental, no vocals, warm neo-soul groove, electric piano, "
     "clean guitar, supple bass, pocket drums, melodic instrumental lead, tasteful dynamics, inviting organic sound"),
    ("Tides Between Stars", "instrumental, no vocals, cinematic ambient, slowly evolving strings, "
     "distant piano, soft analog pads, subtle pulse, expressive melodic arc, peaceful expansive production"),
)


class ScheduleSettings(BaseSettings):
    model_config = {"env_prefix": "SUNO_SCHEDULE_"}

    mcp_url: str = "http://suno-mcp:8085/"
    state_dir: Path = Path("/data/schedule")
    interval_seconds: int = Field(default=7200, ge=300)
    credit_reserve: int = Field(default=100, ge=0)
    model: str = ""


class ScheduledRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    title: str
    tags: str
    started_at: float
    credits_before: int
    status: str = "submitting"
    attempt_id: str | None = None
    song_ids: list[str] = Field(default_factory=list)
    confirmed: bool = False
    credits_after: int | None = None


class ScheduleState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    created_at: float
    interval_seconds: int = Field(ge=300)
    next_run_at: float
    model: str = ""
    paused: bool = False
    pause_reason: str | None = None
    last_error: str | None = None
    starting_credits: int | None = None
    latest_credits: int | None = None
    credits_observed_at: float | None = None
    confirmed_submissions: int = 0
    tracks_created: int = 0
    active_run: ScheduledRun | None = None
    last_run: ScheduledRun | None = None


class CreditBalanceUnavailable(ValueError):
    pass


def parse_credits(text: str) -> int:
    for line in text.splitlines():
        if line.startswith("Form elements: "):
            form = json.loads(line.removeprefix("Form elements: "))
            header = form.get("createFormHeader") or ""
            match = re.search(r'aria-label="Credits remaining:\s*([0-9][0-9, ]*)"', header)
            if match:
                return int(match[1].replace(",", "").replace(" ", ""))
    raise CreditBalanceUnavailable("Suno's exact credit balance is unavailable; credit checks will retry without clicking Create. Rounded balances are not used.")


async def call_tool(client, name: str, arguments: dict | None = None):
    result = await client.call_tool(name, arguments or {})
    if name == "debug_browser":
        text = "\n".join(block.text for block in result.content if hasattr(block, "text"))
        return parse_credits(text)
    if not isinstance(result.structured_content, dict):
        raise ValueError(f"{name} did not return a structured result.")
    return result.structured_content


class SongScheduler:
    def __init__(self, settings: ScheduleSettings):
        self.settings = settings
        self.directory = settings.state_dir
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state_path = self.directory / "state.json"

    @contextmanager
    def locked(self, wait: bool = False):
        descriptor = os.open(self.directory / "worker.lock", os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(descriptor, "a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def load(self) -> ScheduleState:
        if self.state_path.exists():
            return ScheduleState.model_validate_json(self.state_path.read_text())
        now = time.time()
        return ScheduleState(
            created_at=now, interval_seconds=self.settings.interval_seconds,
            next_run_at=now + self.settings.interval_seconds,
        )

    def save(self, state: ScheduleState):
        temporary = self.directory / f".state-{uuid4()}.tmp"
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(descriptor, "w") as handle:
                handle.write(state.model_dump_json(indent=2) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.state_path)
            descriptor = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            temporary.unlink(missing_ok=True)

    def record(self, event: str, **details):
        entry = {"event": event, "observed_at": time.time(), **details}
        descriptor = os.open(self.directory / "history.jsonl", os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "a") as handle:
            handle.write(json.dumps(entry) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        logger.info("%s", json.dumps(entry))

    def pause(self, state: ScheduleState, reason: str):
        if not state.paused or state.pause_reason != reason:
            state.paused = True
            state.pause_reason = reason
            self.record("paused", reason=reason)

    async def observe_credits(self, client, state: ScheduleState, panel: str = "music"):
        for attempt, delay in enumerate((0, 1, 2, 4, 8)):
            if delay:
                await asyncio.sleep(delay)
            try:
                balance = await call_tool(client, "debug_browser", {"panel": panel})
                break
            except CreditBalanceUnavailable:
                if attempt == 4:
                    raise
                panel = "current"
                logger.info("Waiting for Suno's exact credit balance (read %d/5).", attempt + 1)
        if state.starting_credits is None:
            state.starting_credits = balance
        state.latest_credits = balance
        state.credits_observed_at = time.time()
        self.record("credits", balance=balance)
        return balance

    async def select_model(self, client, state: ScheduleState):
        models = await call_tool(client, "list_models")
        requested = self.settings.model.strip() or state.model or models.get("default_model")
        if not any(model.get("id") == requested and model.get("available") for model in models.get("data", [])):
            raise ValueError(f"Scheduled model {requested!r} is unavailable; no fallback is allowed.")
        state.model = requested

    def confirm(self, state: ScheduleState, result: dict):
        run = state.active_run
        if not run.confirmed:
            run.confirmed = True
            state.confirmed_submissions += 1
            run.song_ids = [song["id"] for song in result.get("songs", [])]
            state.tracks_created += len(run.song_ids)
            self.record("confirmed", run=run.model_dump())

    async def reconcile(self, client, state: ScheduleState, generation: dict):
        run = state.active_run
        if not run.attempt_id:
            self.pause(state, "Submission confirmation was interrupted. Inspect your library before explicitly resuming.")
            return
        if generation.get("attempt_id") != run.attempt_id:
            if not run.confirmed:
                self.pause(state, "The original generation is no longer observable. Inspect your library before resuming.")
                return
            run.status = "tracking_replaced"
            self.record("tracking_replaced", run=run.model_dump())
            state.last_run, state.active_run = run, None
            return
        if generation["status"] in {"submitted", "complete"}:
            options = generation.get("submitted_options")
            if (not isinstance(options, dict) or options.get("title") != run.title
                    or options.get("tags") != run.tags or options.get("make_instrumental") is not True):
                if run.status != "submission_mismatch":
                    self.record("submission_mismatch", run=run.model_dump(),
                                submitted_title=options.get("title") if isinstance(options, dict) else None)
                run.status = "submission_mismatch"
                self.pause(state, "The accepted submission does not match the scheduled title, style, or instrumental setting. Inspect your library before resuming.")
                return
        run.status = generation["status"]
        if run.status in {"submitted", "complete"}:
            self.confirm(state, generation)
        elif run.status == "captcha_required":
            self.pause(state, "Generation needs CAPTCHA completion. Solve it via noVNC, then resume after tracking finishes.")
            return
        elif run.status == "pending":
            if generation.get("recovery_required") or generation.get("confirmation_stalled"):
                self.pause(state, "Generation confirmation is stalled or lost. Inspect Suno before resuming; do not retry Create.")
            return
        if run.status in {"complete", "error", "abandoned"}:
            if run.status != "complete":
                self.pause(state, f"Scheduled generation ended with {run.status}; inspect Suno before resuming.")
            try:
                run.credits_after = await self.observe_credits(client, state)
            except CreditBalanceUnavailable:
                raise
            except Exception:
                self.pause(state, "Could not read the final credit balance; inspect Suno before resuming.")
                raise
            self.record("finished", run=run.model_dump())
            state.last_run, state.active_run = run, None

    async def tick(self, client, state: ScheduleState):
        if state.paused and state.active_run is None:
            return
        if not state.active_run and state.starting_credits is not None and time.time() < state.next_run_at:
            return
        generation = await call_tool(client, "get_generation_status")
        if state.active_run:
            await self.reconcile(client, state, generation)
        if state.paused or state.active_run:
            return
        if not generation.get("browser", {}).get("connected"):
            raise RuntimeError("Chrome is unavailable; the scheduler will not reopen it.")
        if generation.get("status") == "pending" and not (
            generation.get("recovery_required") or generation.get("confirmation_stalled")
        ):
            return
        if generation.get("status") in {"pending", "captcha_required"}:
            self.pause(state, "Another submission needs confirmation or CAPTCHA completion; no scheduled Create was clicked.")
            return
        operations = generation.get("operations", {})
        if operations.get("active_operation") or operations.get("queued") or generation.get("status") == "submitted":
            return
        if state.starting_credits is None:
            try:
                await self.select_model(client, state)
                await self.observe_credits(client, state, "music")
            except CreditBalanceUnavailable:
                raise
            except ValueError as error:
                self.pause(state, str(error))
                return
            self.record("initialized", model=state.model, next_run_at=state.next_run_at)
        if time.time() < state.next_run_at:
            return
        try:
            balance = await self.observe_credits(client, state, "music")
            await self.select_model(client, state)
        except CreditBalanceUnavailable:
            raise
        except ValueError as error:
            self.pause(state, str(error))
            return
        if balance <= self.settings.credit_reserve:
            self.pause(state, f"Credit balance {balance} is at or below the reserve of {self.settings.credit_reserve}.")
            return
        name, tags = INSTRUMENTALS[state.confirmed_submissions % len(INSTRUMENTALS)]
        now = time.time()
        timestamp = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        state.active_run = ScheduledRun(
            run_id=str(uuid4()), title=f"{name} - {timestamp}", tags=tags,
            started_at=now, credits_before=balance,
        )
        state.next_run_at = now + state.interval_seconds
        self.save(state)
        self.record("submission_started", model=state.model, run=state.active_run.model_dump())
        try:
            result = await call_tool(client, "generate_song", {
                "model": state.model, "title": state.active_run.title, "tags": tags,
                "lyrics": "", "make_instrumental": True,
            })
            state.active_run.attempt_id = result.get("attempt_id")
            state.active_run.status = result["status"]
            self.save(state)
            self.record("submission_result", run=state.active_run.model_dump())
        except Exception:
            self.pause(state, "Submission outcome is uncertain. Inspect generation status and your library; do not retry automatically.")
            raise
        await self.reconcile(client, state, result)

    def status(self, state: ScheduleState) -> dict:
        result = state.model_dump()
        result["next_run_at_iso"] = datetime.fromtimestamp(state.next_run_at, timezone.utc).isoformat()
        result["credit_reserve"] = self.settings.credit_reserve
        result["net_balance_change"] = (
            state.latest_credits - state.starting_credits
            if state.latest_credits is not None and state.starting_credits is not None else None
        )
        return result

    async def run(self):
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signum, stop.set)
        while not stop.is_set():
            try:
                with self.locked():
                    state = self.load()
                    try:
                        if state.interval_seconds != self.settings.interval_seconds:
                            state.interval_seconds = self.settings.interval_seconds
                            state.next_run_at = time.time() + state.interval_seconds
                            self.record("interval_changed", interval_seconds=state.interval_seconds)
                        if not state.paused or state.active_run:
                            async with Client(self.settings.mcp_url, timeout=150, init_timeout=30) as client:
                                await self.tick(client, state)
                        state.last_error = None
                    except Exception as error:
                        state.last_error = str(error)[:1000]
                        self.record("error", message=state.last_error)
                    finally:
                        self.save(state)
            except BlockingIOError:
                logger.info("Another scheduler operation holds the lock; skipping this check.")
            try:
                await asyncio.wait_for(stop.wait(), timeout=60)
            except TimeoutError:
                pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run", "status", "pause", "resume"), nargs="?", default="run")
    parser.add_argument("--acknowledge-uncertain", action="store_true", help="Resume an uncertain run only after inspecting your Suno library.")
    arguments = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    scheduler = SongScheduler(ScheduleSettings())
    if arguments.command == "run":
        asyncio.run(scheduler.run())
        return
    with scheduler.locked(wait=True):
        state = scheduler.load()
        if arguments.command == "pause":
            scheduler.pause(state, "Paused explicitly by the user.")
            scheduler.save(state)
        elif arguments.command == "resume":
            if state.active_run and not state.active_run.confirmed:
                if not arguments.acknowledge_uncertain:
                    raise SystemExit(
                        "A scheduled submission is unconfirmed. Resolve it in Suno and let status tracking finish. "
                        "If its original confirmation was lost, inspect your library before using --acknowledge-uncertain."
                    )
                state.active_run.status = "uncertainty_acknowledged"
                scheduler.record("uncertainty_acknowledged", run=state.active_run.model_dump())
                state.last_run, state.active_run = state.active_run, None
            state.paused, state.pause_reason, state.last_error = False, None, None
            state.next_run_at = max(state.next_run_at, time.time() + state.interval_seconds)
            scheduler.record("resumed", next_run_at=state.next_run_at)
            scheduler.save(state)
        print(json.dumps(scheduler.status(state), indent=2))


if __name__ == "__main__":
    main()
