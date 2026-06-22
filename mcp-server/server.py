"""
Wiki Brain — MCP Server
========================

WHAT THIS FILE DOES (beginner):
  Cursor/ChatGPT asks this program to "search my wiki" or "create a page".
  This file answers those requests by reading markdown files in wiki/.

NEW HERE? Read docs/LEARNING.md for the full study order.
In this file, search for "# LEARN:" comments — they explain each section.

THREE LAYERS:
  1. WikiEngine (below) — load wiki/*.md, search, find pages by name
  2. sanitize.py       — strip passwords before text goes back to the AI
  3. writer.py         — save edits to GitHub (optional; needs GITHUB_TOKEN)

HOW IT RUNS:
  Local:  python server.py  →  stdio  →  Cursor spawns this as a subprocess
  Cloud:  Docker on Render  →  HTTP   →  ChatGPT calls https://you.onrender.com/mcp

TOOLS: 8 read · 9 write · 3 notion-sync · 2 resources — see README.md
  ChatGPT connectors use the `search` + `fetch` pair (OpenAI MCP schema);
  `search` returns {results:[{id,title,url}]} and `fetch` returns the full
  markdown of a page by id so ChatGPT can display and cite it.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from dataclasses import dataclass, field
from collections import defaultdict
from typing import Any
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

import sanitize  # local: redact passwords / financials before responses leave the server

# ---------------------------------------------------------------------------
# Configuration
# LEARN: WIKI_DIR is where your .md files live. Everything else reads from here.
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent  # opensource/ folder (parent of mcp-server/)
WIKI_DIR = Path(os.environ.get(
    "WIKI_BRAIN_DIR",
    str(_REPO_ROOT / "wiki"),  # override to point at your own vault
))

# Keep LLM responses bounded — full pages can be fetched in chunks via search.
MAX_RESPONSE_CHARS = 12_000
SEARCH_EXCERPT_LEN = 300

# ---------------------------------------------------------------------------
# Wiki Engine — indexing, search, page retrieval
# LEARN: This is the "database" of the app — but it's just Python dicts in RAM,
#        built by scanning every .md file once at startup (and patched after writes).
# ---------------------------------------------------------------------------

# Wikilink syntax in markdown: [[mongodb]] or [[profile|Alex]]
_WIKILINK_RE = re.compile(r"\[\[([^\]\|#]+?)(?:\#[^\]\|]+)?(?:\|[^\]]+)?\]\]")


@dataclass
class WikiPage:
    """One markdown file plus parsed metadata. `body` excludes YAML frontmatter."""
    path: Path       # full path on disk, e.g. wiki/people/jordan-friend.md
    stem: str        # filename without .md — used as the page ID in tools
    folder: str      # parent folder name, e.g. "people" (or "root" for wiki/*.md top level)
    title: str
    aliases: list
    tags: list
    content: str
    body: str          # content without frontmatter
    size: int
    frontmatter: dict
    wikilinks: list = field(default_factory=list)   # raw link targets extracted from [[...]]


class WikiEngine:
    """In-memory wiki index built at startup from all wiki/**/*.md files.

    Indexes are updated incrementally after writes so the same process
    sees changes immediately without a full reload.
    """

    def __init__(self, wiki_dir: Path):
        self.wiki_dir = wiki_dir
        self.pages: dict[str, WikiPage] = {}                  # stem -> page
        self.name_map: dict[str, str] = {}                     # lowercase name/alias -> stem
        self.word_index: dict[str, set] = defaultdict(set)     # inverted index: word -> stems
        self.stem_words: dict[str, set] = {}                   # per-page title/tag tokens (search boost)
        self._load()

    # --- Loading & Indexing ---

    @staticmethod
    def _parse_frontmatter(text: str) -> tuple[dict, str]:
        # LEARN: Many wiki pages start with YAML between --- lines (title, tags, dates).
        if not text.startswith("---"):
            return {}, text
        parts = text.split("---", 2)
        if len(parts) < 3:
            return {}, text
        fm = {}
        for line in parts[1].strip().split("\n"):
            if ":" not in line:
                continue
            key, val = line.split(":", 1)
            key, val = key.strip(), val.strip()
            if val.startswith("[") and val.endswith("]"):
                val = [v.strip().strip("\"'") for v in val[1:-1].split(",") if v.strip()]
            fm[key] = val
        return fm, parts[2].strip()

    def _load(self):
        """Walk the wiki tree once and build pages + search indexes."""
        if not self.wiki_dir.exists():
            raise FileNotFoundError(f"Wiki directory not found: {self.wiki_dir}")

        for f in self.wiki_dir.rglob("*.md"):
            rel = f.relative_to(self.wiki_dir)
            folder = str(rel.parent).replace("\\", "/")
            if folder == ".":
                folder = "root"

            text = f.read_text(encoding="utf-8", errors="replace")
            fm, body = self._parse_frontmatter(text)

            title = fm.get("title", f.stem.replace("-", " ").title())
            aliases = fm.get("aliases", [])
            if isinstance(aliases, str):
                aliases = [aliases]
            tags = fm.get("tags", [])
            if isinstance(tags, str):
                tags = [tags]

            wikilinks = [m.group(1).strip() for m in _WIKILINK_RE.finditer(body)]

            page = WikiPage(
                path=f, stem=f.stem, folder=folder,
                title=title, aliases=aliases, tags=tags,
                content=text, body=body, size=len(text),
                frontmatter=fm,
                wikilinks=wikilinks,
            )
            self.pages[f.stem] = page

            # Build name lookup (stem, title, aliases — all lowercase)
            for name in [f.stem, title] + aliases:
                key = name.lower().strip()
                if key:
                    self.name_map[key] = f.stem
                    # Also map hyphenated/spaced variants
                    self.name_map[key.replace(" ", "-")] = f.stem
                    self.name_map[key.replace("-", " ")] = f.stem

            # Build word-level inverted index
            words = set(re.findall(r"\b[a-z0-9]{2,}\b", body.lower()))
            title_words = set(re.findall(r"\b[a-z0-9]{2,}\b", title.lower()))
            stem_name_words = set(re.findall(r"\b[a-z0-9]{2,}\b", f.stem.replace("-", " ").lower()))
            tag_words = set()
            for t in tags:
                tag_words.update(re.findall(r"\b[a-z0-9]{2,}\b", t.lower()))

            important_words = title_words | stem_name_words | tag_words
            self.stem_words[f.stem] = important_words

            for w in words | important_words:
                self.word_index[w].add(f.stem)

    def _prune_deleted_pages(self) -> None:
        """Drop pages whose files were removed on disk (e.g. external git pull)."""
        to_prune = [p for p in self.pages.values() if not p.path.exists()]
        if to_prune:
            for p in to_prune:
                log.info("Pruning deleted page from index: %s (path: %s)", p.stem, p.path)
                self.pages.pop(p.stem, None)
                # Clean up name_map
                keys_to_del = [k for k, v in self.name_map.items() if v == p.stem]
                for k in keys_to_del:
                    self.name_map.pop(k, None)
                # Clean up word_index
                for word, stems in list(self.word_index.items()):
                    if p.stem in stems:
                        stems.discard(p.stem)
                        if not stems:
                            self.word_index.pop(word, None)

    # --- Search ---

    def search(self, query: str, folder: str = "", limit: int = 10) -> list[dict]:
        self._prune_deleted_pages()
        query_lower = query.lower().strip()
        query_words = set(re.findall(r"\b[a-z0-9]{2,}\b", query_lower))
        if not query_words:
            return []

        scores: dict[str, float] = defaultdict(float)

        # Scoring tiers: body match (+2) · title/tag boost (+4) · phrase in title (+8) · phrase in body (+3)
        for word in query_words:
            for stem in self.word_index.get(word, set()):
                scores[stem] += 2.0
                # Extra boost if word is in title/stem/tags
                if stem in self.stem_words and word in self.stem_words[stem]:
                    scores[stem] += 4.0

        # Boost: exact phrase match in title
        for stem, page in self.pages.items():
            if stem not in scores:
                continue
            title_lower = page.title.lower()
            if query_lower in title_lower:
                scores[stem] += 8.0

        # Boost: exact phrase match in body (only for candidates)
        for stem in list(scores.keys()):
            page = self.pages.get(stem)
            if page and query_lower in page.body.lower():
                scores[stem] += 3.0

        # Filter by folder
        if folder:
            folder_lower = folder.lower().strip()
            scores = {
                s: sc for s, sc in scores.items()
                if s in self.pages and self.pages[s].folder.lower() == folder_lower
            }

        ranked = sorted(scores.items(), key=lambda x: -x[1])[:limit]

        results = []
        for stem, score in ranked:
            page = self.pages[stem]
            excerpt = self._excerpt(page.body, query_lower)
            results.append({
                "page": stem,
                "title": page.title,
                "folder": page.folder,
                "score": round(score, 1),
                "excerpt": excerpt,
                "tags": page.tags[:6],
                "size_kb": round(page.size / 1024, 1),
            })
        return results

    def _excerpt(self, text: str, query: str, length: int = SEARCH_EXCERPT_LEN) -> str:
        text_lower = text.lower()
        pos = text_lower.find(query)
        if pos == -1:
            for word in query.split():
                pos = text_lower.find(word)
                if pos != -1:
                    break
        if pos == -1:
            return text[:length].strip() + ("..." if len(text) > length else "")

        start = max(0, pos - length // 3)
        end = min(len(text), pos + length)
        excerpt = text[start:end].strip()
        if start > 0:
            excerpt = "…" + excerpt
        if end < len(text):
            excerpt += "…"
        # Clean up markdown noise
        excerpt = re.sub(r"\n{2,}", "\n", excerpt)
        return excerpt

    # --- Page Retrieval ---

    def get_page(self, name: str) -> WikiPage | None:
        """Resolve a page by stem, title, alias, or fuzzy match (7-step ladder)."""
        self._prune_deleted_pages()
        name_lower = name.lower().strip().strip("/").removesuffix(".md")
        if not name_lower:
            return None

        # 1. Exact stem match
        if name_lower in self.pages:
            return self.pages[name_lower]

        # 2. Name map (title, alias, variant)
        stem = self.name_map.get(name_lower)
        if stem and stem in self.pages:
            return self.pages[stem]

        # 3. Hyphen / space / underscore variants
        for variant in [
            name_lower.replace(" ", "-"),
            name_lower.replace("-", " "),
            name_lower.replace("_", "-"),
            name_lower.replace(" ", ""),
        ]:
            stem = self.name_map.get(variant)
            if stem and stem in self.pages:
                return self.pages[stem]

        # 4. Folder/name combo (e.g. "personal/diary-2026-04-17")
        if "/" in name_lower:
            _, _, last = name_lower.rpartition("/")
            return self.get_page(last)

        # 5. Substring match on stems (e.g. "diary" matches "diary-2026-04-17")
        matches = [s for s in self.pages if name_lower in s]
        if len(matches) == 1:
            return self.pages[matches[0]]

        # 6. Substring match on titles
        title_matches = [
            p for p in self.pages.values()
            if name_lower in p.title.lower()
        ]
        if len(title_matches) == 1:
            return title_matches[0]

        # 7. Best fuzzy: stem contains all query words
        query_words = set(name_lower.replace("-", " ").split())
        if query_words:
            candidates = []
            for stem_key, page in self.pages.items():
                combined = (stem_key + " " + page.title).lower()
                if all(w in combined for w in query_words):
                    candidates.append(page)
            if len(candidates) == 1:
                return candidates[0]
            # If multiple matches, prefer shorter stems (more specific)
            if candidates:
                candidates.sort(key=lambda p: len(p.stem))
                return candidates[0]

        return None

    def list_pages(self, folder: str = "") -> list[dict]:
        self._prune_deleted_pages()
        pages = []
        for page in sorted(self.pages.values(), key=lambda p: (p.folder, p.stem)):
            if folder and page.folder.lower() != folder.lower().strip():
                continue
            pages.append({
                "page": page.stem,
                "title": page.title,
                "folder": page.folder,
                "tags": page.tags[:4],
                "size_kb": round(page.size / 1024, 1),
            })
        return pages

    def get_folders(self) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for page in self.pages.values():
            counts[page.folder] += 1
        return dict(sorted(counts.items()))


# ---------------------------------------------------------------------------
# Helper: truncate for LLM context
# ---------------------------------------------------------------------------

def _truncate(text: str, max_chars: int = MAX_RESPONSE_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n\n---\n*[Truncated — showing {max_chars:,} of {len(text):,} chars. Use search for specific sections.]*"


# ---------------------------------------------------------------------------
# Auth middleware
# LEARN: Only used when deployed on the web (Render). Cursor local mode skips this.
#        MCP_API_KEY is like a password clients must send to use your cloud server.
# ---------------------------------------------------------------------------

log = logging.getLogger("wiki-brain")

# Unauthenticated paths when MCP_API_KEY is set (/health handled separately below).
_OPEN_PATHS = {"/"}


class APIKeyMiddleware(BaseHTTPMiddleware):
    """HTTP auth for cloud deploys only — stdio transport never reaches this.

    Token via Authorization: Bearer … or ?token= (ChatGPT connectors lack custom headers).
    Compares SHA-256 digests so timing does not leak key length.
    If MCP_API_KEY is unset, all requests pass through (local dev).
    """

    def __init__(self, app, api_key: str | None):
        super().__init__(app)
        self._key = api_key.encode() if api_key else None

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Public health / root endpoints — no auth, short-circuit
        if path in ("/health", "/healthz"):
            return JSONResponse({
                "status": "ok",
                "service": "wiki-brain",
                "transport": "streamable-http",
            })

        if self._key is None:
            return await call_next(request)

        if path in _OPEN_PATHS:
            return await call_next(request)

        # Try Authorization header first, then ?token= query param (for ChatGPT
        # connectors which only support OAuth/None — no custom header support).
        token_bytes: bytes | None = None
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token_bytes = auth[len("Bearer "):].encode()
        else:
            q_token = request.query_params.get("token", "")
            if q_token:
                token_bytes = q_token.encode()

        if token_bytes is None:
            log.warning("[AUTH] No token from %s %s", request.client.host if request.client else "?", path)
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        if not hmac.compare_digest(
            hashlib.sha256(token_bytes).digest(),
            hashlib.sha256(self._key).digest(),
        ):
            log.warning("[AUTH] Invalid token from %s", request.client.host if request.client else "?")
            return JSONResponse({"error": "Forbidden"}, status_code=403)

        return await call_next(request)


# ---------------------------------------------------------------------------
# MCP Server
# LEARN: FastMCP registers @mcp_server.tool() functions so AI clients can call them.
#        "instructions" is a system prompt hint telling the model which tools exist.
# ---------------------------------------------------------------------------

mcp_server = FastMCP(
    "wiki-brain",
    host="0.0.0.0",
    port=int(os.environ.get("PORT", 8000)),
    instructions=(
        "You have access to the user's personal markdown wiki via Wiki Brain.\n\n"
        "READ the wiki using: search to find relevant pages, then fetch (by the "
        "id returned from search) to read a page's full markdown content. Other "
        "read tools: read_page, get_status, list_pages, list_folders, get_people, "
        "get_stats.\n\n"
        "WRITE to the wiki when the user says things like 'add this', 'update', "
        "'remember this in my wiki', 'save this'. Use create_folder for a new "
        "empty directory (optional index page), create_page for new documents, "
        "update_page to replace content, append_to_page to add sections, "
        "delete_page / delete_folder to remove, rename_page / rename_folder to "
        "reorganize. Writes commit to GitHub; changes are visible via search/read."
    ),
)

# Singleton — building the index scans every .md file; defer until first tool call.
_engine: WikiEngine | None = None

def _get_engine() -> WikiEngine:
    global _engine
    if _engine is None:
        _engine = WikiEngine(WIKI_DIR)
    return _engine


# ---------------------------------------------------------------------------
# Read tools — query the in-memory index (responses pass through sanitize)
# LEARN: These run without GitHub. They only read files already on disk / in memory.
#        structured_output=True means the AI gets JSON fields, not just a string.
# ---------------------------------------------------------------------------

def _page_id(page: "WikiPage") -> str:
    """Stable identifier a client passes back to `fetch`. `folder/stem` keeps it
    unique across folders and is resolvable by `engine.get_page` (handles the slash)."""
    if page.folder and page.folder != "root":
        return f"{page.folder}/{page.stem}"
    return page.stem


def _page_url(page: "WikiPage") -> str:
    """Canonical URL for ChatGPT citations. Points at the source markdown on GitHub
    when GITHUB_REPO is configured; otherwise a stable wiki:// reference."""
    rel = f"{page.stem}.md" if (not page.folder or page.folder == "root") else f"{page.folder}/{page.stem}.md"
    repo = os.environ.get("GITHUB_REPO", "").strip()
    if repo:
        branch = (os.environ.get("GITHUB_BRANCH", "main").strip() or "main")
        return f"https://github.com/{repo}/blob/{branch}/wiki/{rel}"
    return f"wiki://{rel}"


# LEARN: ChatGPT (and deep research / company knowledge) require an MCP server to
#        expose two read tools that match OpenAI's schema exactly:
#          - search(query)  -> {"results": [{"id", "title", "url"}, ...]}
#          - fetch(id)      -> {"id", "title", "text", "url", "metadata"}
#        ChatGPT calls search first, then fetch(id) to pull a page's FULL markdown
#        so it can display and cite it. Without a conforming fetch tool the
#        connector finds pages but never shows their content.
@mcp_server.tool(
    structured_output=True,
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
)
def search(query: str) -> dict[str, Any]:
    """Search the personal markdown wiki and return matching pages.

    Returns a list of results, each with an `id` (pass it to `fetch` to read the
    full page), a `title`, and a `url`. Use this first to find relevant pages,
    then call `fetch` with a result's `id` to read its complete markdown content.

    Args:
        query: Search terms (e.g., 'task tracker', 'python notes', 'mongodb').
    """
    engine = _get_engine()
    raw = engine.search(query)

    results: list[dict[str, Any]] = []
    for r in raw:
        page = engine.pages.get(r["page"])
        if not page or sanitize.is_private(page):
            continue
        results.append({
            "id":    _page_id(page),
            "title": page.title,
            "url":   _page_url(page),
            # `text` is an optional snippet ChatGPT may show under the result.
            "text":  sanitize.redact(r.get("excerpt", "")),
        })

    # OpenAI's schema expects the structured payload to be exactly {"results": [...]}.
    # FastMCP also emits this dict as JSON in the content array, satisfying the
    # "both structuredContent and JSON-encoded text" requirement for connectors.
    return {"results": results}


@mcp_server.tool(
    structured_output=True,
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
)
def fetch(id: str) -> dict[str, Any]:
    """Fetch the full markdown content of a wiki page by its id.

    Call this with an `id` returned by `search` (e.g. 'personal/profile' or
    'profile'). Returns the page's complete markdown so it can be displayed and
    cited.

    Args:
        id: The page identifier from a `search` result (folder/stem, stem, title, or alias).
    """
    engine = _get_engine()
    page = engine.get_page(id)
    if not page:
        # ChatGPT expects a populated text field even when nothing is found.
        return {
            "id":       id,
            "title":    "Not found",
            "text":     f"No wiki page matches id '{id}'. Use `search` to find a valid id.",
            "url":      "",
            "metadata": {"found": "false"},
        }

    if sanitize.is_private(page):
        text = sanitize.private_placeholder(page)
    else:
        text = sanitize.redact(_truncate(page.body, max_chars=MAX_RESPONSE_CHARS))

    # metadata values are strings — ChatGPT's fetch schema types metadata as
    # an object of string values.
    metadata = {
        "folder":  page.folder,
        "stem":    page.stem,
        "tags":    ", ".join(page.tags or []),
        "aliases": ", ".join(page.aliases or []),
        "size_kb": str(round(page.size / 1024, 1)),
    }
    return {
        "id":       _page_id(page),
        "title":    page.title,
        "text":     text,
        "url":      _page_url(page),
        "metadata": metadata,
    }


@mcp_server.tool(
    structured_output=True,
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
)
def read_page(name: str) -> dict[str, Any]:
    """Read a specific wiki page by name, title, or alias.

    Args:
        name: Page identifier — stem (e.g., 'profile'), title ('Device Setup'), or alias ('Lovable Projects')
    """
    engine = _get_engine()
    page = engine.get_page(name)
    if not page:
        suggestions = engine.search(name, limit=5)
        if suggestions:
            hints = ", ".join(f"`{s['page']}`" for s in suggestions)
            summary = f"Page `{name}` not found. Similar: {hints}"
        else:
            summary = f"Page `{name}` not found."
        return {
            "found":       False,
            "query":       name,
            "suggestions": [s["page"] for s in suggestions],
            "summary":     summary,
        }

    # Whole-page hide — sensitive stems/folders never leave the server
    if sanitize.is_private(page):
        placeholder = sanitize.private_placeholder(page)
        return {
            "found": True, "page": page.stem, "title": page.title,
            "folder": page.folder, "body": placeholder,
            "tags": [], "aliases": [], "size_kb": round(page.size / 1024, 1),
            "summary": placeholder, "private": True,
        }

    body = sanitize.redact(_truncate(page.body, max_chars=MAX_RESPONSE_CHARS))
    summary = (
        f"# {page.title}\n"
        f"**File:** `{page.folder}/{page.stem}.md` · **Size:** {page.size:,} bytes · "
        f"**Tags:** {', '.join(page.tags)}\n\n---\n\n"
        f"{body}"
    )
    return {
        "found":   True,
        "page":    page.stem,
        "title":   page.title,
        "folder":  page.folder,
        "body":    body,
        "tags":    list(page.tags or []),
        "aliases": list(page.aliases or []),
        "size_kb": round(page.size / 1024, 1),
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# Status helpers — git + filesystem metadata for get_status / wiki://status
# ---------------------------------------------------------------------------

def _get_git_info() -> dict[str, Any]:
    """Best-effort git metadata; falls back to Render env vars when .git is absent."""
    repo_root = WIKI_DIR.parent
    info = {
        "branch": os.environ.get("RENDER_GIT_BRANCH") or os.environ.get("GITHUB_BRANCH", "main"),
        "commit": os.environ.get("RENDER_GIT_COMMIT") or "unknown",
        "message": "unknown",
        "author": "unknown",
        "date": "unknown",
        "dirty": False
    }
    try:
        # Branch
        r_branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(repo_root), capture_output=True, text=True, timeout=5
        )
        if r_branch.returncode == 0:
            info["branch"] = r_branch.stdout.strip()
            
        # Commit info
        r_log = subprocess.run(
            ["git", "log", "-1", "--format=%H%n%s%n%an%n%ad"],
            cwd=str(repo_root), capture_output=True, text=True, timeout=5
        )
        if r_log.returncode == 0:
            lines = r_log.stdout.strip().split("\n")
            if len(lines) >= 1: info["commit"] = lines[0]
            if len(lines) >= 2: info["message"] = lines[1]
            if len(lines) >= 3: info["author"] = lines[2]
            if len(lines) >= 4: info["date"] = lines[3]
            
        # Dirty check
        r_status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(repo_root), capture_output=True, text=True, timeout=5
        )
        if r_status.returncode == 0:
            info["dirty"] = bool(r_status.stdout.strip())
    except Exception:
        pass
    return info


def _get_recent_changelog() -> list[str]:
    changelog_path = WIKI_DIR / "changelog.md"
    if not changelog_path.exists():
        return []
    try:
        content = changelog_path.read_text(encoding="utf-8")
        # Match lines starting with ## [date] description or ## description
        entries = re.findall(r"^##\s*(.+?)(?:\r?\n|\Z)", content, re.M)
        return entries[:5]
    except Exception:
        return []


def _get_recently_modified_files() -> list[dict[str, Any]]:
    files = []
    try:
        for p in WIKI_DIR.rglob("*.md"):
            if p.is_file() and p.name != "changelog.md" and p.name != "index.md":
                mtime = p.stat().st_mtime
                rel_path = p.relative_to(WIKI_DIR).as_posix()
                files.append({"rel_path": rel_path, "mtime": mtime})
        files.sort(key=lambda x: x["mtime"], reverse=True)
        
        # format time ago
        now = time.time()
        for f in files[:5]:
            dt = now - f["mtime"]
            if dt < 60:
                f["ago"] = "just now"
            elif dt < 3600:
                f["ago"] = f"{int(dt // 60)}m ago"
            elif dt < 86400:
                f["ago"] = f"{int(dt // 3600)}h ago"
            else:
                f["ago"] = f"{int(dt // 86400)}d ago"
            # remove raw mtime from output to keep clean
            del f["mtime"]
        return files[:5]
    except Exception:
        return []


def _get_wiki_stats() -> dict[str, Any]:
    engine = _get_engine()
    folders = engine.get_folders()
    total_pages = sum(folders.values())
    
    # Calculate total size of markdown files
    total_size = 0
    try:
        for p in WIKI_DIR.rglob("*.md"):
            if p.is_file():
                total_size += p.stat().st_size
    except Exception:
        pass
        
    return {
        "total_pages": total_pages,
        "total_size_kb": round(total_size / 1024, 1),
        "folders": [{"name": k, "page_count": v} for k, v in sorted(folders.items(), key=lambda x: x[1], reverse=True)]
    }


@mcp_server.tool(structured_output=True)
async def get_status() -> dict[str, Any]:
    """Wiki health snapshot: git state, page counts, recent changelog + file edits."""
    git_info = _get_git_info()
    wiki_stats = _get_wiki_stats()
    changelog = _get_recent_changelog()
    modified = _get_recently_modified_files()

    return {
        "status": "ok",
        "git": git_info,
        "wiki": wiki_stats,
        "recent_changes": {
            "changelog": changelog,
            "modified_files": modified
        },
    }


@mcp_server.tool()
def list_pages(folder: str = "") -> str:
    """List all wiki pages, optionally filtered by folder.

    Args:
        folder: Folder name — personal, people, topics, projects, academics, etc. (or empty for all)
    """
    engine = _get_engine()
    pages = engine.list_pages(folder)
    if not pages:
        folders = engine.get_folders()
        folder_list = ", ".join(f"`{f}` ({c})" for f, c in folders.items())
        return f"No pages in `{folder}`. Available folders: {folder_list}"

    lines = [f"**{len(pages)} pages**" + (f" in `{folder}/`" if folder else "") + "\n"]
    current_folder = ""
    for p in pages:
        if p["folder"] != current_folder:
            current_folder = p["folder"]
            lines.append(f"\n### {current_folder}/")
        tags = f" · {', '.join(p['tags'])}" if p["tags"] else ""
        lines.append(f"- **{p['page']}** — {p['title']} ({p['size_kb']} KB{tags})")
    return "\n".join(lines)


@mcp_server.tool()
def get_people(name: str = "") -> str:
    """Get information about a person — family, friends, clients, classmates.

    Args:
        name: Person name (e.g., 'jordan'). Empty for people index.
    """
    engine = _get_engine()

    if not name:
        page = engine.get_page("people-index")
        if page:
            return sanitize.safe_content(page) if not sanitize.is_private(page) else sanitize.private_placeholder(page)
        return "People index not found."

    # Try direct page match in people folder
    for stem, page in engine.pages.items():
        if page.folder == "people" and name.lower() in stem.lower():
            return sanitize.safe_content(page)

    # Search people folder
    results = engine.search(name, folder="people")
    if results:
        page = engine.get_page(results[0]["page"])
        if page:
            return sanitize.safe_content(page)

    # Broader search
    results = engine.search(name, limit=5)
    if results:
        lines = [f"No dedicated page for '{name}'. Found mentions in:\n"]
        for r in results:
            lines.append(f"- **{r['page']}** ({r['folder']}): {sanitize.redact(r['excerpt'][:150])}")
        return "\n".join(lines)

    return f"No information found about '{name}'."


@mcp_server.tool()
def get_stats() -> str:
    """Get overall wiki statistics — page counts and folder breakdown."""
    engine = _get_engine()
    folders = engine.get_folders()
    total_size = sum(p.size for p in engine.pages.values())

    lines = [
        "# Wiki Brain — Statistics\n",
        "| Metric | Value |",
        "|--------|-------|",
        f"| **Total pages** | {len(engine.pages)} |",
        f"| **Total size** | {total_size / 1024 / 1024:.2f} MB |",
        f"| **Folders** | {len(folders)} |",
    ]
    lines.extend(["", "## Pages by Folder\n"])
    for folder, count in folders.items():
        lines.append(f"- **{folder}/** — {count} pages")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# MCP resources — static URIs some clients prefetch at connect time
# ---------------------------------------------------------------------------

@mcp_server.resource("wiki://index")
def resource_index() -> str:
    """Navigation hub — returns the wiki index page."""
    engine = _get_engine()
    page = engine.get_page("index")
    return sanitize.safe_content(page) if page else "Index not found."
@mcp_server.resource("wiki://status")
async def resource_status() -> str:
    """System status — git details, wiki metrics, recent activity."""
    status_data = await get_status()
    lines = [
        "# Wiki System Status",
        "",
        "## Git Repository Info",
        f"- **Branch**: {status_data['git']['branch']}",
        f"- **Latest Commit**: `{status_data['git']['commit'][:8]}` by {status_data['git']['author']}",
        f"- **Date**: {status_data['git']['date']}",
        f"- **Commit Message**: *{status_data['git']['message']}*",
        f"- **Dirty (Local Changes)**: {'Yes' if status_data['git']['dirty'] else 'No'}",
        "",
        "## Wiki Knowledge Base Stats",
        f"- **Total Pages**: {status_data['wiki']['total_pages']}",
        f"- **Total Size**: {status_data['wiki']['total_size_kb']} KB",
        "- **Folders Page Count**:",
    ]
    for folder in status_data['wiki']['folders']:
        lines.append(f"  - `{folder['name']}/`: {folder['page_count']} pages")
    
    lines.append("")
    lines.append("## Recent Activity Log (changelog.md)")
    for entry in status_data['recent_changes']['changelog']:
        lines.append(f"- {entry}")
        
    lines.append("")
    lines.append("## Recently Modified Files on Disk")
    for file in status_data['recent_changes']['modified_files']:
        lines.append(f"- `wiki/{file['rel_path']}` (modified {file['ago']})")
        
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Write tools — GitHub-backed (optional; require GITHUB_TOKEN + GITHUB_REPO)
#
# Every write follows the same pattern:
#   1. Commit to GitHub (source of truth on Render)
#   2. Mirror to local wiki/ (dev working tree)
#   3. Patch WikiEngine indexes (_index_page / _deindex_page)
# ---------------------------------------------------------------------------

# Lazy import — server starts in read-only mode if writer.py is missing.
try:
    from writer import get_writer, GitHubWriteError  # type: ignore
except ImportError:  # pragma: no cover
    get_writer = None  # type: ignore
    GitHubWriteError = RuntimeError  # type: ignore

# Optional Notion mirror — server runs fine without it (no NOTION_TOKEN = no-op).
try:
    from notion_sync import (  # type: ignore
        push_file_if_configured,
        delete_file_if_configured,
        NotionSync,
        NotionSyncError,
    )
except ImportError:  # pragma: no cover
    push_file_if_configured = None  # type: ignore
    delete_file_if_configured = None  # type: ignore
    NotionSync = None  # type: ignore
    NotionSyncError = RuntimeError  # type: ignore


async def _maybe_notion_push(rel_path: str) -> str:
    """Best-effort Notion mirror after a GitHub write. Never blocks the write."""
    if push_file_if_configured is None:
        return ""
    try:
        msg = await push_file_if_configured(rel_path)
        return f"\nNotion: {msg}" if msg else ""
    except Exception as e:  # pragma: no cover - network/runtime guard
        log.warning("[notion] push failed for %s: %s", rel_path, e)
        return f"\nNotion: sync skipped ({e})"


def _rel_path_for(page: WikiPage) -> str:
    """Convert an absolute page path into a repo-relative POSIX path (wiki/folder/name.md)."""
    rel = page.path.relative_to(WIKI_DIR)
    return "wiki/" + rel.as_posix()


# ---------------------------------------------------------------------------
# Folder + index helpers (shared by page/folder write tools)
# ---------------------------------------------------------------------------

# LEARN: Folder validation stops path tricks like "../secrets" from reaching the file system.
_FOLDER_SEGMENT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_PROTECTED_FOLDERS = frozenset({"root"})


def _normalize_folder(folder: str) -> str:
    """Strip slashes and normalize separators to forward slashes."""
    return folder.strip().strip("/").replace("\\", "/")


def _validate_folder_path(folder: str) -> str | None:
    """Return an error message when folder is invalid, else None."""
    folder = _normalize_folder(folder)
    if not folder:
        return "folder path is required"
    if folder in _PROTECTED_FOLDERS:
        return f"'{folder}' is reserved — use a named subfolder (e.g. people, topics)"
    if ".." in folder.split("/"):
        return "folder path must not contain '..'"
    for segment in folder.split("/"):
        if not segment or not _FOLDER_SEGMENT_RE.match(segment):
            return (
                f"invalid folder segment '{segment}' — use lowercase letters, "
                "numbers, hyphens, underscores; each segment must start with a letter or digit"
            )
    return None


def _pages_in_folder(engine: WikiEngine, folder: str) -> list[WikiPage]:
    """All indexed pages whose folder matches exactly (not nested children)."""
    folder = _normalize_folder(folder)
    return [p for p in engine.pages.values() if p.folder == folder]


def _local_folder_exists(folder: str) -> bool:
    """True if wiki/<folder>/ exists on disk with any entries."""
    path = WIKI_DIR / _normalize_folder(folder)
    return path.is_dir() and any(path.iterdir())


def _index_page_in_engine(engine: WikiEngine, page: WikiPage) -> None:
    """Add or replace a page in name_map + inverted word index (mirrors _load logic)."""
    stem = page.stem
    engine.pages[stem] = page

    for name in [stem, page.title] + list(page.aliases or []):
        key = name.lower().strip()
        if not key:
            continue
        engine.name_map[key] = stem
        engine.name_map[key.replace(" ", "-")] = stem
        engine.name_map[key.replace("-", " ")] = stem

    words = set(re.findall(r"\b[a-z0-9]{2,}\b", page.body.lower()))
    title_words = set(re.findall(r"\b[a-z0-9]{2,}\b", page.title.lower()))
    stem_words = set(re.findall(r"\b[a-z0-9]{2,}\b", stem.replace("-", " ").lower()))
    tag_words: set[str] = set()
    for tag in page.tags or []:
        tag_words.update(re.findall(r"\b[a-z0-9]{2,}\b", str(tag).lower()))

    important = title_words | stem_words | tag_words
    engine.stem_words[stem] = important
    for word in words | important:
        engine.word_index[word].add(stem)


def _deindex_page_in_engine(engine: WikiEngine, page: WikiPage) -> None:
    """Remove a page from the in-memory index (pages, aliases, search tokens)."""
    stem = page.stem
    engine.pages.pop(stem, None)

    names = {stem, page.title, *list(page.aliases or [])}
    keys_to_drop = [k for k, v in engine.name_map.items() if v == stem]
    for key in keys_to_drop:
        engine.name_map.pop(key, None)
    for name in names:
        key = name.lower().strip()
        if engine.name_map.get(key) == stem:
            engine.name_map.pop(key, None)

    body_words = set(re.findall(r"\b[a-z0-9]{2,}\b", page.body.lower()))
    for word in body_words | engine.stem_words.pop(stem, set()):
        if stem in engine.word_index.get(word, set()):
            engine.word_index[word].discard(stem)
            if not engine.word_index[word]:
                engine.word_index.pop(word, None)


def _ensure_local_folder(folder: str) -> Path:
    """Create wiki/<folder>/ on disk if missing."""
    path = WIKI_DIR / _normalize_folder(folder)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _delete_local_folder(folder: str) -> None:
    """Remove wiki/<folder>/ from disk (best-effort)."""
    path = WIKI_DIR / _normalize_folder(folder)
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)


def _rename_local_folder(old_folder: str, new_folder: str) -> None:
    """Rename wiki/<old>/ to wiki/<new>/ on disk (best-effort)."""
    old_path = WIKI_DIR / _normalize_folder(old_folder)
    new_path = WIKI_DIR / _normalize_folder(new_folder)
    if old_path.is_dir():
        new_path.parent.mkdir(parents=True, exist_ok=True)
        if new_path.exists():
            shutil.rmtree(new_path, ignore_errors=True)
        old_path.rename(new_path)


def _require_writer():
    """Gate all write tools — returns (writer, None) or (None, error_message)."""
    if get_writer is None:
        return None, "Error: write tools not available (writer module missing)."
    try:
        return get_writer(), None
    except GitHubWriteError as e:
        return None, f"Error: {e}"


# --- Folder CRUD ---

@mcp_server.tool()
async def create_folder(
    folder: str,
    reason: str = "",
    create_index_page: bool = False,
    index_name: str = "",
    index_content: str = "",
    index_title: str = "",
    tags: list[str] | None = None,
) -> str:
    """Create a new wiki folder on GitHub (and local disk).

    Folders are path prefixes under wiki/. Git does not track empty directories,
    so by default a small .gitkeep marker file is committed.

    Args:
        folder:            Target folder path (e.g. 'notes', 'work/projects').
        reason:            Optional audit note for the Git commit message.
        create_index_page: When True, create the first .md page instead of .gitkeep.
        index_name:        Page stem when create_index_page=True (required then).
        index_content:     Markdown body for the index page (default: '# Index').
        index_title:       Optional frontmatter title for the index page.
        tags:              Optional frontmatter tags for the index page.

    Use create_page afterward to add more pages inside the folder.
    """
    writer, err = _require_writer()
    if err:
        return err

    folder = _normalize_folder(folder)
    if msg := _validate_folder_path(folder):
        return f"Error: {msg}"

    if create_index_page:
        index_name = index_name.strip().strip("/").removesuffix(".md")
        if not index_name:
            return "Error: index_name is required when create_index_page=True."

    try:
        result = await writer.create_wiki_folder(
            folder,
            index_name=index_name if create_index_page else None,
            index_body=index_content or "# Index\n",
            index_title=index_title or None,
            index_tags=tags,
            reason=reason,
        )
    except GitHubWriteError as e:
        return f"Error: {e}"

    _ensure_local_folder(folder)
    engine = _get_engine()

    if create_index_page:
        from writer import _build_page, extract_page_meta

        body = index_content or "# Index\n"
        pmeta = extract_page_meta(body)
        final_title = index_title or index_name.replace("-", " ").title()
        all_tags: list[str] = list(tags or []) + pmeta.get("hashtag_tags", [])
        all_tags = list(dict.fromkeys(all_tags))[:12]
        full_content = _build_page(body, index_title or None, all_tags or None)
        _write_local_copy(folder, index_name, full_content)

        page = WikiPage(
            path=WIKI_DIR / folder / f"{index_name}.md",
            stem=index_name,
            folder=folder,
            title=final_title,
            aliases=[],
            tags=all_tags,
            content=full_content,
            body=body,
            size=len(full_content),
            frontmatter={"title": final_title, "tags": all_tags},
            wikilinks=pmeta["wikilinks"],
        )
        _index_page_in_engine(engine, page)
        commit_url = result.get("commit", {}).get("html_url", "")
        return f"Created folder `{folder}/` with index page `{index_name}.md`\n{commit_url}"

    marker = WIKI_DIR / folder / ".gitkeep"
    marker.write_text("# Wiki folder placeholder\n", encoding="utf-8")
    commit_url = result.get("commit", {}).get("html_url", "")
    return f"Created empty folder `{folder}/` (.gitkeep marker)\n{commit_url}"


@mcp_server.tool()
async def delete_folder(folder: str, reason: str) -> str:
    """Delete a wiki folder and every file inside it (pages, markers, nested paths).

    Args:
        folder: Folder path to remove (e.g. 'drafts', 'archive/old').
        reason: Required audit reason (included in Git commit messages).

    This is destructive — all pages in the folder are permanently removed from GitHub.
    """
    if not reason or not reason.strip():
        return "Error: reason is required for folder deletion."

    writer, err = _require_writer()
    if err:
        return err

    folder = _normalize_folder(folder)
    if msg := _validate_folder_path(folder):
        return f"Error: {msg}"

    engine = _get_engine()
    pages = _pages_in_folder(engine, folder)
    page_count = len(pages)

    try:
        deleted_paths = await writer.delete_wiki_folder(folder, reason=reason)
    except GitHubWriteError as e:
        return f"Error: {e}"

    for page in pages:
        _deindex_page_in_engine(engine, page)

    _delete_local_folder(folder)

    lines = [
        f"Deleted folder `{folder}/` — {reason.strip()}",
        f"- {len(deleted_paths)} file(s) removed from GitHub",
        f"- {page_count} page(s) removed from in-memory index",
    ]
    return "\n".join(lines)


@mcp_server.tool()
async def rename_folder(old_folder: str, new_folder: str, reason: str = "") -> str:
    """Rename or move a wiki folder by relocating every file inside it.

    Args:
        old_folder: Current folder path (e.g. 'drafts').
        new_folder: Destination folder path (e.g. 'archive/drafts').
        reason:     Optional audit note for Git commit messages.

    All pages keep their filenames; only the folder prefix changes.
    """
    writer, err = _require_writer()
    if err:
        return err

    old_folder = _normalize_folder(old_folder)
    new_folder = _normalize_folder(new_folder)
    for label, path in (("old_folder", old_folder), ("new_folder", new_folder)):
        if msg := _validate_folder_path(path):
            return f"Error: invalid {label} — {msg}"

    if old_folder == new_folder:
        return "Error: old_folder and new_folder are the same."

    engine = _get_engine()
    pages = _pages_in_folder(engine, old_folder)
    if not pages and not _local_folder_exists(old_folder):
        return f"Error: source folder not found: {old_folder}"

    try:
        moved = await writer.rename_wiki_folder(old_folder, new_folder, reason=reason)
    except GitHubWriteError as e:
        return f"Error: {e}"

    for page in pages:
        _deindex_page_in_engine(engine, page)
        new_path = WIKI_DIR / new_folder / f"{page.stem}.md"
        page.path = new_path
        page.folder = new_folder
        _index_page_in_engine(engine, page)

    _rename_local_folder(old_folder, new_folder)

    return (
        f"Renamed folder `{old_folder}/` → `{new_folder}/` "
        f"({len(moved)} file(s) moved on GitHub, {len(pages)} page(s) re-indexed)"
    )


# --- Page CRUD ---

@mcp_server.tool()
async def create_page(
    folder: str,
    name: str,
    content: str,
    title: str = "",
    tags: list[str] | None = None,
) -> str:
    """Create a new wiki page. Commits to GitHub and updates the in-memory index.

    The parent folder is created automatically if it does not exist.
    For an empty folder with no pages yet, use create_folder first (optional).

    Args:
        folder:  Target folder path (e.g. 'people', 'topics', 'work/notes').
        name:    kebab-case file stem (no .md extension), e.g. 'new-friend'.
        content: Markdown body (without frontmatter).
        title:   Optional frontmatter title (defaults to prettified name).
        tags:    Optional frontmatter tags.

    Returns a confirmation string with the commit URL.
    """
    writer, err = _require_writer()
    if err:
        return err

    name = name.strip().strip("/").removesuffix(".md")
    folder = _normalize_folder(folder)
    if not name:
        return "Error: name is required."
    if msg := _validate_folder_path(folder):
        return f"Error: {msg}"

    try:
        result = await writer.create_wiki_page(
            folder=folder,
            name=name,
            content=content,
            title=title or None,
            tags=tags or None,
        )
    except GitHubWriteError as e:
        return f"Error: {e}"

    # Update in-memory engine so subsequent reads in this session see it.
    engine = _get_engine()
    new_path = WIKI_DIR / folder / f"{name}.md"

    # Extract metadata from body for immediate in-memory use
    from writer import _build_page, extract_page_meta
    pmeta = extract_page_meta(content)
    final_title = title or name.replace("-", " ").title()
    all_tags: list[str] = list(tags or []) + pmeta.get("hashtag_tags", [])
    all_tags = list(dict.fromkeys(all_tags))[:12]

    full_content = _build_page(content, title or None, all_tags or None)
    _write_local_copy(folder, name, full_content)

    page = WikiPage(
        path=new_path,
        stem=name,
        folder=folder,
        title=final_title,
        aliases=[],
        tags=all_tags,
        content=full_content,
        body=content,
        size=len(full_content),
        frontmatter={
            "title": final_title,
            "tags": all_tags,
            "word_count": str(pmeta["word_count"]),
            "char_count": str(pmeta["char_count"]),
            "heading_count": str(pmeta["heading_count"]),
        },
        wikilinks=pmeta["wikilinks"],
    )
    _index_page_in_engine(engine, page)

    commit_url = result.get("commit", {}).get("html_url", "")
    rel_path = f"wiki/{folder}/{name}.md"
    notion_note = await _maybe_notion_push(rel_path)
    return f"Created {rel_path}\n{commit_url}{notion_note}"


@mcp_server.tool()
async def update_page(name: str, content: str, reason: str = "") -> str:
    """Replace the entire content of an existing page.

    Args:
        name:    page stem or alias (e.g. 'profile').
        content: new full markdown content.
        reason:  short reason for the edit (included in commit message).
    """
    if get_writer is None:
        return "Error: write tools not available."
    try:
        writer = get_writer()
    except GitHubWriteError as e:
        return f"Error: {e}"

    engine = _get_engine()
    page = engine.get_page(name)
    if not page:
        return f"Page not found: {name}"

    rel_path = _rel_path_for(page)
    try:
        result = await writer.update_wiki_page(rel_path, content, reason=reason)
    except GitHubWriteError as e:
        if "not found" in str(e).lower():
            # Already deleted on GitHub, prune locally
            engine.pages.pop(page.stem, None)
            engine.name_map.pop(page.stem.lower(), None)
            _delete_local_copy(page.folder, page.stem)
            return f"Error: Page was not found on GitHub (it may have been deleted externally). Cleaned up local index."
        return f"Error: {e}"

    # Re-parse frontmatter for proper in-memory state
    fm, body = WikiEngine._parse_frontmatter(content)
    page.body = body
    page.content = content
    page.size = len(content)
    page.frontmatter = fm
    if fm.get("title"):
        page.title = fm["title"]
    if fm.get("tags"):
        page.tags = fm["tags"] if isinstance(fm["tags"], list) else [fm["tags"]]

    # Local sync
    _write_local_copy(page.folder, page.stem, content)

    commit_url = result.get("commit", {}).get("html_url", "")
    notion_note = await _maybe_notion_push(rel_path)
    return f"Updated {rel_path}\n{commit_url}{notion_note}"


@mcp_server.tool()
async def append_to_page(name: str, content: str, heading: str = "") -> str:
    """Append content to an existing page, optionally under a heading.

    Args:
        name:    page stem or alias.
        content: text/markdown to append.
        heading: optional heading text (case-insensitive, no leading '#').
                 If found, content is inserted at the end of that section.
                 If not found, a new `## heading` section is created at the end.
                 If empty, content is appended at the very end.
    """
    if get_writer is None:
        return "Error: write tools not available."
    try:
        writer = get_writer()
    except GitHubWriteError as e:
        return f"Error: {e}"

    engine = _get_engine()
    page = engine.get_page(name)
    if not page:
        return f"Page not found: {name}"

    rel_path = _rel_path_for(page)
    try:
        result = await writer.append_to_wiki_page(rel_path, content, heading=heading or None)
    except GitHubWriteError as e:
        return f"Error: {e}"

    # Refresh in-memory body from the fresh commit
    new_body = result.get("content", {}).get("content")  # base64, optional
    if isinstance(new_body, str):
        try:
            page.body = base64.b64decode(new_body).decode("utf-8")
            page.content = page.body
            page.size = len(page.body)
        except Exception:
            pass

    # Also write local copy if we decoded new content
    if page.body:
        _write_local_copy(page.folder, page.stem, page.content)

    commit_url = result.get("commit", {}).get("html_url", "")
    target = f"under '{heading}'" if heading else "at end"
    notion_note = await _maybe_notion_push(rel_path)
    return f"Appended to {rel_path} ({target})\n{commit_url}{notion_note}"


@mcp_server.tool()
async def delete_page(name: str, reason: str) -> str:
    """Delete a wiki page. A reason is required for the audit log.

    Args:
        name:   page stem or alias.
        reason: why the page is being deleted (required).
    """
    if not reason or not reason.strip():
        return "Error: reason is required for delete operations."
    if get_writer is None:
        return "Error: write tools not available."
    try:
        writer = get_writer()
    except GitHubWriteError as e:
        return f"Error: {e}"

    engine = _get_engine()
    page = engine.get_page(name)
    if not page:
        return f"Page not found: {name}"

    rel_path = _rel_path_for(page)
    try:
        await writer.delete_wiki_page(rel_path, reason=reason)
    except GitHubWriteError as e:
        return f"Error: {e}"

    _deindex_page_in_engine(engine, page)
    _delete_local_copy(page.folder, page.stem)

    notion_note = ""
    if delete_file_if_configured is not None:
        try:
            msg = await delete_file_if_configured(rel_path)
            if msg:
                notion_note = f"\nNotion: {msg}"
        except Exception as e:  # pragma: no cover - network/runtime guard
            log.warning("[notion] delete archive failed for %s: %s", rel_path, e)
            notion_note = f"\nNotion: archive skipped ({e})"

    return f"Deleted {rel_path} — {reason}{notion_note}"


# ---------------------------------------------------------------------------
# Local disk mirror — keeps wiki/ in sync during dev (Render FS is ephemeral)
# ---------------------------------------------------------------------------

def _write_local_copy(folder: str, name: str, content: str) -> None:
    """Mirror a GitHub write to the local wiki/ tree (best-effort, non-fatal)."""
    try:
        local_path = WIKI_DIR / folder / f"{name}.md"
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_text(content, encoding="utf-8")
    except Exception as exc:
        log.warning("[local-sync] failed to write %s/%s.md: %s", folder, name, exc)


def _delete_local_copy(folder: str, name: str) -> None:
    """Remove a local wiki page file."""
    try:
        local_path = WIKI_DIR / folder / f"{name}.md"
        if local_path.exists():
            local_path.unlink()
    except Exception as exc:
        log.warning("[local-sync] failed to delete %s/%s.md: %s", folder, name, exc)


def _git_pull() -> str:
    """Run `git pull` in the repo root. Returns output."""
    repo_root = WIKI_DIR.parent  # wiki is at <repo>/wiki
    try:
        result = subprocess.run(
            ["git", "pull", "--ff-only", "origin", "master"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=30,
        )
        out = (result.stdout + result.stderr).strip()
        if result.returncode != 0:
            return f"git pull failed (rc={result.returncode}): {out}"
        return out
    except FileNotFoundError:
        return "git not found on PATH"
    except subprocess.TimeoutExpired:
        return "git pull timed out (30s)"
    except Exception as exc:
        return f"git pull error: {exc}"


# --- Rename / Move ---

@mcp_server.tool()
async def rename_page(
    name: str,
    new_name: str = "",
    new_folder: str = "",
    reason: str = "",
) -> str:
    """Rename a wiki page and/or move it to a different folder.

    This creates the new file, copies the content, deletes the old file,
    and updates the in-memory index — all in one operation.

    Args:
        name:       current page stem or alias.
        new_name:   new file stem (kebab-case, no .md). Empty = keep same name.
        new_folder: destination folder. Empty = keep same folder.
        reason:     short reason for the rename (included in commit message).
    """
    if get_writer is None:
        return "Error: write tools not available."
    if not new_name and not new_folder:
        return "Error: provide new_name, new_folder, or both."
    try:
        writer = get_writer()
    except GitHubWriteError as e:
        return f"Error: {e}"

    engine = _get_engine()
    page = engine.get_page(name)
    if not page:
        return f"Page not found: {name}"

    old_rel = _rel_path_for(page)
    dst_folder = _normalize_folder(new_folder or page.folder)
    dst_name = (new_name.strip().strip("/").removesuffix(".md") or page.stem)
    if msg := _validate_folder_path(dst_folder):
        return f"Error: {msg}"
    new_rel = f"wiki/{dst_folder}/{dst_name}.md"

    if old_rel == new_rel:
        return "Error: source and destination are the same."

    # Check destination doesn't already exist
    existing = await writer.get_file(new_rel)
    if existing is not None:
        return f"Error: destination already exists: {new_rel}"

    # Read current content from GitHub (authoritative)
    current = await writer.get_file(old_rel)
    if current is None:
        return f"Error: source not found on GitHub: {old_rel}"
    content, old_sha = current

    # Create new file
    msg = f"chatgpt: rename {old_rel} → {new_rel}"
    if reason:
        msg += f" — {reason[:120]}"
    await writer.put_file(new_rel, content, msg)

    # Delete old file
    await writer.delete_file(old_rel, f"chatgpt: cleanup old path after rename to {new_rel}", old_sha)

    _deindex_page_in_engine(engine, page)

    fm, body = WikiEngine._parse_frontmatter(content)
    title = fm.get("title", dst_name.replace("-", " ").title())
    aliases = fm.get("aliases", [])
    if isinstance(aliases, str):
        aliases = [aliases]
    tags = fm.get("tags", [])
    if isinstance(tags, str):
        tags = [tags]
    wikilinks = [m.group(1).strip() for m in _WIKILINK_RE.finditer(body)]

    new_page = WikiPage(
        path=WIKI_DIR / dst_folder / f"{dst_name}.md",
        stem=dst_name, folder=dst_folder,
        title=title, aliases=aliases, tags=tags,
        content=content, body=body, size=len(content),
        frontmatter=fm,
        wikilinks=wikilinks,
    )
    _index_page_in_engine(engine, new_page)

    # Local sync
    _write_local_copy(dst_folder, dst_name, content)
    _delete_local_copy(page.folder, page.stem)

    return f"Renamed {old_rel} → {new_rel}"


@mcp_server.tool()
async def move_page(name: str, new_folder: str, reason: str = "") -> str:
    """Move a wiki page to a different folder (shortcut for rename_page).

    Args:
        name:       current page stem or alias.
        new_folder: destination folder (e.g. 'personal', 'people').
        reason:     short reason for the move.
    """
    return await rename_page(name=name, new_folder=new_folder, reason=reason)


# --- Admin tools ---

@mcp_server.tool()
def list_folders() -> str:
    """List all wiki folders with page counts."""
    engine = _get_engine()
    folders = engine.get_folders()
    lines = [f"**{len(folders)} folders** · {sum(folders.values())} total pages\n"]
    for f, c in sorted(folders.items()):
        lines.append(f"- **{f}/** — {c} pages")
    return "\n".join(lines)


# --- Notion sync tools (optional; require NOTION_TOKEN + NOTION_DATABASE_ID) ---

@mcp_server.tool()
async def notion_sync_push(all_pages: bool = True) -> str:
    """Mirror wiki markdown into Notion (needs NOTION_TOKEN + NOTION_DATABASE_ID).

    create_page / update_page / append_to_page already auto-push the one file
    they touch. Use this to (re)push every page, e.g. for a first full sync.

    Args:
        all_pages: push every wiki file (default True).
    """
    if NotionSync is None:
        return "Error: notion_sync module not available."
    try:
        sync = NotionSync()
        if all_pages:
            lines = await sync.push_all()
            return "\n".join(lines) if lines else "No wiki files to push."
        return "Error: set all_pages=true, or just create_page (it auto-syncs one file)."
    except NotionSyncError as e:
        return f"Error: {e}"


@mcp_server.tool()
async def notion_sync_pull() -> str:
    """Pull edits made in Notion back into the GitHub wiki (needs NOTION_TOKEN + GITHUB_TOKEN)."""
    if NotionSync is None:
        return "Error: notion_sync module not available."
    try:
        sync = NotionSync()
        lines = await sync.pull_all()
        return "\n".join(lines) if lines else "Nothing to pull."
    except NotionSyncError as e:
        return f"Error: {e}"


@mcp_server.tool()
async def notion_sync_status() -> str:
    """Show Notion sync configuration and how many pages are mapped."""
    if NotionSync is None:
        return "Error: notion_sync module not available."
    sync = NotionSync()
    return json.dumps(sync.status(), indent=2)


# ---------------------------------------------------------------------------
# Entry point — stdio (local) or streamable-http / sse (cloud)
# LEARN: Running "python server.py" directly starts this block.
#        Cursor instead runs the same file but connects via stdin/stdout (stdio).
# ---------------------------------------------------------------------------

def _load_dotenv() -> None:
    """Load KEY=VALUE pairs from a repo-root .env (local dev). No-op on Render."""
    env_path = _REPO_ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(name)s  %(message)s")
    _load_dotenv()

    # Optional: pull latest wiki from git before indexing (local dev only)
    if os.environ.get("WIKI_AUTO_PULL", "").strip() in ("1", "true", "yes"):
        log.info("[auto-pull] pulling latest from GitHub...")
        pull_result = _git_pull()
        log.info("[auto-pull] %s", pull_result)

    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if "--sse" in sys.argv:
        transport = "sse"
    if "--http" in sys.argv:
        transport = "streamable-http"

    if transport in ("sse", "streamable-http"):
        api_key = os.environ.get("MCP_API_KEY")
        if api_key:
            log.info("[AUTH] API key auth ENABLED — token required on all /mcp requests")
            # Monkey-patch FastMCP app factories to inject auth middleware into uvicorn.
            _orig_http = mcp_server.streamable_http_app
            _orig_sse = mcp_server.sse_app

            def _secured_http(*args, **kwargs):
                app = _orig_http(*args, **kwargs)
                return APIKeyMiddleware(app, api_key)

            def _secured_sse(*args, **kwargs):
                app = _orig_sse(*args, **kwargs)
                return APIKeyMiddleware(app, api_key)

            mcp_server.streamable_http_app = _secured_http  # type: ignore[method-assign]
            mcp_server.sse_app = _secured_sse              # type: ignore[method-assign]
        else:
            log.warning("[AUTH] MCP_API_KEY not set — server is PUBLIC. Set it on Render!")

    mcp_server.run(transport=transport)
