# Connect Wiki Brain to ChatGPT

ChatGPT connects to MCP servers over **HTTP** (not stdio). Deploy Wiki Brain to Render, then add it as a custom connector.

## Prerequisites

- Render account (free tier works)
- GitHub fork of this repo
- Strong `MCP_API_KEY` (random 32+ char string)

## 1. Deploy to Render

### Option A — Blueprint

1. Push this `opensource/` folder to your GitHub repo
2. Render → **New** → **Blueprint** → connect repo
3. Render reads `render.yaml` automatically

### Option B — Manual

1. New **Web Service** → Docker
2. Dockerfile path: `./Dockerfile`
3. Set environment variables (see below)

## 2. Environment variables on Render

| Variable | Required | Example |
|----------|----------|---------|
| `MCP_API_KEY` | Yes | random secret string |
| `MCP_ALLOW_QUERY_TOKEN` | No | `1` to accept `?token=` auth (off by default; see below) |
| `MCP_TRANSPORT` | Yes | `streamable-http` (set in Dockerfile) |
| `GITHUB_TOKEN` | For writes | fine-grained PAT |
| `GITHUB_REPO` | For writes | `you/wiki-brain` |
| `GITHUB_BRANCH` | For writes | `main` |

After deploy, note your URL: `https://wiki-brain-mcp-xxxx.onrender.com`

## 3. Health check

```bash
curl https://YOUR-SERVICE.onrender.com/health
```

Expected: `{"status":"ok","service":"wiki-brain",...}`

## 4. Add ChatGPT connector

1. ChatGPT → **Settings** → **Connectors** (or Developer / MCP section)
2. Add MCP server URL:

```
https://YOUR-SERVICE.onrender.com/mcp
```

3. Authentication: **Bearer token** = your `MCP_API_KEY`

   `?token=` query-string auth is **disabled by default**: URLs end up in access
   logs, proxy logs, browser history and `Referer` headers, so a key in the URL
   is a leaked key. If your client truly cannot send headers, you can opt in by
   setting `MCP_ALLOW_QUERY_TOKEN=1` on the server and using:

```
https://YOUR-SERVICE.onrender.com/mcp?token=YOUR_MCP_API_KEY
```

   Treat that URL as a secret. The server strips the token from the request
   before logging and sends `Referrer-Policy: no-referrer`, but upstream
   proxies/CDNs may still log it — rotate `MCP_API_KEY` if the URL is ever
   shared or logged.

4. Save and enable the connector in a new chat

## How ChatGPT reads the wiki (`search` + `fetch`)

ChatGPT connectors expect two read-only tools that follow OpenAI's MCP schema:

| Tool | Input | Output |
|------|-------|--------|
| `search` | `{ query }` | `{ "results": [{ "id", "title", "url" }] }` |
| `fetch` | `{ id }` | `{ "id", "title", "text", "url", "metadata" }` |

The flow is two steps: ChatGPT calls `search(query)` to find pages, then
`fetch(id)` (using an `id` from the search results) to pull a page's **full
markdown** so it can display and cite the content. Both tools return their
payload as `structuredContent` **and** as a JSON-encoded string in the `content`
array — Wiki Brain does this automatically.

> If ChatGPT shows search hits but never displays the page content, the server
> is almost always missing a conforming `fetch` tool (or `search` returns a
> non-standard shape). Wiki Brain ships both — make sure your Render service is
> redeployed on the latest commit.

`url` powers ChatGPT citations. When `GITHUB_REPO` is set, Wiki Brain points it
at the source markdown on GitHub (`https://github.com/<repo>/blob/<branch>/wiki/...`);
otherwise it returns a `wiki://` reference.

## Troubleshooting

| Issue | Fix |
|-------|-----|
| 401 Unauthorized | Check `MCP_API_KEY` matches Render env |
| Connected, but **page content / markdown never shows** | Server must expose `search` **and** `fetch` (OpenAI schema). Redeploy Render on the latest commit; verify both tools appear in the connector tool list |
| ChatGPT refuses the whole connector | Tool names must be `snake_case`; `search`/`fetch` must conform to the schema above |
| Empty search results | Redeploy after pushing wiki changes to GitHub |
| Write tools fail | Set `GITHUB_TOKEN` + `GITHUB_REPO` on Render |
| Cold start slow | Free tier sleeps after inactivity — first request wakes it |
