"""
Notion sync — unit tests (offline, mocked HTTP).

Run: python mcp-server/test_notion_sync.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent))

import notion_sync as ns
from writer import GitHubWriteError

PASS = 0
FAIL = 0
TOTAL = 0


def test(name: str, condition: bool, detail: str = ""):
    global PASS, FAIL, TOTAL
    TOTAL += 1
    if condition:
        PASS += 1
        print(f"  OK  {name}")
    else:
        FAIL += 1
        msg = f"  FAIL {name}"
        if detail:
            msg += f" -- {detail}"
        print(msg)


def test_parse_frontmatter():
    print("\n1. Frontmatter parsing")
    fm, body = ns._parse_frontmatter("---\ntitle: Hello\n---\nBody")
    test("simple title", fm.get("title") == "Hello")
    test("body extracted", body == "Body")

    fm2, _ = ns._parse_frontmatter('---\ntitle: "Quoted"\naliases: [A, B]\n---\nx')
    test("quoted title", fm2.get("title") == "Quoted")

    fm3, body3 = ns._parse_frontmatter("no frontmatter")
    test("no fm empty", fm3 == {})
    test("no fm body", body3 == "no frontmatter")


def test_parse_tags():
    print("\n2. Tag / alias parsing")
    test("bracket tags", ns._parse_tags('[ "a", "b" ]') == ["a", "b"])
    test("empty", ns._parse_tags("") == [])
    test("max 12", len(ns._parse_tags(", ".join(f"t{i}" for i in range(20)))) == 12)


def test_rebuild_wiki_file():
    print("\n3. Rebuild wiki file from Notion pull")
    sync = ns.NotionSync(client=MagicMock(), state=ns.SyncState(Path("/tmp/unused.json")))
    existing = "---\ntitle: Old\naliases: [Projects Overview]\ntags: [demo]\ncreated: 2026-01-01\n---\n# Body\n"
    full = sync._rebuild_wiki_file(
        title="Projects",
        tags=["demo"],
        body="# Body\n",
        existing_full=existing,
    )
    test("preserves aliases", "Projects Overview" in full)
    test("has title", 'title: "Projects"' in full)
    test("has body", "# Body" in full)
    test("has updated_at", "updated_at:" in full)


def test_wiki_rel_path(tmp: Path):
    print("\n4. Path helpers")
    wiki = tmp / "wiki" / "projects" / "a.md"
    wiki.parent.mkdir(parents=True)
    wiki.write_text("# x", encoding="utf-8")
    with patch.object(ns, "_REPO_ROOT", tmp), patch.object(ns, "_WIKI_DIR", tmp / "wiki"):
        rel = ns._wiki_rel_path(wiki)
        test("rel path", rel == "wiki/projects/a.md")
        files = ns._iter_wiki_files()
        test("iter wiki", len(files) == 1)


def test_sync_state(tmp: Path):
    print("\n5. Sync state persistence")
    state_file = tmp / "notion-sync.json"
    old_db = os.environ.pop("NOTION_DATABASE_ID", None)
    try:
        s = ns.SyncState(state_file)
        s.set_database_id("db-123")
        s.set_page("wiki/a.md", {"notion_page_id": "p1"})
        s2 = ns.SyncState(state_file)
        test("database id", s2.data.get("database_id") == "db-123")
    finally:
        if old_db is not None:
            os.environ["NOTION_DATABASE_ID"] = old_db
    test("page map", s2.get_page("wiki/a.md")["notion_page_id"] == "p1")


async def test_push_skips_unchanged(tmp: Path):
    print("\n6. Push skips unchanged files")
    wiki = tmp / "wiki" / "topics"
    wiki.mkdir(parents=True)
    content = "---\ntitle: T\n---\nbody"
    (wiki / "test.md").write_text(content, encoding="utf-8")
    state_file = tmp / "state.json"
    state = ns.SyncState(state_file)
    rel = "wiki/topics/test.md"
    h = ns._content_hash(content)
    state.set_page(rel, {"notion_page_id": "page-1", "github_hash": h})
    state.set_database_id("db-1")

    mock_client = MagicMock()
    mock_client.update_db_properties = AsyncMock()
    mock_client.replace_markdown = AsyncMock()
    mock_client.query_database = AsyncMock(return_value=[])

    with patch.object(ns, "_REPO_ROOT", tmp), patch.object(ns, "_WIKI_DIR", tmp / "wiki"):
        os.environ["NOTION_DATABASE_ID"] = "db-1"
        sync = ns.NotionSync(client=mock_client, state=state)
        msg = await sync.push_file(rel)
        test("skip unchanged", "skip" in msg)
        test("no notion update", mock_client.replace_markdown.await_count == 0)


async def test_push_resolves_path_from_notion(tmp: Path):
    print("\n7. Push resolves existing Notion page by Path")
    base = tmp / "resolve_case"
    wiki = base / "wiki"
    wiki.mkdir(parents=True, exist_ok=True)
    (wiki / "x.md").write_text("---\ntitle: X\n---\nnew content", encoding="utf-8")
    state_file = tmp / "state.json"
    state = ns.SyncState(state_file)
    state.set_database_id("db-1")

    mock_client = MagicMock()
    mock_client._prop_text = ns.NotionClient._prop_text
    mock_client.query_database = AsyncMock(return_value=[{
        "id": "existing-page",
        "properties": {
            "Path": {"type": "rich_text", "rich_text": [{"plain_text": "wiki/x.md"}]},
        },
    }])
    mock_client.update_db_properties = AsyncMock()
    mock_client.replace_markdown = AsyncMock()
    mock_client.create_db_page = AsyncMock(return_value="new-page")

    with patch.object(ns, "_REPO_ROOT", base), patch.object(ns, "_WIKI_DIR", wiki):
        os.environ["NOTION_DATABASE_ID"] = "db-1"
        sync = ns.NotionSync(client=mock_client, state=state)
        await sync.push_file("wiki/x.md")
        test("indexed from notion", state.get_page("wiki/x.md") is not None)
        test("used update not create", mock_client.replace_markdown.await_count == 1)
        test("no duplicate create", mock_client.create_db_page.await_count == 0)


async def test_pull_uses_put_file_for_new(tmp: Path):
    print("\n8. Pull creates via put_file with full content")
    base = tmp / "pull_case"
    wiki = base / "wiki" / "topics"
    wiki.mkdir(parents=True)
    state_file = base / "state.json"
    state = ns.SyncState(state_file)
    state.set_database_id("db-1")

    mock_client = MagicMock()
    mock_client._prop_text = ns.NotionClient._prop_text
    mock_client._prop_tags = ns.NotionClient._prop_tags
    mock_client.query_database = AsyncMock(return_value=[{
        "id": "n-page",
        "properties": {
            "Name": {"type": "title", "title": [{"plain_text": "New Page"}]},
            "Folder": {"type": "rich_text", "rich_text": [{"plain_text": "topics"}]},
            "Path": {"type": "rich_text", "rich_text": [{"plain_text": "wiki/topics/new-page.md"}]},
            "Tags": {"type": "multi_select", "multi_select": []},
        },
    }])
    mock_client.get_markdown = AsyncMock(return_value="# Hello\n")

    mock_writer = MagicMock()
    mock_writer.update_wiki_page = AsyncMock(side_effect=GitHubWriteError("not found"))
    mock_writer.put_file = AsyncMock(return_value={})

    with patch.object(ns, "_REPO_ROOT", base), patch.object(ns, "_WIKI_DIR", base / "wiki"):
        with patch("writer.get_writer", return_value=mock_writer):
            sync = ns.NotionSync(client=mock_client, state=state)
            results = await sync.pull_all()
        test("pull succeeded", any("pulled" in r for r in results))
        test("put_file called", mock_writer.put_file.await_count == 1)
        put_content = mock_writer.put_file.await_args[0][1]
        test("full file has frontmatter", put_content.startswith("---"))
        test("full file has body", "# Hello" in put_content)


async def test_notion_client_retries_429():
    print("\n9. Notion client retries on 429")
    client = ns.NotionClient("test-token")
    calls = {"n": 0}

    async def fake_request(method, path, **kwargs):
        calls["n"] += 1
        mock_resp = MagicMock()
        if calls["n"] == 1:
            mock_resp.status_code = 429
            mock_resp.text = "rate limited"
            return mock_resp
        mock_resp.status_code = 200
        mock_resp.text = '{"object":"list","results":[]}'
        mock_resp.json.return_value = {"object": "list", "results": []}
        return mock_resp

    with patch("httpx.AsyncClient") as mock_cls:
        inst = mock_cls.return_value.__aenter__.return_value
        inst.request = AsyncMock(side_effect=fake_request)
        with patch("asyncio.sleep", AsyncMock()):
            data = await client._request("POST", "/search", json_body={})
            test("retry succeeded", data.get("results") == [])
            test("two attempts", calls["n"] == 2)


def main() -> bool:
    print("=" * 60)
    print(" Notion Sync — Unit Tests")
    print("=" * 60)

    test_parse_frontmatter()
    test_parse_tags()

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        test_rebuild_wiki_file()
        test_wiki_rel_path(tmp)
        test_sync_state(tmp)
        asyncio.run(test_push_skips_unchanged(tmp))
        asyncio.run(test_push_resolves_path_from_notion(tmp))
        asyncio.run(test_pull_uses_put_file_for_new(tmp))

    asyncio.run(test_notion_client_retries_429())

    print(f"\n{'=' * 60}")
    print(f" Results: {PASS}/{TOTAL} passed, {FAIL} failed")
    print("=" * 60)
    return FAIL == 0


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
