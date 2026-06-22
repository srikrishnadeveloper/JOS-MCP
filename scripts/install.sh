#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "Wiki Brain install"
echo "Root: $ROOT"

command -v python3 >/dev/null || { echo "Python 3.12+ required"; exit 1; }
python3 --version

pip install -r "$ROOT/mcp-server/requirements.txt"

if [[ ! -f "$ROOT/.env" ]]; then
  cp "$ROOT/.env.example" "$ROOT/.env"
  echo "Created .env — edit GITHUB_TOKEN and GITHUB_REPO for write tools."
fi

export WIKI_BRAIN_DIR="$ROOT/wiki"
python3 "$ROOT/mcp-server/test_server.py"
python3 "$ROOT/mcp-server/test_notion_sync.py"
python3 "$ROOT/mcp-server/test_notion_e2e.py"

echo "Done. Start: ./scripts/start.sh"
