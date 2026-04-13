# suno-mcp

A [FastMCP 3](https://www.jlowin.dev/blog/fastmcp-3) server that lets AI assistants generate music through [Suno](https://suno.com) using Playwright for browser automation. It handles the full Suno authentication flow and persists the session so you only need to log in once.

## Features

- 🎵 **Generate songs** — describe the music you want (or provide full lyrics, style, and title in custom mode)
- 🔒 **Persistent authentication** — log in once; the session is saved under `~/.suno-mcp/session.json`
- 📋 **Browse your feed** — list recently generated songs
- 🔍 **Song info** — retrieve metadata (title, audio URL, cover art) for any song

## Requirements

- Python 3.11+
- [Playwright](https://playwright.dev/python/) with Chromium (`playwright install chromium`)
- A [Suno](https://suno.com) account

## Installation

```bash
pip install suno-mcp
playwright install chromium
```

Or, to install directly from source:

```bash
git clone https://github.com/venetanji/suno-mcp
cd suno-mcp
pip install -e .
playwright install chromium
```

## Usage

### As an MCP server (stdio transport)

Add the server to your MCP client configuration.  For example, in Claude Desktop's `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "suno": {
      "command": "suno-mcp"
    }
  }
}
```

Then ask your AI assistant to:

1. **Check authentication:** `"Check if I'm logged in to Suno."`
2. **Authenticate:** `"Log in to Suno."` — a browser window will open for you to complete the login flow.
3. **Generate music:** `"Create a cheerful lo-fi hip hop track about summer mornings."`
4. **Custom mode:** `"Generate a song with these lyrics: …, in an acoustic folk style, titled 'River Road'."`

### Running directly

```bash
suno-mcp
```

## Available Tools

| Tool | Description |
|------|-------------|
| `authenticate` | Open a browser window for the user to log in to Suno |
| `check_auth_status` | Check whether the current session is authenticated |
| `generate_song` | Generate one or more songs from a description or lyrics |
| `get_song_info` | Get metadata for a specific song by ID |
| `get_songs` | List recently generated songs from the user's feed |
| `get_songs_by_ids` | Fetch metadata for specific song IDs |
| `clear_session` | Delete the saved session (forces re-authentication) |

## How it works

1. **Authentication** — Playwright opens a Chromium browser (non-headless) to `suno.com/sign-in`. After you log in, cookies and localStorage are saved to `~/.suno-mcp/session.json`.
2. **Song generation** — Playwright navigates to `suno.com/create`, fills in the form fields, and intercepts the generation API response to capture song IDs. It then polls until the songs are ready.
3. **Feed & song info** — Network requests to the Suno API are intercepted via route handlers to extract structured data.

## Session storage

The authentication session is stored at `~/.suno-mcp/session.json`. Delete this file (or call the `clear_session` tool) to force a fresh login.

## License

MIT
