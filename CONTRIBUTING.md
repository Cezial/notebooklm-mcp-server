# Contributing

Thanks for your interest in `notebooklm-mcp-server`!

## Quick rules

- **Discuss before big changes.** Open an issue first for anything beyond a small bug fix or doc improvement.
- **One concern per PR.** Easier to review, easier to revert.
- **Don't break the public API.** MCP tool signatures and return shapes are contract.
- **No secrets in commits.** `.env`, cookies, `storage_state.json`, `dumps/*` are gitignored — keep it that way.

## Reporting a parser bug

If `ask_notebook` returns empty answer:

1. Set `CAPTURE_DUMPS=1` (default) and reproduce
2. Find the failing dump: `ls dumps/fail_*.txt`
3. Attach the dump (review for sensitive content first) + the question that triggered it
4. We forward upstream to [teng-lin/notebooklm-py](https://github.com/teng-lin/notebooklm-py)

## Development setup

```bash
git clone https://github.com/cezial/notebooklm-mcp-server.git
cd notebooklm-mcp-server
uv sync
uv run python server.py
```

You'll need `~/.notebooklm/storage_state.json` from `notebooklm login` first.

## Code style

- Python 3.13+
- `ruff` for lint (config in `pyproject.toml`)
- Type hints on all `@mcp.tool` functions
- Docstring describing args + return shape (becomes MCP tool description for clients)

## Adding a new MCP tool

1. Add `@mcp.tool async def tool_name(...) -> dict:` in `server.py`
2. Wrap the underlying `notebooklm-py` call inside `async with await get_client() as client:`
3. Return a JSON-serializable dict (or list of dicts)
4. Update README tool table
5. Manual test via Claude Code or `curl` against `/mcp` endpoint

## Submitting a PR

- Branch from `main`
- Conventional commits preferred: `feat:`, `fix:`, `docs:`, `chore:`
- Reference any related issue: `Closes #123`
- CI must pass (TODO — CI incoming)

## License

By contributing, you agree your contributions are MIT-licensed (same as the project).
