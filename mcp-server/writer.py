"""
GitHub-backed writer — persists wiki changes via the Contents API.

NEW HERE? Read docs/LEARNING.md first. Search this file for "# LEARN:" comments.

WHY THIS FILE EXISTS:
  On your laptop, create_page can write straight to wiki/ on disk.
  On Render (cloud), the disk is wiped on restart — so writes go to GitHub instead.
  GitHub stores the real copy; Render rebuilds from git on deploy.

SKIP IF: You only use Wiki Brain locally with Cursor (read-only works without this).

Env vars: GITHUB_TOKEN, GITHUB_REPO, GITHUB_BRANCH — see .env.example

Layers (read top to bottom):
  1. get_file / put_file / delete_file  — raw GitHub HTTP calls
  2. create_wiki_page / update_…        — one markdown file at a time
  3. create_wiki_folder / delete_…      — move or remove whole folders
"""

from __future__ import annotations

import base64
import os
import re
import httpx
from datetime import datetime, timezone


class GitHubWriteError(RuntimeError):
    """Raised when a GitHub write call fails."""


class GitHubWriter:
    """Async wrapper around GitHub Contents API for wiki file operations."""

    def __init__(self, token: str, repo: str, branch: str = "master"):
        if not token:
            raise ValueError("GITHUB_TOKEN is required")
        self.branch = branch
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "wiki-brain-mcp",
        }
        self.base = f"https://api.github.com/repos/{repo}"

    # -------------------------------------------------------------------
    # Low-level GitHub API
    # LEARN: Every file on GitHub has a SHA (version id). You must send the SHA
    #        when updating or deleting, or GitHub rejects the change.
    # -------------------------------------------------------------------

    async def get_file(self, path: str) -> tuple[str, str] | None:
        """Fetch file text + SHA. SHA is required for updates/deletes (optimistic locking)."""
        async with httpx.AsyncClient() as client:
            r = await client.get(
                f"{self.base}/contents/{path}",
                params={"ref": self.branch},
                headers=self.headers,
                timeout=20,
            )
            if r.status_code == 404:
                return None
            if r.status_code >= 400:
                raise GitHubWriteError(f"GET {path} -> {r.status_code}: {r.text[:200]}")
            data = r.json()
            content = base64.b64decode(data["content"]).decode("utf-8")
            return content, data["sha"]

    async def put_file(
        self,
        path: str,
        content: str,
        message: str,
        sha: str | None = None,
    ) -> dict:
        """Create (no sha) or update (with sha) a file. Content is base64-encoded for the API."""
        body = {
            "message": message,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "branch": self.branch,
        }
        if sha:
            body["sha"] = sha
        async with httpx.AsyncClient() as client:
            r = await client.put(
                f"{self.base}/contents/{path}",
                headers=self.headers,
                json=body,
                timeout=30,
            )
            if r.status_code >= 400:
                raise GitHubWriteError(f"PUT {path} -> {r.status_code}: {r.text[:300]}")
            return r.json()

    async def delete_file(self, path: str, message: str, sha: str) -> dict:
        """Remove a file. GitHub requires the current SHA to prevent accidental overwrites."""
        async with httpx.AsyncClient() as client:
            r = await client.request(
                "DELETE",
                f"{self.base}/contents/{path}",
                headers=self.headers,
                json={"message": message, "branch": self.branch, "sha": sha},
                timeout=20,
            )
            if r.status_code >= 400:
                raise GitHubWriteError(f"DELETE {path} -> {r.status_code}: {r.text[:300]}")
            return r.json()

    # -------------------------------------------------------------------
    # High-level wiki helpers (called by server.py MCP tools)
    # LEARN: Paths always look like wiki/people/jordan-friend.md (repo-relative).
    # -------------------------------------------------------------------

    async def create_wiki_page(
        self,
        folder: str,
        name: str,
        content: str,
        title: str | None = None,
        tags: list[str] | None = None,
    ) -> dict:
        """Create a new wiki page under wiki/<folder>/<name>.md with optional frontmatter."""
        folder = folder.strip("/ ")
        name = name.strip(" /.md")
        if not name or "/" in name or "\\" in name or ".." in folder.split("/") + [name]:
            raise GitHubWriteError(f"Invalid page path: {folder}/{name}")
        path = f"wiki/{folder}/{name}.md"

        # Abort if exists
        existing = await self.get_file(path)
        if existing is not None:
            raise GitHubWriteError(f"Page already exists: {path} (use update_page)")

        # Build content with optional frontmatter
        full = _build_page(content, title, tags)

        return await self.put_file(path, full, f"chatgpt: create {folder}/{name}")

    async def update_wiki_page(
        self,
        rel_path: str,
        new_content: str,
        reason: str = "",
    ) -> dict:
        """Replace a wiki page entirely. rel_path is relative to repo root, e.g. wiki/people/jordan-friend.md."""
        existing = await self.get_file(rel_path)
        if existing is None:
            raise GitHubWriteError(f"Page not found on GitHub: {rel_path}")
        current_text, sha = existing
        # Preserve frontmatter block but refresh updated_at + body metrics
        if current_text.startswith("---"):
            updated_fm = _update_meta_on_write(current_text, new_content)
            # Splice new body after frontmatter
            parts = updated_fm.split("---", 2)
            if len(parts) >= 3:
                new_content = "---" + parts[1] + "---\n" + new_content.lstrip()
        msg = f"chatgpt: update {rel_path}"
        if reason:
            msg += f" — {reason[:120]}"
        return await self.put_file(rel_path, new_content, msg, sha=sha)

    async def append_to_wiki_page(
        self,
        rel_path: str,
        content: str,
        heading: str | None = None,
    ) -> dict:
        """Append content to an existing wiki page.

        If `heading` is given, the content is inserted at the end of that section.
        If the heading doesn't exist, a new ## heading is created at the end.
        Otherwise content is appended at the very end.
        """
        existing = await self.get_file(rel_path)
        if existing is None:
            raise GitHubWriteError(f"Page not found on GitHub: {rel_path}")
        current, sha = existing
        new_content = _append_under_heading(current, content, heading)
        # Refresh updated_at + metrics in frontmatter
        if new_content.startswith("---"):
            new_content = _update_meta_on_write(new_content, new_content)
        msg = f"chatgpt: append to {rel_path}"
        if heading:
            msg += f" ({heading[:50]})"
        return await self.put_file(rel_path, new_content, msg, sha=sha)

    async def delete_wiki_page(self, rel_path: str, reason: str) -> dict:
        existing = await self.get_file(rel_path)
        if existing is None:
            raise GitHubWriteError(f"Page not found on GitHub: {rel_path}")
        _, sha = existing
        return await self.delete_file(
            rel_path,
            f"chatgpt: delete {rel_path} — {reason[:120]}",
            sha=sha,
        )

    # -------------------------------------------------------------------
    # Folder operations
    # LEARN: Git doesn't have empty folders — we use a .gitkeep file as a placeholder,
    #        or create a real .md page inside the folder.
    # -------------------------------------------------------------------

    async def list_directory(self, dir_path: str) -> list[dict]:
        """List immediate children under a repo path. Returns [] when missing."""
        dir_path = dir_path.strip("/")
        async with httpx.AsyncClient() as client:
            r = await client.get(
                f"{self.base}/contents/{dir_path}",
                params={"ref": self.branch},
                headers=self.headers,
                timeout=20,
            )
            if r.status_code == 404:
                return []
            if r.status_code >= 400:
                raise GitHubWriteError(f"LIST {dir_path} -> {r.status_code}: {r.text[:200]}")
            data = r.json()
            return [data] if isinstance(data, dict) else data

    async def _collect_files_recursive(self, dir_path: str) -> list[tuple[str, str]]:
        """Walk a directory tree — needed because folder delete/rename touches every file."""
        dir_path = dir_path.strip("/")
        found: list[tuple[str, str]] = []
        for item in await self.list_directory(dir_path):
            item_type = item.get("type")
            item_path = item.get("path", "")
            if item_type == "file" and item_path and item.get("sha"):
                found.append((item_path, item["sha"]))
            elif item_type == "dir" and item_path:
                found.extend(await self._collect_files_recursive(item_path))
        return found

    async def create_wiki_folder(
        self,
        folder: str,
        *,
        index_name: str | None = None,
        index_body: str | None = None,
        index_title: str | None = None,
        index_tags: list[str] | None = None,
        reason: str = "",
    ) -> dict:
        """Create wiki/<folder>/ on GitHub.

        Without index_name: writes a .gitkeep marker so Git tracks the folder.
        With index_name: creates the first markdown page (folder path is implicit).
        """
        folder = folder.strip("/ ")
        base = f"wiki/{folder}"
        if await self.list_directory(base):
            raise GitHubWriteError(f"Folder already exists: {folder}/")

        msg = f"wiki: create folder {folder}"
        if reason:
            msg += f" — {reason[:120]}"

        if index_name:
            return await self.create_wiki_page(
                folder=folder,
                name=index_name.strip().removesuffix(".md"),
                content=index_body or "# Index\n",
                title=index_title,
                tags=index_tags,
            )

        marker_path = f"{base}/.gitkeep"
        return await self.put_file(marker_path, "# Wiki folder placeholder\n", msg)

    async def delete_wiki_folder(self, folder: str, reason: str) -> list[str]:
        """Delete every file under wiki/<folder>/ (pages, markers, nested paths)."""
        folder = folder.strip("/ ")
        base = f"wiki/{folder}"
        files = await self._collect_files_recursive(base)
        if not files:
            raise GitHubWriteError(f"Folder not found or already empty: {folder}")

        deleted: list[str] = []
        msg = f"wiki: delete folder {folder} — {reason[:120]}"
        for path, sha in files:
            await self.delete_file(path, msg, sha)
            deleted.append(path)
        return deleted

    async def rename_wiki_folder(
        self,
        old_folder: str,
        new_folder: str,
        reason: str = "",
    ) -> list[tuple[str, str]]:
        """Move all files from wiki/<old>/ to wiki/<new>/ (preserves filenames)."""
        old_folder = old_folder.strip("/ ")
        new_folder = new_folder.strip("/ ")
        old_base = f"wiki/{old_folder}"
        new_base = f"wiki/{new_folder}"

        if await self.list_directory(new_base):
            raise GitHubWriteError(f"Destination folder already exists: {new_folder}/")

        files = await self._collect_files_recursive(old_base)
        if not files:
            raise GitHubWriteError(f"Source folder not found: {old_folder}")

        msg = f"wiki: rename folder {old_folder} → {new_folder}"
        if reason:
            msg += f" — {reason[:120]}"

        moved: list[tuple[str, str]] = []
        for old_path, _ in files:
            fetched = await self.get_file(old_path)
            if fetched is None:
                continue
            content, old_sha = fetched
            rel = old_path[len(old_base):].lstrip("/")
            new_path = f"{new_base}/{rel}"
            await self.put_file(new_path, content, msg)
            await self.delete_file(
                old_path,
                f"wiki: cleanup after folder rename to {new_folder}",
                old_sha,
            )
            moved.append((old_path, new_path))
        return moved


# ------------------------------------------------------------------------
# Frontmatter builders — auto-inject title, tags, timestamps, body metrics
# ------------------------------------------------------------------------

_WIKILINK_RE    = re.compile(r"\[\[([^\]\|#]+?)(?:\#[^\]\|]+)?(?:\|[^\]]+)?\]\]")
_HASHTAG_RE     = re.compile(r"(?<!\w)#([a-zA-Z][a-zA-Z0-9_-]{1,40})")  # #hashtag in body → tag
_HEADING_RE     = re.compile(r"^#{1,6}\s+(.+)", re.MULTILINE)
_CODE_FENCE_RE  = re.compile(r"```[\s\S]*?```")


def extract_page_meta(content: str) -> dict:
    """Scan markdown body for stats stored in YAML frontmatter on write.

    Returns a dict with:
        word_count   — number of words in body
        char_count   — character count (excl. whitespace)
        wikilinks    — unique resolved [[targets]]
        hashtag_tags — unique #tags found in body
        summary      — first meaningful sentence / heading
        heading_count — number of headings
    """
    stripped = _CODE_FENCE_RE.sub("", content)  # don't count code blocks as prose

    word_count = len(re.findall(r"\b\w+\b", stripped))
    char_count = len(re.sub(r"\s", "", stripped))

    wikilinks = list(dict.fromkeys(
        m.group(1).strip() for m in _WIKILINK_RE.finditer(content)
    ))

    hashtag_tags = list(dict.fromkeys(
        m.group(1).lower() for m in _HASHTAG_RE.finditer(stripped)
    ))

    heading_count = len(_HEADING_RE.findall(content))

    # First non-empty, non-heading, non-code line as summary
    summary = ""
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("```") or line.startswith("---"):
            continue
        # Strip markdown noise for a clean summary
        clean = re.sub(r"[*_`\[\]()#>~]", "", line).strip()
        if len(clean) > 12:
            summary = clean[:200]
            break

    return {
        "word_count":    word_count,
        "char_count":    char_count,
        "wikilinks":     wikilinks,
        "hashtag_tags":  hashtag_tags,
        "heading_count": heading_count,
        "summary":       summary,
    }


def _build_page(
    content: str,
    title: str | None,
    tags: list[str] | None,
) -> str:
    # LEARN: When you create a page, you usually pass just the markdown body.
    #        This function wraps it with --- frontmatter --- automatically.
    """Assemble a wiki page with auto-generated rich frontmatter.

    Always injects:
        title, tags, created_at, updated_at, word_count, wikilinks, summary

    Hashtag-tags found in the body are merged into the tag list.
    """
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    meta = extract_page_meta(content)

    # Merge explicit tags + hashtag tags, deduplicate, max 12
    merged_tags: list[str] = list(tags or [])
    for ht in meta.get("hashtag_tags", []):
        if ht not in merged_tags:
            merged_tags.append(ht)
    merged_tags = merged_tags[:12]

    # Always write frontmatter
    fm_lines = ["---"]
    if title:
        safe_title = title.replace('"', '\\"')
        fm_lines.append(f'title: "{safe_title}"')
    if merged_tags:
        tag_list = ", ".join(f'"{t}"' for t in merged_tags)
        fm_lines.append(f"tags: [{tag_list}]")

    fm_lines.append(f"created_at: {now_iso}")
    fm_lines.append(f"updated_at: {now_iso}")

    if meta:
        fm_lines.append(f"word_count: {meta['word_count']}")
        fm_lines.append(f"char_count: {meta['char_count']}")
        fm_lines.append(f"heading_count: {meta['heading_count']}")
        if meta["wikilinks"]:
            wl_list = ", ".join(f'"{w}"' for w in meta["wikilinks"][:20])
            fm_lines.append(f"wikilinks: [{wl_list}]")
        if meta["summary"]:
            safe_summary = meta["summary"].replace('"', '\\"')
            fm_lines.append(f'summary: "{safe_summary}"')

    fm_lines.append("---\n")
    return "\n".join(fm_lines) + content.rstrip() + "\n"


def _update_meta_on_write(current_fm_text: str, new_body: str) -> str:
    """Re-inject updated_at + recalculated metrics into existing frontmatter.

    Called when updating an existing page — preserves created_at, title, tags,
    but refreshes updated_at + body metrics.
    """
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    meta = extract_page_meta(new_body)

    def _set_fm_field(text: str, key: str, value: str) -> str:
        pattern = re.compile(rf"^({re.escape(key)}:\s*).*$", re.MULTILINE)
        repl = rf"\g<1>{value}"
        if pattern.search(text):
            return pattern.sub(repl, text)
        # Field missing — insert before closing ---
        return re.sub(r"(\n---\n)", f"\n{key}: {value}\\1", text, count=1)

    text = current_fm_text
    text = _set_fm_field(text, "updated_at", now_iso)
    text = _set_fm_field(text, "word_count", str(meta["word_count"]))
    text = _set_fm_field(text, "char_count",  str(meta["char_count"]))
    text = _set_fm_field(text, "heading_count", str(meta["heading_count"]))
    if meta["wikilinks"]:
        wl_list = "[" + ", ".join(f'"{w}"' for w in meta["wikilinks"][:20]) + "]"
        text = _set_fm_field(text, "wikilinks", wl_list)
    return text


def _append_under_heading(current: str, new_text: str, heading: str | None) -> str:
    if not heading:
        return current.rstrip() + "\n\n" + new_text.rstrip() + "\n"

    lines = current.splitlines()
    target = heading.lower().lstrip("# ").strip()

    insert_at = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#"):
            heading_text = stripped.lstrip("#").strip().lower()
            if heading_text == target:
                # Find end of this section (next heading or EOF)
                j = i + 1
                while j < len(lines) and not lines[j].lstrip().startswith("#"):
                    j += 1
                insert_at = j
                break

    if insert_at is not None:
        new_lines = lines[:insert_at] + ["", new_text.rstrip(), ""] + lines[insert_at:]
        return "\n".join(new_lines).rstrip() + "\n"

    # Heading not found — create new section at end
    return current.rstrip() + f"\n\n## {heading}\n\n" + new_text.rstrip() + "\n"


# ------------------------------------------------------------------------
# Singleton — one writer per process, credentials read from env on first use
# ------------------------------------------------------------------------

_writer: GitHubWriter | None = None


def get_writer() -> GitHubWriter:
    """Lazy-init GitHubWriter from GITHUB_TOKEN / GITHUB_REPO / GITHUB_BRANCH."""
    global _writer
    if _writer is None:
        token = os.environ.get("GITHUB_TOKEN", "").strip()
        repo = os.environ.get("GITHUB_REPO", "")
        branch = os.environ.get("GITHUB_BRANCH", "master")
        if not token:
            raise GitHubWriteError(
                "GITHUB_TOKEN not set — write tools disabled. "
                "Create a fine-grained PAT with Contents: read+write on the wiki repo."
            )
        if not repo or "/" not in repo:
            raise GitHubWriteError(
                "GITHUB_REPO not set — use owner/repo (e.g. your-org/wiki-brain). "
                "See docs/GITHUB.md."
            )
        _writer = GitHubWriter(token=token, repo=repo, branch=branch)
    return _writer
