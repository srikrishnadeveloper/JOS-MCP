# Notion sync

Mirror your `wiki/` markdown in Notion. **GitHub stays the source of truth**; Notion is for browsing and editing in Notion’s UI.

## Setup (one time)

1. Create a [Notion integration](https://www.notion.so/my-integrations).
2. Open a workspace page → **⋯** → **Connections** → add your integration.
3. Copy the page ID from the URL → set `NOTION_PARENT_PAGE_ID` in Render (or `.env` locally).
4. Set `NOTION_TOKEN` in Render Environment.
5. Run setup (creates the **Wiki Brain** database):

```bash
python mcp-server/notion_sync.py setup
```

Copy the printed `NOTION_DATABASE_ID` into Render Environment.

## Commands

```bash
python mcp-server/notion_sync.py push --all   # wiki → Notion
python mcp-server/notion_sync.py pull         # Notion → GitHub
python mcp-server/notion_sync.py sync         # pull then push
python mcp-server/notion_sync.py status
```

## MCP tools

| Tool | Action |
|------|--------|
| `notion_sync_push` | Push all wiki pages to Notion |
| `notion_sync_pull` | Pull Notion edits to GitHub |
| `notion_sync_status` | Show sync stats |

After `create_page` / `update_page`, Wiki Brain auto-pushes that file to Notion when `NOTION_TOKEN` is set.

## Render env vars

| Variable | Required |
|----------|----------|
| `NOTION_TOKEN` | Yes |
| `NOTION_DATABASE_ID` | Yes (from `setup`) |
| `NOTION_PARENT_PAGE_ID` | For first `setup` only |
| `GITHUB_TOKEN` | For pull (Notion → GitHub) |

## Organization in Notion

Each wiki file becomes a row in the **Wiki Brain** database:

- **Name** — page title  
- **Folder** — `projects`, `people`, `topics`, …  
- **Path** — `wiki/projects/task-tracker.md`  
- **Tags** — from YAML frontmatter  

Group or filter by **Folder** in Notion for the same structure as your repo.

## Limitations

- Notion-only blocks (buttons, synced blocks) are not round-tripped.
- `[[wikilinks]]` are kept as text in Notion; links are not auto-resolved on first sync.
- Notion may convert markdown tables to HTML on round-trip — prefer simple markdown in Notion edits.
- Conflicts: last pull/push wins; use `sync` after editing in one place.
- Deletes: `delete_page` archives the matching Notion row (does not hard-delete).

## How auto-sync works

`create_page`, `update_page`, and `append_to_page` call an internal
`_maybe_notion_push` after the GitHub commit. When `NOTION_TOKEN` (+ a known
`NOTION_DATABASE_ID`, set directly or via `setup`) are present, the touched file
is mirrored to Notion in the same request — so a page ChatGPT creates appears in
Notion immediately. If Notion is not configured, the push is a silent no-op and
the write still succeeds. `delete_page` archives the matching Notion row.

State (which wiki file maps to which Notion page) is stored in
`.wiki-brain/notion-sync.json` (override with `WIKI_BRAIN_STATE_DIR`).

## Tests

```bash
python mcp-server/test_notion_sync.py   # 27 offline unit tests (mocked client)
python mcp-server/test_notion_e2e.py    # 21 end-to-end tests vs a mock Notion API
python mcp-server/notion_sync.py status # live config check
```

> `test_notion_e2e.py` spins up a local mock of the Notion REST API (set via
> `NOTION_API_BASE`) and exercises the real sync code end-to-end — including the
> "ChatGPT `create_page` → row appears in Notion" path — without needing a real
> token.
>
> `NOTION_API_BASE` is **test-only — never set it in production.** Every request
> carries `NOTION_TOKEN`, so the override is only honoured for
> `https://api.notion.com` or a loopback host (`localhost`, `127.0.0.1`, `::1`);
> any other value is ignored with a warning and the official API is used.
