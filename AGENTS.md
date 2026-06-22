# Wiki Brain — Agent Instructions

This file mirrors `CLAUDE.md` for tools that discover `AGENTS.md` automatically (Cursor, Copilot, Claude Code).

## Project

**Wiki Brain** — MCP server that indexes a folder of markdown wiki pages and exposes them to AI clients (Cursor, Claude Desktop, ChatGPT).

## Key paths

| Path | Purpose |
|------|---------|
| `mcp-server/server.py` | MCP server entry point |
| `mcp-server/notion_sync.py` | Notion ↔ wiki mirror (optional) |
| `wiki/` | Markdown knowledge base |
| `CLAUDE.md` | Wiki maintenance schema |
| `docs/` | Setup guides |

## When editing

- Keep `wiki/` free of secrets and government IDs in the public fork.
- Run `python mcp-server/test_server.py` after wiki or engine changes.
- Run `python mcp-server/test_notion_sync.py` after touching `notion_sync.py`.
- Write tools need `GITHUB_TOKEN` + `GITHUB_REPO` — see `docs/GITHUB.md`.
- Notion mirror needs `NOTION_TOKEN` + `NOTION_DATABASE_ID` — see `docs/NOTION.md`.

## Demo vs production

This repo ships a **demo wiki** (fictional Alex Dev). Replace `wiki/` with your own vault for personal use.

## Cursor Cloud specific instructions

### Requirements

- **Python 3.12+** (system `python3` is sufficient)
- No Node.js, Docker, or database required for local development

### Install & test

```bash
chmod +x scripts/*.sh   # first time only
./scripts/install.sh    # pip install + smoke tests (creates .env from .env.example if missing)
```

Or manually: `pip install -r mcp-server/requirements.txt` then `python mcp-server/test_server.py`.

There is no separate linter configured; `test_server.py` is the CI-style check (44 assertions against the demo wiki).

### Running the MCP server

| Mode | Command | Notes |
|------|---------|-------|
| **stdio** (Cursor/Claude) | `./scripts/start.sh` | Default; client spawns the process |
| **HTTP** (cloud/ChatGPT) | `MCP_TRANSPORT=streamable-http PORT=8000 python mcp-server/server.py` | MCP at `http://localhost:8000/mcp` |

Set `WIKI_BRAIN_DIR` to point at a custom vault (defaults to `./wiki`).

### HTTP dev gotchas

- `/health` returns JSON only when `MCP_API_KEY` is set (auth middleware handles it). Without a key, use the MCP `/mcp` endpoint or run `test_server.py` instead.
- `pip install` puts CLI tools (`uvicorn`, `mcp`) in `~/.local/bin`; add to `PATH` if needed: `export PATH="$HOME/.local/bin:$PATH"`.
- Write tools (`create_page`, etc.) need `GITHUB_TOKEN` + `GITHUB_REPO` in `.env` — see `docs/GITHUB.md`. Read tools work offline with no credentials.
- Cloud smoke scripts: `MCP_TEST_URL` + `MCP_API_KEY` for `mcp-server/_test_cloud.py` and `_test_write.py`.
