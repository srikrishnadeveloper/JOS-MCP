"""
Wiki Brain — smoke tests against the bundled demo wiki.

NEW HERE? Run this file after any code change — all lines should say OK.

WHAT IT TESTS (no GitHub, no network):
  - WikiEngine loads wiki/*.md
  - get_page finds pages by name and alias
  - search returns sensible results
  - folder path validation rules
  - get_status() async tool
  - HTTP API key middleware (Bearer only; ?token= opt-in, redacted)

Run from repo root:  python mcp-server/test_server.py
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from server import (
    WikiEngine,
    WIKI_DIR,
    _truncate,
    _validate_folder_path,
    _normalize_folder,
)

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


def main():
    # LEARN: Each test("name", condition) increments PASS or FAIL. No pytest — plain Python.
    global PASS, FAIL, TOTAL
    start = time.time()

    print("=" * 60)
    print(" Wiki Brain MCP Server -- Test Suite (demo wiki)")
    print("=" * 60)

    print("\n1. Engine Loading")
    engine = WikiEngine(WIKI_DIR)
    n = len(engine.pages)
    test("Wiki directory exists", WIKI_DIR.exists())
    test("Pages loaded", n >= 10, f"got {n}")
    test("Word index built", len(engine.word_index) > 50, f"got {len(engine.word_index)} words")
    test("Name map built", len(engine.name_map) > 15, f"got {len(engine.name_map)} entries")

    print("\n2. Folder Structure")
    folders = engine.get_folders()
    for ef in ["academics", "people", "personal", "projects", "topics", "root"]:
        test(f"Folder '{ef}' exists", ef in folders, f"got {list(folders.keys())}")

    print("\n3. Page Retrieval")
    for stem in ["profile", "index", "task-tracker", "mongodb", "python"]:
        test(f"get_page('{stem}')", engine.get_page(stem) is not None)

    alias_tests = [
        ("Profile", "profile"),
        ("Projects Overview", "projects-index"),
        ("People", "people-index"),
        ("Changelog", "changelog"),
        ("Courses", "courses"),
    ]
    for alias, expected in alias_tests:
        page = engine.get_page(alias)
        test(
            f"get_page('{alias}') -> {expected}",
            page is not None and page.stem == expected,
            f"got {page.stem if page else 'None'}",
        )

    print("\n4. Search")
    for query, expect in [
        ("task tracker", "task-tracker"),
        ("mongodb", "mongodb"),
        ("python", "python"),
        ("jordan", "jordan"),
    ]:
        results = engine.search(query)
        test(f"search('{query}') finds results", len(results) > 0)
        if expect:
            stems = [r["page"] for r in results]
            test(f"  contains '{expect}'", any(expect in s for s in stems), f"got {stems[:5]}")

    test("search('xyznonexistent123') empty", len(engine.search("xyznonexistent123")) == 0)

    folder_results = engine.search("react", folder="projects")
    test("folder filter projects", all(r["folder"] == "projects" for r in folder_results) or len(folder_results) == 0)

    print("\n5. List Pages")
    test("list_pages() >= 10", len(engine.list_pages()) >= 10)
    test("list_pages('people') >= 1", len(engine.list_pages("people")) >= 1)

    print("\n6. Folder Path Validation")
    test("_validate_folder_path('people') ok", _validate_folder_path("people") is None)
    test("_validate_folder_path('work/notes') ok", _validate_folder_path("work/notes") is None)
    test("_validate_folder_path('..') rejected", _validate_folder_path("../x") is not None)
    test("_validate_folder_path('root') rejected", _validate_folder_path("root") is not None)
    test("_normalize_folder strips slashes", _normalize_folder("/people/") == "people")

    print("\n7. Truncation and Edge Cases")
    test("truncate short", _truncate("hello", 100) == "hello")
    test("get_page('') is None", engine.get_page("") is None)
    test("search('') empty", len(engine.search("")) == 0)

    print("\n8. get_status (async)")
    import asyncio
    import server as srv

    async def _status():
        return await srv.get_status()

    try:
        status = asyncio.run(_status())
        test("get_status returns ok", status.get("status") == "ok")
        test("get_status has wiki stats", "total_pages" in status.get("wiki", {}))
        test("get_status has no jos key", "jos" not in status)
    except Exception as e:
        test("get_status runs without error", False, str(e))

    print("\n9. ChatGPT connector compatibility (search + fetch shapes)")
    import server as srv2

    s_out = srv2.search("python")
    test("search returns {results:[...]}", list(s_out.keys()) == ["results"], f"got {list(s_out.keys())}")
    test("search has results", len(s_out["results"]) > 0, f"got {len(s_out['results'])}")
    if s_out["results"]:
        r0 = s_out["results"][0]
        test("search result has id/title/url",
             all(k in r0 for k in ("id", "title", "url")), f"got {list(r0)}")
        f_out = srv2.fetch(r0["id"])
        for k in ("id", "title", "text", "url", "metadata"):
            test(f"fetch has '{k}'", k in f_out, f"got {list(f_out)}")
        test("fetch returns full body", len(f_out.get("text", "")) > 20,
             f"got {len(f_out.get('text', ''))} chars")
        test("fetch metadata values are strings",
             all(isinstance(v, str) for v in f_out.get("metadata", {}).values()))
    miss = srv2.fetch("definitely-not-a-real-page-xyz")
    test("fetch(missing) still returns text", bool(miss.get("text")))

    print("\n10. HTTP auth middleware (API key)")
    import logging
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse as _JR
    from starlette.routing import Route
    from starlette.testclient import TestClient

    seen_qs: list[str] = []

    async def _echo(request):
        seen_qs.append(request.url.query)
        return _JR({"ok": True})

    def _client(allow_query: bool) -> TestClient:
        inner = Starlette(routes=[Route("/mcp", _echo, methods=["GET", "POST"])])
        return TestClient(srv.APIKeyMiddleware(inner, "s3cret", allow_query_token=allow_query))

    c = _client(False)
    test("no token -> 401", c.get("/mcp").status_code == 401)
    test("Bearer ok -> 200", c.get("/mcp", headers={"Authorization": "Bearer s3cret"}).status_code == 200)
    test("Bearer wrong -> 403", c.get("/mcp", headers={"Authorization": "Bearer nope"}).status_code == 403)
    r = c.get("/mcp?token=s3cret")
    test("?token= rejected by default", r.status_code == 401, f"got {r.status_code}")
    test("?token= rejection sets no-referrer", r.headers.get("referrer-policy") == "no-referrer")

    c2 = _client(True)
    seen_qs.clear()
    r = c2.get("/mcp?token=s3cret&x=1")
    test("?token= accepted when opted in", r.status_code == 200, f"got {r.status_code}")
    test("?token= stripped before app", seen_qs == ["x=1"], f"got {seen_qs}")
    test("?token= wrong -> 403 when opted in", c2.get("/mcp?token=bad").status_code == 403)

    rec = logging.LogRecord("uvicorn.access", logging.INFO, "", 0,
                            '%s - "%s %s HTTP/%s" %d',
                            ("1.2.3.4:1", "POST", "/mcp?a=1&token=s3cret", "1.1", 200), None)
    srv._RedactQueryTokenFilter().filter(rec)
    test("log filter redacts token=", "s3cret" not in rec.getMessage() and "[REDACTED]" in rec.getMessage(),
         rec.getMessage())

    print("\n11. Performance")
    t0 = time.time()
    for _ in range(50):
        engine.search("python mongodb task")
    avg_ms = (time.time() - t0) / 50 * 1000
    test("search avg < 500ms", avg_ms < 500, f"avg {avg_ms:.1f}ms")

    elapsed = time.time() - start
    print(f"\n{'=' * 60}")
    print(f" Results: {PASS}/{TOTAL} passed, {FAIL} failed ({elapsed:.1f}s)")
    print("=" * 60)
    return FAIL == 0


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
