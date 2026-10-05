# suno-mcp

Generate music, speech, and sound effects through Suno's native browser interface
using MCP or REST. The container runs Google Chrome Stable, a persistent login profile,
and a noVNC desktop for login and human CAPTCHA completion. Playwright controls
regular Chrome; it does not install Chrome for Testing or spoof an older browser.

## Running

This Compose setup uses the existing `comfy-docker-tailscale:local` image and an
external `tailscale-mesh` network. Set `HS_AUTHKEY` in `.env` using `.env.example`,
then run:

```sh
docker compose up -d --build
docker compose ps
docker compose logs -f suno-mcp
```

For this deployment, connect through Headscale/Tailscale:

- MCP: `http://suno-mcp.tail.ait4x.org/mcp`
- REST: `http://suno-mcp.tail.ait4x.org/api/v1`
- Swagger docs: `http://suno-mcp.tail.ait4x.org/api/docs`
- noVNC: `http://suno-mcp.tail.ait4x.org/vnc.html?autoconnect=1&resize=scale`
- Direct MCP on the Docker host: `http://127.0.0.1:8085/`

Use the hostname, not the raw tailnet IP: routing depends on the HTTP Host header.
Call `suno_login` and complete login in noVNC if needed. Login persists in
`data/chrome-profile`. Do not delete that directory when upgrading the image.
Keep MCP and noVNC on a trusted network; they control a logged-in browser and
are not intended to be exposed publicly without access controls.

## REST API

REST and MCP share one authenticated Chrome instance, browser-operation lock,
generation state, and CAPTCHA guards. REST does not launch a second browser.
Direct access is at `http://127.0.0.1:8085/api/v1`; Swagger UI is at `/api/docs`,
ReDoc at `/api/redoc`, and the OpenAPI schema at `/api/openapi.json`.

When upgrading an already running deployment, apply the new tailnet route
without restarting the tailnet container:

```sh
docker compose exec headscale tailscale serve --bg --http=80 --set-path=/api/ \
  --yes http://suno-mcp-suno-mcp-1:8085/api/
```

| Method | Path (under `/api/v1`) | Purpose |
| --- | --- | --- |
| GET | `/health` | Browser readiness; no login navigation. |
| POST | `/login` | Check Suno login or open interactive login in noVNC; does not wait for human input. |
| POST | `/generate/music` | All music options, including uploaded/existing audio references and up to four inspiration tracks. |
| POST | `/generate/speech` | Script, tone, background music, gender, and variety. |
| POST | `/generate/sound` | One-shot or loop, prompt, and optional BPM. |
| GET | `/generation/status` | Poll the shared generation and CAPTCHA state. |
| POST | `/generation/abandon` | Explicit recovery using `{"attempt_id":"..."}`; cannot cancel accepted work. |
| GET | `/songs/{song_id}` | Track status and metadata. |
| POST | `/songs/{song_id}/download` | Save decrypted playback audio; returns file metadata and URLs. |
| GET | `/songs/{song_id}/audio` | Serve previously saved audio, with range requests; `?download=true` requests an attachment. |
| POST | `/uploads` | Multipart file upload with explicit rights confirmation. |
| POST | `/uploads/from-path` | Upload an existing file under `/data/uploads` or `/downloads`, using the MCP upload arguments as JSON. |
| GET | `/uploads/status` | Poll an audio upload without submitting it again. |

Generation JSON uses the same field names and defaults as MCP, including
`dry_run`. Unknown JSON fields and invalid options return HTTP 422. Generation
returns HTTP 202 for `pending`, `submitted`, or `captcha_required`; 202 is not
a failure and is not a reason to retry the POST. Poll `/generation/status`,
then `/songs/{song_id}` for each returned track. A generation awaiting
confirmation blocks conflicting operations with HTTP 409. Solve CAPTCHAs via
the returned `novnc_url`, then poll again; never automatically retry Create.
Dry-runs return 200 with `status: "prepared"` and spend no generation credits.
Uploads return 201 once confirmed or 202 while processing. Errors use
`{"error":{"code":"...","message":"..."}}`, with validation or pending-state
details when relevant. Browser failures return 502; browser timeouts return
504 and require checking status before retrying.

Optional REST authentication: set `SUNO_REST_API_TOKEN` in `.env` and recreate
the service with `docker compose up -d suno-mcp`. Send
`Authorization: Bearer <token>` or use Swagger's **Authorize** button.
Health and documentation remain readable without a token. This token protects
REST endpoints only, not MCP, noVNC, or legacy `/audio/` links: keep the entire
service on a trusted network. The `/songs/{song_id}/audio` REST route does
require the token when configured.

Saved audio supports byte ranges, `If-Range`, and conditional caching through
`If-None-Match` or `If-Modified-Since`. A matching cache validator returns 304
without resending the file; authentication still applies to conditional requests.

### Request Spikes And Timeouts

Browser operations from MCP and REST share one FIFO admission queue and one
creation tab. There is no parallel form filling or multi-tab generation. A
temporary playback tab is also serialized. Once Suno accepts a submission,
track generation runs remotely and the next browser operation may start.
Pending confirmation, CAPTCHA, or uploads block conflicting operations with
409 instead of navigating away. Cached track lookups, status polling, and
saved-file streaming do not wait for browser admission.

Defaults (configurable in `.env` and Compose):

- `SUNO_BROWSER_QUEUE_CAPACITY=8`: waiting operations, in addition to one active.
- `SUNO_BROWSER_QUEUE_TIMEOUT=15`: maximum queue wait in seconds.
- `SUNO_BROWSER_OPERATION_TIMEOUT=120`: overall browser execution deadline in seconds,
  including nested lookups, rather than a fresh deadline per UI action.

A full queue returns 429 (`browser_queue_full`); an expired wait returns 503
(`browser_queue_timeout`). Both include `Retry-After: 5` and
`operation_started: false`, so callers can back off without duplicate submissions.
An execution deadline returns 504 (`browser_operation_timeout`), with
`operation_started: true` and current generation/upload state. Never blindly
retry it: the click might already have spent credits. Disconnected REST callers
are removed from the waiting queue; active cancellations preserve submission
state and cannot undo an accepted Suno request. MCP cancellation uses the same
admission cleanup. `/generation/status` exposes queue/deadline state under
`operations`. Keep upstream HTTP timeouts above queue wait plus execution time
(Easel defaults to 180 seconds). This is an in-memory admission queue, not a
durable job queue or per-client generation history; restarting loses that state.

Examples (add the Authorization header if configured):

```sh
BASE=http://suno-mcp.tail.ait4x.org/api/v1

curl "$BASE/health"
curl -X POST "$BASE/generate/music" -H 'Content-Type: application/json' \
  -d '{"tags":"gentle piano, ambient","make_instrumental":true,"dry_run":true}'
curl "$BASE/generation/status"

curl -X POST "$BASE/uploads" \
  -F file=@reference.wav -F confirm_rights=true -F audio_type='Song Demo'
curl -X POST "$BASE/generate/music" -H 'Content-Type: application/json' \
  -d '{"tags":"ambient piano","reference_audio_id":"UPLOAD_OR_TRACK_UUID","audio_mode":"cover"}'

curl -X POST "$BASE/songs/TRACK_UUID/download"
curl "$BASE/songs/TRACK_UUID/audio?download=true" -o track.m4a
```

Multipart uploads default to a 100 MiB file limit, adjustable using
`SUNO_REST_UPLOAD_MAX_BYTES`. Files are staged with generated names in
`data/uploads`, never with caller-supplied filesystem paths, and successful
uploads remain there for reuse. The original audio is not transcoded.

## Tools

| Tool | Purpose |
| --- | --- |
| `suno_login` | Check authentication or wait for interactive login. |
| `generate_song` | Lyrics, styles, title, excluded styles, model, sliders, vocal gender, duration, references, and inspiration. |
| `generate_speech` | Script, delivery tone, background music, vocal gender, and variety. |
| `generate_sound` | One-shot effects or loops, with an optional BPM setting. |
| `get_generation_status` | Check submission confirmation, CAPTCHA state, and frequency counters without clicking Create again. |
| `abandon_generation_wait` | Explicitly abandon a stuck local wait using its exact attempt ID; this cannot undo an accepted Suno request. |
| `get_song` | Read a track's status, preview URL, and available audio metadata. |
| `upload_audio` | Upload a file with explicit permission confirmation and obtain its reference ID. |
| `get_upload_status` | Check a pending upload without uploading again. |
| `download_song` | Save the browser player's audio in its original format, without opening export dialogs. |
| `debug_browser` | Save `data/debug.png` and inspect browser, form, and request diagnostics. |

Generation tools return structured results. `submitted` means the native
generation response confirmed new track IDs; it does not mean the audio is
finished. Poll `get_song` for each returned ID until `complete` or `error`.
`preview_url` opens the track in Suno. An unavailable or restricted `audio_url`
is `null`, never a guessed CDN URL. Generation failures are MCP tool errors.

All generation tools accept `dry_run: true` to prepare the form without clicking
Create or spending generation credits. Inspiration dry-runs can still create
an MCP playlist. Browser operations are serialized; do not edit the form in
noVNC while a tool is filling it.

Music sliders `weirdness`, `style_influence`, and `audio_influence` accept 0-100.
`variety` accepts 0-4. `duration_seconds` accepts 10-360, subject to the selected
model's live controls. Duration is set using the native `m:ss` input. Empty
lyrics produce instrumental music; `make_instrumental: true` rejects nonempty
lyrics rather than silently discarding them. Model availability and reference
options depend on the current Suno account and interface.

Example music arguments:

```json
{
  "tags": "gentle piano, warm analog pads, ambient",
  "title": "Quiet Morning",
  "make_instrumental": true,
  "weirdness": 30,
  "style_influence": 60,
  "duration_seconds": 30
}
```

Example speech arguments:

```json
{
  "script": "Welcome to our sound studio.",
  "tone": "warm, clear narrator, relaxed conversational pace",
  "background_music": false,
  "vocal_gender": "female",
  "variety": 1
}
```

Example sound arguments:

```json
{
  "prompt": "A soft wooden percussion loop with delicate shakers, no vocals",
  "sound_type": "loop",
  "bpm": 90
}
```

## Audio References

Place authorized audio in `data/uploads/` on the Docker host, then call
`upload_audio` with the container path:

```json
{
  "audio_path": "/data/uploads/my-reference.wav",
  "confirm_rights": true,
  "description": "My original melody",
  "audio_type": "Song Demo"
}
```

Only files under `/data/uploads` or the configured downloads directory are
accepted. `confirm_rights` must explicitly be true. A confirmed upload returns
`status: "uploaded"` and `id`; if it returns `upload_pending`, poll
`get_upload_status` instead of uploading the same file again.

Pass that ID, or an existing Suno track UUID, to `generate_song` as
`reference_audio_id`, with `audio_mode: "cover"` or `"extend"` and optional
`audio_influence`. Extend uses the cut point selected by Suno's native form;
inspect it in noVNC with a dry-run before submission if that point matters.

For inspiration, pass one to four unique track UUIDs as `inspiration_ids`.
The server creates an `MCP Inspo ...` playlist and selects it through the native
Inspo picker. It reuses that playlist for the same ordered IDs until restart.
Alternatively, set `inspiration_playlist` to an existing playlist's exact name.
Choose reference audio or inspiration, not both. These reference controls apply
to music generation, not the Speech or Sounds tools.

## CAPTCHAs And Downloads

If a challenge appears, generation returns `status: "captcha_required"`, an
actionable message, and `novnc_url`. Solve it yourself in noVNC, then call
`get_generation_status`. The original browser submission remains active; do
not call a generation tool or manually click Create again. New generation
requests are blocked while confirmation is pending. A challenge can appear
after an initial `pending` response, so continue checking status.

The status response includes `attempts`, `captcha_attempts`, and
`captcha_challenges` under `metrics`. These counters cover the current server
process and reset on restart. Repeated checks do not count the same visible
challenge repeatedly. Regular Chrome can still encounter CAPTCHAs; no solver
or CAPTCHA bypass is used. Avoid restarting the service while a challenge or
submission is pending.

If the challenge was canceled or disappeared without confirmation and the wait
remains stuck, first inspect your Suno library for newly created tracks. Then
call `abandon_generation_wait` with the exact `attempt_id` from status to close
the old browser flow. This is explicit recovery, not an automatic retry or a
refund; an already accepted request can still finish on Suno.

Generation credits and native export allowance are separate. `download_song`
saves the decoded audio actually used by the authenticated browser player,
not Suno's quota-controlled export. It uses a captured response or its observed
URL as a fallback, never a guessed CDN address, and does not open export or
purchase dialogs. Encrypted CDN responses are not saved as if they were audio;
the downloader reads the playable blob or the decoded audio segments appended
to the player's media buffer. Temporary playback is muted and accelerated
while waiting for the complete track; this does not alter the saved audio.
If needed, playback starts in a temporary tab that closes after capture.
The file keeps its actual format (for example `.m4a`, not a renamed `.mp3`).
Empty, non-audio, and partial responses are rejected, including inaccessible
resources; short sound effects are valid. Files are saved atomically and the
tool returns a local path and a stream URL. This does not provide WAV/stem
exports or change Suno's export allowance; if playback is unavailable, the tool
returns an error rather than claiming success.
