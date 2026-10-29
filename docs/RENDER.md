# Deploy on Render

## Quick deploy

1. Fork this repository on GitHub
2. [Render Dashboard](https://dashboard.render.com/) → **New +** → **Blueprint**
3. Connect your fork — `render.yaml` defines the service
4. Set secret env vars when prompted:
   - `MCP_API_KEY` (required — the server exits on startup without it)
   - `GITHUB_TOKEN` (optional)
   - `GITHUB_REPO` (optional)

## Service details

- **Runtime:** Docker (`Dockerfile`)
- **Port:** 8000
- **Health:** `/health`
- **MCP endpoint:** `/mcp`

## Updating wiki content on cloud

The Docker image bakes in `wiki/` at build time. To update:

1. Push markdown changes to GitHub
2. Trigger **Manual Deploy** on Render (or enable auto-deploy)

For live writes without redeploy, enable `GITHUB_TOKEN` write tools — changes commit to GitHub and appear after index reload.

## Custom domain (optional)

Render → your service → **Settings** → **Custom Domains**

Use the custom domain in ChatGPT connector URL instead of `onrender.com`.
