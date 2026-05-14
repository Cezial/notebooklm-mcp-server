# notebooklm-mcp-server

> **Bring Google NotebookLM into Claude Code, Claude Desktop, Cursor, or any MCP client.**

An [MCP (Model Context Protocol)](https://modelcontextprotocol.io/) server that exposes Google NotebookLM as a set of tools — let your AI assistant create notebooks, upload sources, ask grounded questions, generate audio/video overviews, mind maps, slide decks, and more.

Built and maintained by [cezial](#).

---

## Status

**Beta.** Used in production internally for design review workflows (`ask_notebook` + `add_source_file`). 24 MCP tools. Defensive retry on transient parser fails. See [Known issues](#known-issues).

## Features

24 MCP tools covering the full NotebookLM API surface:

| Category | Tools |
|---|---|
| **Notebook management** | `list_notebooks`, `create_notebook`, `rename_notebook`, `delete_notebook`, `get_notebook_summary` |
| **Sources** | `list_sources`, `add_source_url`, `add_source_text`, `add_source_file`, `wait_for_source_ready`, `delete_source` |
| **Chat** | `ask_notebook` (with auto-retry on empty answer), `get_conversation_history` |
| **Research** | `web_research` |
| **AI artifacts** | `generate_audio_overview`, `generate_video_overview`, `generate_slide_deck`, `generate_mind_map`, `generate_infographic`, `generate_quiz`, `generate_flashcards`, `generate_report`, `generate_data_table` |
| **Sharing** | `share_notebook` |

## Quick start

### Prerequisites

- Docker + Docker Compose
- A Google account with NotebookLM access
- [`notebooklm-py`](https://github.com/teng-lin/notebooklm-py) CLI installed locally for one-time auth (or browser cookie import)

### 1. Authenticate

```bash
pip install "notebooklm-py[browser]"
notebooklm login
```

This opens a browser to log in to Google → stores session at `~/.notebooklm/storage_state.json`.

### 2. Run the server

```bash
git clone https://github.com/cezial/notebooklm-mcp-server.git
cd notebooklm-mcp-server
docker compose up -d --build
```

Health check:

```bash
curl http://localhost:10007/health
# {"status":"ok","service":"notebooklm-mcp"}
```

### 3. Register with your MCP client

**Claude Code (`~/.claude.json`):**

```json
{
  "mcpServers": {
    "notebooklm-mcp": {
      "type": "http",
      "url": "http://localhost:10007/mcp"
    }
  }
}
```

**Claude Desktop / Cursor**: see their MCP server registration docs — endpoint is `http://localhost:10007/mcp` with `streamable-http` transport.

### 4. Use it

```
> List my NotebookLM notebooks.
> Upload these 5 PDFs to notebook X and wait for them to be ready.
> Ask notebook X: "What are the 3 main themes across these sources?"
> Generate an audio overview for notebook X.
```

## Configuration

Environment variables (set in `docker-compose.yml` or `.env`):

| Var | Default | Purpose |
|---|---|---|
| `HOST` | `0.0.0.0` | Bind address |
| `PORT` | `10007` | HTTP port |
| `LOG_LEVEL` | `INFO` | Python logging level |
| `AUTH_STORAGE_PATH` | `/auth/storage_state.json` | Path to NotebookLM Playwright storage state |
| `CAPTURE_DUMPS` | `1` | Set to `0` to disable debug response dumping |
| `CAPTURE_DUMP_DIR` | `/app/dumps` | Where to write captured response samples |

## Architecture

```
┌──────────────────────────────┐
│ MCP client (Claude Code, ...) │
└──────────────┬───────────────┘
               │ streamable-http
               ▼
┌──────────────────────────────┐
│ notebooklm-mcp-server         │
│ ┌──────────────────────────┐ │
│ │ FastMCP (24 @mcp.tool)   │ │
│ └─────────┬────────────────┘ │
│           ▼                   │
│ ┌──────────────────────────┐ │
│ │ notebooklm-py v0.4.0+    │ │
│ │ (reverse-engineered RPC) │ │
│ └─────────┬────────────────┘ │
└───────────┼─────────────────┘
            │ HTTPS + cookies
            ▼
    notebooklm.google.com
```

- **FastMCP** handles MCP protocol (streamable-http transport)
- **`notebooklm-py`** is the underlying client library (we ship pinned version, contribute fixes upstream)
- **Auth** via Playwright-extracted Google session cookies, mounted read-only into the container

## Known issues

### Empty answer on `ask_notebook` (~rare)

Upstream lib `notebooklm-py` parser occasionally returns empty answer when Google's response stream lacks expected text chunks. Symptoms: `WARNING [notebooklm._chat] No answer extracted from response (N lines parsed)`.

**Mitigation in this server:** `ask_notebook` auto-retries up to 3 times with exponential backoff (2s → 5s → 10s) before returning empty answer with `error` + `hint` fields. Each retry reuses the same conversation_id so context is preserved.

**Upstream tracking:** see `dumps/` directory for raw response captures (disable via `CAPTURE_DUMPS=0`). Failing samples will be submitted to [teng-lin/notebooklm-py](https://github.com/teng-lin/notebooklm-py) as the bug is isolated.

### Single-account only

Current version uses a single `storage_state.json`. `notebooklm-py` v0.4.0+ supports multi-account profiles — multi-account exposure in this MCP server is on the roadmap.

## Development

Run locally without Docker:

```bash
uv sync
uv run python server.py
```

Run tests (TODO — test suite incoming):

```bash
uv run pytest
```

## Contributing

Issues + PRs welcome. See [`CONTRIBUTING.md`](CONTRIBUTING.md).

If you find a parser bug, please attach a `dumps/fail_*.txt` sample (or describe the question that triggered it) — we forward upstream.

## Safety notes

- Auth cookies are mounted **read-only** into the container
- Server binds to `127.0.0.1` by default (not exposed to network)
- `dumps/` may contain real Q&A content — gitignored by default; never commit

## Contact

Questions, bugs, partnerships → `contact@cezial.ai` or open a GitHub issue.

## License

[MIT](LICENSE) © 2026 cezial

## Acknowledgments

- [teng-lin/notebooklm-py](https://github.com/teng-lin/notebooklm-py) — the underlying client library
- [FastMCP](https://github.com/jlowin/fastmcp) — MCP server framework
- [Anthropic MCP](https://modelcontextprotocol.io/) — the protocol
- Google NotebookLM — the product we wrap (unofficial, no affiliation)
