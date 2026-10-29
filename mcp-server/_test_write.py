"""Cloud write round-trip — create_page then delete_page over HTTP.

Requires env vars (never hardcode secrets):
    MCP_TEST_URL=https://your-service.onrender.com
    MCP_API_KEY=your-secret

Run:  python _test_write.py
"""
import os
import sys
import json


def _parse_sse(text):
    for line in text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    return {"_raw": text[:400]}


def main() -> None:
    try:
        import requests
    except ImportError:
        print("pip install requests")
        sys.exit(1)

    base = os.environ.get("MCP_TEST_URL", "").rstrip("/")
    token = os.environ.get("MCP_API_KEY", "")

    if not base or not token:
        print("Set MCP_TEST_URL and MCP_API_KEY environment variables.")
        sys.exit(1)

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }

    def call(session, rid, method, params=None):
        h = {**headers, "mcp-session-id": session}
        body = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params:
            body["params"] = params
        r = requests.post(f"{base}/mcp", headers=h, json=body, timeout=60)
        return _parse_sse(r.text)

    r = requests.post(
        f"{base}/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "clientInfo": {"name": "test-write", "version": "1.0"},
                "capabilities": {},
            },
        },
        timeout=60,
    )
    session = r.headers.get("mcp-session-id")
    print(f"[init] session={session}")
    assert session, "No session id returned"

    requests.post(
        f"{base}/mcp",
        headers={**headers, "mcp-session-id": session},
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        timeout=30,
    )

    print("\n--- create_page ---")
    res = call(session, 2, "tools/call", {
        "name": "create_page",
        "arguments": {
            "folder": "topics",
            "name": "_test-write-verify",
            "content": "# Test Page\nWrite-tool verification. Safe to delete.",
            "title": "Write Tool Test",
        },
    })
    print(res)

    print("\n--- delete_page ---")
    res = call(session, 4, "tools/call", {
        "name": "delete_page",
        "arguments": {"name": "_test-write-verify", "reason": "automated test cleanup"},
    })
    print(res)
    print("\nWrite tools round-trip complete.")


if __name__ == "__main__":
    main()
