"""
Notion ↔ wiki/ sync for Wiki Brain.

GitHub (wiki/*.md) is the source of truth. Notion is a mirror + editor UI.

Env:
  NOTION_TOKEN          — integration token (required)
  NOTION_DATABASE_ID    — target database (created by `setup` if missing)
  NOTION_PARENT_PAGE_ID — optional parent for new database (workspace page)

CLI:
  python notion_sync.py setup
  python notion_sync.py push [--all]
  python notion_sync.py pull
  python notion_sync.py sync
  python notion_sync.py status
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("wiki-brain.notion")

_REPO_ROOT = Path(__file__).resolve().parent.parent
_WIKI_DIR = Path(os.environ.get("WIKI_BRAIN_DIR", str(_REPO_ROOT / "wiki")))
# State (wiki path -> Notion page id) lives here. Overridable so tests can
# isolate it and deploys can point it at a writable/persistent location.
_STATE_DIR = Path(os.environ.get("WIKI_BRAIN_STATE_DIR", str(_REPO_ROOT / ".wiki-brain")))
_STATE_FILE = _STATE_DIR / "notion-sync.json"

_NOTION_VERSION_BLOCKS = "2022-06-28"
_NOTION_VERSION_MD = "2025-09-03"
_RATE_DELAY = 0.35  # ~3 req/s Notion limit

# Base URL is overridable so the sync logic can be exercised against a local
# mock Notion server in tests. Defaults to the real Notion API.
_NOTION_API_BASE = os.environ.get("NOTION_API_BASE", "https://api.notion.com/v1").rstrip("/")


class NotionSyncError(RuntimeError):
    pass


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    fm: dict[str, str] = {}
    for line in parts[1].strip().splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            fm[k.strip()] = v.strip().strip('"')
    return fm, parts[2].lstrip("\n")


def _parse_tags(raw: str) -> list[str]:
    if not raw:
        return []
    inner = raw.strip()
    if inner.startswith("[") and inner.endswith("]"):
        inner = inner[1:-1]
    tags = []
    for part in re.split(r",\s*", inner):
        part = part.strip().strip('"').strip("'")
        if part:
            tags.append(part)
    return tags[:12]


def _wiki_rel_path(path: Path) -> str:
    """Repo-relative path like wiki/projects/foo.md."""
    try:
        rel = path.relative_to(_REPO_ROOT)
    except ValueError:
        rel = path.relative_to(_WIKI_DIR)
        return f"wiki/{rel.as_posix()}"
    return rel.as_posix()


def _iter_wiki_files() -> list[Path]:
    if not _WIKI_DIR.exists():
        return []
    return sorted(_WIKI_DIR.rglob("*.md"))


class NotionClient:
    def __init__(self, token: str):
        if not token.strip():
            raise NotionSyncError("NOTION_TOKEN is required")
        self.token = token.strip()
        self._last_req = 0.0

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        markdown_api: bool = False,
        _attempt: int = 0,
    ) -> dict:
        elapsed = time.monotonic() - self._last_req
        if elapsed < _RATE_DELAY:
            await asyncio.sleep(_RATE_DELAY - elapsed)
        version = _NOTION_VERSION_MD if markdown_api else _NOTION_VERSION_BLOCKS
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Notion-Version": version,
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient() as client:
            r = await client.request(
                method,
                f"{_NOTION_API_BASE}{path}",
                headers=headers,
                json=json_body,
                timeout=60,
            )
        self._last_req = time.monotonic()
        if r.status_code == 429 and _attempt < 3:
            await asyncio.sleep(2.0 * (_attempt + 1))
            return await self._request(
                method, path, json_body=json_body, markdown_api=markdown_api, _attempt=_attempt + 1
            )
        if r.status_code >= 400:
            raise NotionSyncError(f"Notion {method} {path} -> {r.status_code}: {r.text[:400]}")
        return r.json() if r.text else {}

    async def search_database(self, title: str = "Wiki Brain") -> str | None:
        data = await self._request(
            "POST",
            "/search",
            json_body={
                "query": title,
                "filter": {"value": "database", "property": "object"},
                "page_size": 20,
            },
        )
        for item in data.get("results", []):
            for t in item.get("title", []):
                if t.get("plain_text") == title:
                    return item["id"]
        return None

    async def create_database(self, parent_page_id: str, title: str = "Wiki Brain") -> str:
        data = await self._request(
            "POST",
            "/databases",
            json_body={
                "parent": {"type": "page_id", "page_id": parent_page_id},
                "title": [{"type": "text", "text": {"content": title}}],
                "properties": {
                    "Name": {"title": {}},
                    "Folder": {"rich_text": {}},
                    "Path": {"rich_text": {}},
                    "Tags": {"multi_select": {}},
                },
            },
        )
        return data["id"]

    async def query_database(self, database_id: str) -> list[dict]:
        results: list[dict] = []
        cursor: str | None = None
        while True:
            body: dict[str, Any] = {"page_size": 100}
            if cursor:
                body["start_cursor"] = cursor
            data = await self._request(
                "POST",
                f"/databases/{database_id}/query",
                json_body=body,
            )
            results.extend(data.get("results", []))
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
        return results

    async def create_db_page(
        self,
        database_id: str,
        *,
        name: str,
        folder: str,
        path: str,
        tags: list[str],
    ) -> str:
        data = await self._request(
            "POST",
            "/pages",
            json_body={
                "parent": {"type": "database_id", "database_id": database_id},
                "properties": {
                    "Name": {"title": [{"text": {"content": name[:2000]}}]},
                    "Folder": {"rich_text": [{"text": {"content": folder[:2000]}}]},
                    "Path": {"rich_text": [{"text": {"content": path[:2000]}}]},
                    "Tags": {"multi_select": [{"name": t[:100]} for t in tags[:12]]},
                },
            },
        )
        return data["id"]

    async def update_db_properties(
        self,
        page_id: str,
        *,
        name: str,
        folder: str,
        path: str,
        tags: list[str],
    ) -> None:
        await self._request(
            "PATCH",
            f"/pages/{page_id}",
            json_body={
                "properties": {
                    "Name": {"title": [{"text": {"content": name[:2000]}}]},
                    "Folder": {"rich_text": [{"text": {"content": folder[:2000]}}]},
                    "Path": {"rich_text": [{"text": {"content": path[:2000]}}]},
                    "Tags": {"multi_select": [{"name": t[:100]} for t in tags[:12]]},
                },
            },
        )

    async def replace_markdown(self, page_id: str, markdown: str) -> None:
        await self._request(
            "PATCH",
            f"/pages/{page_id}/markdown",
            json_body={
                "type": "replace_content",
                "replace_content": {"new_str": markdown},
            },
            markdown_api=True,
        )

    async def get_markdown(self, page_id: str) -> str:
        data = await self._request(
            "GET",
            f"/pages/{page_id}/markdown",
            markdown_api=True,
        )
        return data.get("markdown", "")

    async def archive_page(self, page_id: str) -> None:
        await self._request(
            "PATCH",
            f"/pages/{page_id}",
            json_body={"archived": True},
        )

    @staticmethod
    def _prop_text(props: dict, key: str) -> str:
        val = props.get(key, {})
        if val.get("type") == "rich_text":
            parts = val.get("rich_text", [])
            return "".join(p.get("plain_text", "") for p in parts)
        if val.get("type") == "title":
            parts = val.get("title", [])
            return "".join(p.get("plain_text", "") for p in parts)
        return ""

    @staticmethod
    def _prop_tags(props: dict) -> list[str]:
        val = props.get("Tags", {})
        return [o.get("name", "") for o in val.get("multi_select", []) if o.get("name")]


class SyncState:
    def __init__(self, path: Path = _STATE_FILE):
        self.path = path
        self.data: dict[str, Any] = {
            "database_id": "",
            "parent_page_id": "",
            "pages": {},
        }
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as e:
                log.warning("Could not load sync state: %s", e)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")

    @property
    def database_id(self) -> str:
        return (
            os.environ.get("NOTION_DATABASE_ID", "").strip()
            or self.data.get("database_id", "")
        )

    def set_database_id(self, db_id: str) -> None:
        self.data["database_id"] = db_id
        self.save()

    def get_page(self, rel_path: str) -> dict | None:
        return self.data.get("pages", {}).get(rel_path)

    def set_page(self, rel_path: str, entry: dict) -> None:
        self.data.setdefault("pages", {})[rel_path] = entry
        self.save()


class NotionSync:
    def __init__(self, client: NotionClient | None = None, state: SyncState | None = None):
        _load_dotenv()
        token = os.environ.get("NOTION_TOKEN", "").strip()
        self.client = client or NotionClient(token)
        self.state = state or SyncState()
        self._path_index: dict[str, str] = {}

    async def setup(self, parent_page_id: str | None = None) -> str:
        parent = (
            parent_page_id
            or os.environ.get("NOTION_PARENT_PAGE_ID", "").strip()
            or self.state.data.get("parent_page_id", "")
        )
        if not parent:
            raise NotionSyncError(
                "NOTION_PARENT_PAGE_ID required for setup — share a Notion page "
                "with your integration, then set NOTION_PARENT_PAGE_ID to its ID."
            )
        self.state.data["parent_page_id"] = parent
        db_id = self.state.database_id or await self.client.search_database()
        if not db_id:
            log.info("Creating Wiki Brain database under parent %s", parent)
            db_id = await self.client.create_database(parent)
        self.state.set_database_id(db_id)
        log.info("Notion database ready: %s", db_id)
        return db_id

    def _file_meta(self, path: Path) -> dict[str, Any]:
        text = path.read_text(encoding="utf-8")
        fm, body = _parse_frontmatter(text)
        rel = _wiki_rel_path(path)
        folder = path.parent.name if path.parent != _WIKI_DIR else "root"
        title = fm.get("title") or path.stem.replace("-", " ").title()
        tags = _parse_tags(fm.get("tags", ""))
        return {
            "rel": rel,
            "full": text,
            "body": body,
            "folder": folder,
            "title": title,
            "tags": tags,
            "hash": _content_hash(text),
        }

    async def _ensure_path_index(self) -> None:
        if self._path_index:
            return
        db_id = self.state.database_id
        if not db_id:
            return
        for page in await self.client.query_database(db_id):
            props = page.get("properties", {})
            rel = self.client._prop_text(props, "Path")
            if rel:
                self._path_index[rel] = page["id"]
                if not self.state.get_page(rel):
                    self.state.set_page(rel, {"notion_page_id": page["id"], "imported": True})

    async def push_file(self, rel_path: str) -> str:
        db_id = self.state.database_id
        if not db_id:
            raise NotionSyncError("NOTION_DATABASE_ID not set — run: python notion_sync.py setup")

        path = _REPO_ROOT / rel_path
        if not path.exists():
            path = _WIKI_DIR / rel_path.removeprefix("wiki/")
        if not path.exists():
            raise NotionSyncError(f"File not found: {rel_path}")

        meta = self._file_meta(path)
        rel = meta["rel"]
        await self._ensure_path_index()
        entry = self.state.get_page(rel) or {}
        if entry.get("github_hash") == meta["hash"] and entry.get("notion_page_id"):
            return f"skip {rel} (unchanged)"

        page_id = entry.get("notion_page_id") or self._path_index.get(rel)

        if page_id:
            await self.client.update_db_properties(
                page_id,
                name=meta["title"],
                folder=meta["folder"],
                path=rel,
                tags=meta["tags"],
            )
            await self.client.replace_markdown(page_id, meta["body"])
            action = "updated"
        else:
            page_id = await self.client.create_db_page(
                db_id,
                name=meta["title"],
                folder=meta["folder"],
                path=rel,
                tags=meta["tags"],
            )
            await self.client.replace_markdown(page_id, meta["body"])
            action = "created"

        self.state.set_page(
            rel,
            {
                "notion_page_id": page_id,
                "github_hash": meta["hash"],
                "notion_hash": _content_hash(meta["body"]),
                "last_push": datetime.now(timezone.utc).isoformat(),
            },
        )
        return f"{action} {rel} -> Notion ({page_id})"

    async def push_all(self) -> list[str]:
        if not self.state.database_id:
            await self.setup(os.environ.get("NOTION_PARENT_PAGE_ID") or None)
        await self._ensure_path_index()

        results = []
        for path in _iter_wiki_files():
            rel = _wiki_rel_path(path)
            try:
                msg = await self.push_file(rel)
                results.append(msg)
                log.info(msg)
            except NotionSyncError as e:
                results.append(f"ERROR {rel}: {e}")
                log.error("%s", e)
        return results

    def _rebuild_wiki_file(
        self,
        *,
        title: str,
        tags: list[str],
        body: str,
        existing_full: str | None = None,
    ) -> str:
        fm, _ = _parse_frontmatter(existing_full or "")
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        created = fm.get("created_at") or fm.get("created") or now
        safe_title = title.replace('"', "")
        lines = ["---", f'title: "{safe_title}"']
        aliases = _parse_tags(fm.get("aliases", ""))
        if aliases:
            al = ", ".join(f'"{a}"' for a in aliases)
            lines.append(f"aliases: [{al}]")
        if tags:
            tag_list = ", ".join(f'"{t}"' for t in tags)
            lines.append(f"tags: [{tag_list}]")
        lines.append(f"created_at: {created}")
        lines.append(f"updated_at: {now}")
        lines.append("---\n")
        return "\n".join(lines) + body.rstrip() + "\n"

    async def pull_all(self) -> list[str]:
        db_id = self.state.database_id
        if not db_id:
            raise NotionSyncError("NOTION_DATABASE_ID not set")

        from writer import get_writer, GitHubWriteError  # noqa: F811

        writer = get_writer()
        results: list[str] = []
        pages = await self.client.query_database(db_id)

        path_to_notion: dict[str, dict] = {}
        for page in pages:
            props = page.get("properties", {})
            rel = self.client._prop_text(props, "Path")
            if rel:
                path_to_notion[rel] = page

        for rel, page in path_to_notion.items():
            page_id = page["id"]
            props = page.get("properties", {})
            try:
                body = await self.client.get_markdown(page_id)
                notion_hash = _content_hash(body)
                local_path = _REPO_ROOT / rel
                existing = local_path.read_text(encoding="utf-8") if local_path.exists() else None
                title = self.client._prop_text(props, "Name") or Path(rel).stem
                tags = self.client._prop_tags(props)
                full = self._rebuild_wiki_file(
                    title=title, tags=tags, body=body, existing_full=existing
                )
                gh_hash = _content_hash(full)
                entry = self.state.get_page(rel) or {}
                if (
                    entry.get("notion_hash") == notion_hash
                    and entry.get("github_hash") == gh_hash
                ):
                    results.append(f"skip {rel} (unchanged)")
                    continue

                stem = Path(rel).stem
                try:
                    await writer.update_wiki_page(rel, full, reason="notion sync pull")
                except GitHubWriteError as e:
                    if "not found" not in str(e).lower():
                        raise
                    await writer.put_file(rel, full, f"notion sync: create {stem}")

                local_path.parent.mkdir(parents=True, exist_ok=True)
                local_path.write_text(full, encoding="utf-8")

                self.state.set_page(
                    rel,
                    {
                        "notion_page_id": page_id,
                        "github_hash": gh_hash,
                        "notion_hash": notion_hash,
                        "last_pull": datetime.now(timezone.utc).isoformat(),
                    },
                )
                results.append(f"pulled {rel} -> GitHub")
                log.info("Pulled %s from Notion", rel)
            except (NotionSyncError, GitHubWriteError) as e:
                results.append(f"ERROR {rel}: {e}")
                log.error("%s", e)
        return results

    async def sync(self) -> dict[str, list[str]]:
        pull_results = await self.pull_all()
        push_results = await self.push_all()
        return {"pull": pull_results, "push": push_results}

    async def delete_file(self, rel_path: str) -> str:
        """Archive Notion page when wiki file is deleted."""
        rel = rel_path if rel_path.startswith("wiki/") else f"wiki/{rel_path}"
        await self._ensure_path_index()
        entry = self.state.get_page(rel) or {}
        page_id = entry.get("notion_page_id") or self._path_index.get(rel)
        if not page_id:
            return f"skip {rel} (not in Notion)"
        await self.client.archive_page(page_id)
        pages = self.state.data.get("pages", {})
        pages.pop(rel, None)
        self.state.data["pages"] = pages
        self._path_index.pop(rel, None)
        self.state.save()
        return f"archived {rel} in Notion"

    def status(self) -> dict[str, Any]:
        files = [_wiki_rel_path(p) for p in _iter_wiki_files()]
        mapped = self.state.data.get("pages", {})
        return {
            "database_id": self.state.database_id,
            "wiki_files": len(files),
            "mapped_pages": len(mapped),
            "notion_configured": bool(os.environ.get("NOTION_TOKEN")),
            "github_configured": bool(os.environ.get("GITHUB_TOKEN")),
        }


async def push_file_if_configured(rel_path: str) -> str | None:
    """Called after GitHub writes. No-op if Notion is not configured."""
    _load_dotenv()
    if not os.environ.get("NOTION_TOKEN", "").strip():
        return None
    if not os.environ.get("NOTION_DATABASE_ID", "").strip() and not _STATE_FILE.exists():
        return None
    sync = NotionSync()
    # Clear cached hash so post-write push always runs
    rel = rel_path if rel_path.startswith("wiki/") else f"wiki/{rel_path}"
    entry = sync.state.get_page(rel)
    if entry and "github_hash" in entry:
        entry = dict(entry)
        entry.pop("github_hash", None)
        sync.state.set_page(rel, entry)
    return await sync.push_file(rel_path)


async def delete_file_if_configured(rel_path: str) -> str | None:
    _load_dotenv()
    if not os.environ.get("NOTION_TOKEN", "").strip():
        return None
    if not os.environ.get("NOTION_DATABASE_ID", "").strip() and not _STATE_FILE.exists():
        return None
    sync = NotionSync()
    return await sync.delete_file(rel_path)


def _load_dotenv() -> None:
    env_path = _REPO_ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())


async def _main_async(args: argparse.Namespace) -> int:
    _load_dotenv()
    sync = NotionSync()

    if args.command == "setup":
        db = await sync.setup(args.parent or None)
        print(f"NOTION_DATABASE_ID={db}")
        return 0

    if args.command == "push":
        if args.all or not args.path:
            for line in await sync.push_all():
                print(line)
        else:
            print(await sync.push_file(args.path))
        return 0

    if args.command == "pull":
        for line in await sync.pull_all():
            print(line)
        return 0

    if args.command == "sync":
        result = await sync.sync()
        print("=== pull ===")
        for line in result["pull"]:
            print(line)
        print("=== push ===")
        for line in result["push"]:
            print(line)
        return 0

    if args.command == "status":
        print(json.dumps(sync.status(), indent=2))
        return 0

    return 1


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
    p = argparse.ArgumentParser(description="Wiki Brain Notion sync")
    sub = p.add_subparsers(dest="command", required=True)

    s_setup = sub.add_parser("setup", help="Create/find Notion database")
    s_setup.add_argument("--parent", help="Notion parent page ID")

    s_push = sub.add_parser("push", help="Push wiki -> Notion")
    s_push.add_argument("path", nargs="?", help="wiki/... path (default: all)")
    s_push.add_argument("--all", action="store_true")

    sub.add_parser("pull", help="Pull Notion -> GitHub")
    sub.add_parser("sync", help="Pull then push")
    sub.add_parser("status", help="Show sync status")

    args = p.parse_args()
    raise SystemExit(asyncio.run(_main_async(args)))


if __name__ == "__main__":
    main()
