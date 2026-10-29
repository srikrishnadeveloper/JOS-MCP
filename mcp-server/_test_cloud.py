"""Cloud MCP smoke test — initialize session and list tools over HTTP.

Requires env vars (never hardcode secrets):
    MCP_TEST_URL=https://your-service.onrender.com
    MCP_API_KEY=your-secret

Run:  python _test_cloud.py
"""
import os
import sys
import json


def main() -> None:
    try:
        import httpx
    except ImportError:
        print("pip install httpx")
        sys.exit(1)

    base = os.environ.get("MCP_TEST_URL", "").rstrip("/")
    key = os.environ.get("MCP_API_KEY", "")

    if not base or not key:
        print("Set MCP_TEST_URL and MCP_API_KEY environment variables.")
        sys.exit(1)

    url = f"{base}/mcp/"
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }

    init_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test-script", "version": "1.0"},
        },
    }
    r = httpx.post(url, headers=headers, json=init_body, timeout=20, follow_redirects=True)
    print(f"[init] Status: {r.status_code}")
    session = r.headers.get("mcp-session-id", "")
    print(f"[init] Session: {session[:30]}..." if session else "[init] NO session header!")

    if session:
        headers["Mcp-Session-Id"] = session
        httpx.post(url, headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"}, timeout=10)

    tools_body = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    r2 = httpx.post(url, headers=headers, json=tools_body, timeout=20, follow_redirects=True)
    tool_names = []
    for line in r2.text.splitlines():
        if line.startswith("data:"):
            try:
                d = json.loads(line[5:])
                tool_names = [t["name"] for t in d.get("result", {}).get("tools", [])]
            except (json.JSONDecodeError, KeyError):
                pass

    print(f"[tools] {len(tool_names)} tools: {', '.join(tool_names[:8])}{'...' if len(tool_names) > 8 else ''}")


if __name__ == "__main__":
    main()
