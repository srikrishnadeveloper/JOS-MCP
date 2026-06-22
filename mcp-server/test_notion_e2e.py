"""
Notion sync — end-to-end integration test against a faithful in-memory mock of
the Notion REST API (no real NOTION_TOKEN required).

A tiny local HTTP server implements the exact endpoints the sync code calls
(/search, /databases, /databases/:id/query, /pages, /pages/:id,
/pages/:id/markdown). We then drive the real sync code through the full flow:

  Engine : setup -> push(create) -> push(skip) -> edit+push(update)
           -> edit-in-Notion + pull -> delete(archive)
  Server : server.create_page() auto-mirrors the new page to Notion
           (the "ChatGPT creates a page, it shows up in Notion" path)

Run: python mcp-server/test_notion_e2e.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_PORT = 8731
_TMP = Path(tempfile.mkdtemp(prefix="notion_e2e_"))
(_TMP / "wiki" / "projects").mkdir(parents=True)

# Must be set BEFORE importing notion_sync / server (module-level reads).
os.environ["NOTION_API_BASE"] = f"http://127.0.0.1:{_PORT}"
os.environ["NOTION_TOKEN"] = "test-token"
os.environ["WIKI_BRAIN_DIR"] = str(_TMP / "wiki")
os.environ["WIKI_BRAIN_STATE_DIR"] = str(_TMP / ".state")
os.environ.pop("NOTION_DATABASE_ID", None)

sys.path.insert(0, str(Path(__file__).parent))

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  OK  {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} -- {detail}")


# ---------------------------------------------------------------------------
# Mock Notion API (in-memory)
# ---------------------------------------------------------------------------
DBS: dict[str, dict] = {}
PAGES: dict[str, dict] = {}


def _parse_props(props: dict) -> dict:
    out: dict = {}
    for k, v in props.items():
        if "title" in v:
            out[k] = "".join(s.get("text", {}).get("content", "") for s in v["title"])
        elif "rich_text" in v:
            out[k] = "".join(s.get("text", {}).get("content", "") for s in v["rich_text"])
        elif "multi_select" in v:
            out[k] = [o.get("name", "") for o in v["multi_select"]]
    return out


def _emit_props(p: dict) -> dict:
    return {
        "Name": {"type": "title", "title": [{"plain_text": p.get("Name", "")}]},
        "Folder": {"type": "rich_text", "rich_text": [{"plain_text": p.get("Folder", "")}]},
        "Path": {"type": "rich_text", "rich_text": [{"plain_text": p.get("Path", "")}]},
        "Tags": {"type": "multi_select", "multi_select": [{"name": t} for t in p.get("Tags", [])]},
    }


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence
        pass

    def _send(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        path = self.path.split("?")[0]
        if path.startswith("/pages/") and path.endswith("/markdown"):
            pid = path[len("/pages/"):-len("/markdown")]
            pg = PAGES.get(pid)
            if not pg:
                return self._send({"object": "error"}, 404)
            return self._send({"object": "page_markdown", "id": pid,
                               "markdown": pg["markdown"], "truncated": False,
                               "unknown_block_ids": []})
        self._send({"object": "error"}, 404)

    def do_POST(self):
        path = self.path.split("?")[0]
        body = self._body()
        if path == "/search":
            q = body.get("query", "")
            results = [{"object": "database", "id": dbid, "title": [{"plain_text": d["title"]}]}
                       for dbid, d in DBS.items() if d["title"] == q]
            return self._send({"object": "list", "results": results})
        if path == "/databases":
            dbid = "db-" + uuid.uuid4().hex[:12]
            title = "".join(s.get("text", {}).get("content", "") for s in body.get("title", []))
            DBS[dbid] = {"title": title or "Wiki Brain"}
            return self._send({"object": "database", "id": dbid})
        if path.startswith("/databases/") and path.endswith("/query"):
            dbid = path[len("/databases/"):-len("/query")]
            results = [{"object": "page", "id": pid, "archived": pg["archived"],
                        "properties": _emit_props(pg["props"])}
                       for pid, pg in PAGES.items() if pg["db"] == dbid and not pg["archived"]]
            return self._send({"object": "list", "results": results, "has_more": False})
        if path == "/pages":
            pid = "page-" + uuid.uuid4().hex[:12]
            dbid = body.get("parent", {}).get("database_id", "")
            PAGES[pid] = {"db": dbid, "props": _parse_props(body.get("properties", {})),
                          "markdown": "", "archived": False}
            return self._send({"object": "page", "id": pid})
        self._send({"object": "error"}, 404)

    def do_PATCH(self):
        path = self.path.split("?")[0]
        body = self._body()
        if path.startswith("/pages/") and path.endswith("/markdown"):
            pid = path[len("/pages/"):-len("/markdown")]
            pg = PAGES.get(pid)
            if not pg:
                return self._send({"object": "error"}, 404)
            if body.get("type") == "replace_content":
                pg["markdown"] = body["replace_content"]["new_str"]
            elif body.get("type") == "insert_content":
                pg["markdown"] += body["insert_content"].get("content", "")
            return self._send({"object": "page_markdown", "id": pid, "markdown": pg["markdown"]})
        if path.startswith("/pages/"):
            pid = path[len("/pages/"):]
            pg = PAGES.get(pid)
            if not pg:
                return self._send({"object": "error"}, 404)
            if body.get("archived") is True:
                pg["archived"] = True
            elif "properties" in body:
                pg["props"].update(_parse_props(body["properties"]))
            return self._send({"object": "page", "id": pid})
        self._send({"object": "error"}, 404)


import notion_sync as ns  # noqa: E402  (after env setup)

# Isolate all path resolution to the temp tree.
ns._REPO_ROOT = _TMP
ns._WIKI_DIR = _TMP / "wiki"


async def _test_engine() -> None:
    print("\n1. Engine: setup / push / update / pull / delete")
    os.environ["NOTION_PARENT_PAGE_ID"] = "parent-abc"
    state = ns.SyncState(_TMP / "stateA.json")
    sync = ns.NotionSync(client=ns.NotionClient("test-token"), state=state)

    db = await sync.setup()
    check("setup creates database", bool(db) and db in DBS, f"db={db}")

    f = _TMP / "wiki" / "projects" / "sprint-notes.md"
    f.write_text('---\ntitle: "Sprint Notes"\ntags: [productivity, tools]\n---\n# Sprint Notes\n\nKanban notes.\n', encoding="utf-8")

    check("push creates Notion page", (await sync.push_file("wiki/projects/sprint-notes.md")).startswith("created"))
    pid = next((p for p, pg in PAGES.items() if pg["props"].get("Path") == "wiki/projects/sprint-notes.md"), None)
    check("page exists in Notion", pid is not None)
    pg = PAGES[pid]
    check("Name property", pg["props"]["Name"] == "Sprint Notes", pg["props"]["Name"])
    check("Folder property", pg["props"]["Folder"] == "projects", pg["props"]["Folder"])
    check("Tags property", pg["props"]["Tags"] == ["productivity", "tools"], str(pg["props"]["Tags"]))
    check("markdown body pushed", "Kanban notes." in pg["markdown"])

    check("re-push skips unchanged", (await sync.push_file("wiki/projects/sprint-notes.md")).startswith("skip"))

    f.write_text('---\ntitle: "Sprint Notes"\ntags: [productivity]\n---\n# Sprint Notes\n\nUPDATED notes.\n', encoding="utf-8")
    check("edit then push updates", (await sync.push_file("wiki/projects/sprint-notes.md")).startswith("updated"))
    check("Notion markdown reflects edit", "UPDATED notes." in PAGES[pid]["markdown"])
    check("no duplicate page", sum(1 for x in PAGES.values() if x["props"].get("Path") == "wiki/projects/sprint-notes.md") == 1)

    import writer as wmod

    class _FakeWriter:
        def __init__(self):
            self.calls = []
        async def update_wiki_page(self, rel, full, reason=""):
            self.calls.append(("update", rel)); return {"commit": {"html_url": "x"}}
        async def put_file(self, rel, full, msg):
            self.calls.append(("put", rel)); return {"commit": {"html_url": "x"}}

    fake = _FakeWriter()
    orig = wmod.get_writer
    wmod.get_writer = lambda: fake
    try:
        PAGES[pid]["markdown"] = "# Sprint Notes\n\nEdited inside Notion.\n"
        pulls = await sync.pull_all()
    finally:
        wmod.get_writer = orig
    check("pull reports change", any("pulled" in p for p in pulls), str(pulls))
    check("writer received the pull", bool(fake.calls), str(fake.calls))
    check("local file updated from Notion", "Edited inside Notion." in f.read_text(encoding="utf-8"))

    check("delete archives Notion page", (await sync.delete_file("wiki/projects/sprint-notes.md")).startswith("archived"))
    check("Notion page archived", PAGES[pid]["archived"] is True)


async def _test_server_create_page() -> None:
    print("\n2. Server: create_page() auto-mirrors to Notion (ChatGPT path)")
    import httpx
    r = httpx.post(f"http://127.0.0.1:{_PORT}/databases", json={"title": [{"text": {"content": "Wiki Brain"}}]})
    db_b = r.json()["id"]
    os.environ["NOTION_DATABASE_ID"] = db_b

    import server as srv

    class _FakeWriter:
        async def create_wiki_page(self, folder, name, content, title=None, tags=None):
            return {"commit": {"html_url": "http://fake/commit"}}

    orig = srv.get_writer
    srv.get_writer = lambda: _FakeWriter()
    try:
        out = await srv.create_page(
            folder="topics", name="graphql",
            content="# GraphQL\n\nNotes created via ChatGPT.\n",
            title="GraphQL", tags=["api", "graphql"],
        )
    finally:
        srv.get_writer = orig

    check("create_page reports Notion sync", "Notion: created" in out or "Notion: updated" in out, out)
    pid = next((p for p, pg in PAGES.items()
                if pg["db"] == db_b and pg["props"].get("Path") == "wiki/topics/graphql.md"), None)
    check("new page appears in Notion DB", pid is not None)
    if pid:
        pg = PAGES[pid]
        check("Notion Name = GraphQL", pg["props"]["Name"] == "GraphQL", pg["props"]["Name"])
        check("Notion has markdown body", "Notes created via ChatGPT." in pg["markdown"])
        check("Notion Tags carried over", set(pg["props"]["Tags"]) >= {"api", "graphql"}, str(pg["props"]["Tags"]))


async def _main() -> bool:
    httpd = ThreadingHTTPServer(("127.0.0.1", _PORT), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print("=" * 60)
    print(" Wiki Brain -- Notion sync end-to-end (mock Notion API)")
    print("=" * 60)
    try:
        await _test_engine()
        await _test_server_create_page()
    finally:
        httpd.shutdown()
    print(f"\n{'=' * 60}\n Results: {PASS}/{PASS + FAIL} passed, {FAIL} failed\n{'=' * 60}")
    return FAIL == 0


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(_main()) else 1)
