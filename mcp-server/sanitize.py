"""
sanitize.py — privacy firewall at the MCP output boundary.

START HERE if you're new — this is the smallest, easiest file to understand.

THE PROBLEM IT SOLVES:
  Your wiki might contain "password: abc123" in a markdown file.
  You still want the AI to search/read the wiki — but never see that password.
  This file scrubs secrets from text *on the way out* to the AI.

TWO LAYERS (search "# LEARN:" below):
  1. is_private()  — block entire pages (e.g. folder secrets/, tag private)
  2. redact()      — mask patterns inside text (API keys, card numbers, …)

IMPORTANT: Files on disk are never changed. Redaction is output-only.

Local dev bypass: WIKI_DISABLE_REDACTION=1 — honoured only for the stdio
transport. On HTTP transports (sse / streamable-http) the flag is ignored and
an error is logged, so a public deploy can never switch the filter off.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any

REDACTED = "***REDACTED***"

log = logging.getLogger("wiki-brain.sanitize")

# Label patterns for secrets — longer/specific terms before short fallbacks (secret, token).
_SECRET_KEYS = (
    r"passwords?|passphrase|passcode|"
    r"api[\s_-]?keys?|secret[\s_-]?keys?|access[\s_-]?keys?|private[\s_-]?keys?|"
    r"auth[\s_-]?(?:token|key)s?|bearer[\s_-]?tokens?|refresh[\s_-]?tokens?|"
    r"session[\s_-]?(?:id|token)s?|jwt|"
    r"client[\s_-]?secrets?|webhook[\s_-]?secrets?|"
    r"cvv|cvc|cvv2|"
    r"card[\s_-]?(?:numbers?|nos?|\#)|"
    r"account[\s_-]?(?:numbers?|nos?|\#)|bank[\s_-]?accounts?|"
    r"iban|swift(?:[\s_-]?code)?|ifsc(?:[\s_-]?code)?|routing[\s_-]?(?:numbers?|codes?)|"
    r"upi[\s_-]?(?:id|pin)|atm[\s_-]?pin|pin[\s_-]?codes?|pins?|"
    r"otp|"
    r"ssn|aadhaar|aadhar|pan(?:[\s_-]?(?:nos?|numbers?|cards?))?|"
    r"passport[\s_-]?(?:numbers?|nos?)?|"
    r"license[\s_-]?(?:numbers?|nos?)?|"
    r"secret|token"
)

# "Password: hunter2" or "- **API Key**: sk-…" (line-anchored)
_SECRET_LINE_RE = re.compile(
    rf"(?im)^(\s*(?:[-*+]\s+)?(?:\*\*|__)?\s*(?:{_SECRET_KEYS})\s*(?:\*\*|__)?\s*)"
    r"([:=]\s*)(.+)$",
)

# "password=hunter2" mid-sentence
_SECRET_INLINE_RE = re.compile(
    rf"(?i)\b({_SECRET_KEYS})\b(\s*[:=]\s*)"
    r'(["\']?[^\s,<>|]{4,}["\']?)'
)

_CARD_RE = re.compile(r"\b(?:\d[\s-]?){12,18}\d\b")          # 13–19 digit card numbers
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_PEM_RE = re.compile(
    r"-----BEGIN[^\n]*PRIVATE KEY-----[\s\S]*?-----END[^\n]*PRIVATE KEY-----"
)
_AADHAAR_RE = re.compile(r"\b\d{4}\s?\d{4}\s?\d{4}\b")

# ------------------------------------------------------------------
# Whole-page deny lists — matched against stem, folder, tags, frontmatter
# ------------------------------------------------------------------
PRIVATE_STEMS: set[str] = {
    "passwords", "password", "financials", "finances",
    "secrets", "secret", "bank", "banking", "cards",
}
PRIVATE_FOLDERS: set[str] = {
    "secrets", "private", "finances", "financials", "banking",
}
PRIVATE_TAGS: set[str] = {
    "private", "secret", "confidential", "financial", "password",
}


_bypass_refused_logged = False


def _disabled() -> bool:
    global _bypass_refused_logged
    if os.environ.get("WIKI_DISABLE_REDACTION", "").strip().lower() not in ("1", "true", "yes"):
        return False
    transport = os.environ.get("MCP_TRANSPORT", "stdio").strip().lower() or "stdio"
    if transport == "stdio":
        return True
    if not _bypass_refused_logged:
        log.error(
            "[PRIVACY] WIKI_DISABLE_REDACTION is set but MCP_TRANSPORT=%s is not stdio "
            "— IGNORING it; redaction and private-page hiding stay ON. "
            "Remove WIKI_DISABLE_REDACTION from this deployment's environment.",
            transport,
        )
        _bypass_refused_logged = True
    return False


def redact(text: str) -> str:
    # LEARN: Called on search excerpts, read_page body, etc. Idempotent = safe to run twice.
    """Mask labeled secrets, PEM blocks, and bare number patterns. Safe to call twice."""
    if _disabled():
        return text or ""
    if not text or not isinstance(text, str):
        return text or ""

    text = _PEM_RE.sub(f"{REDACTED} [private key removed]", text)
    text = _SECRET_LINE_RE.sub(
        lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text
    )
    text = _SECRET_INLINE_RE.sub(
        lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text
    )
    text = _CARD_RE.sub("****-****-****-****", text)
    text = _SSN_RE.sub("***-**-****", text)
    text = _AADHAAR_RE.sub("**** **** ****", text)

    return text


def is_private(page: Any) -> bool:
    # LEARN: Checked before read_page returns content — private pages get a placeholder message.
    """True when the page should be fully hidden (not just redacted field-by-field)."""
    if _disabled() or page is None:
        return False
    fm = getattr(page, "frontmatter", {}) or {}
    if str(fm.get("private", "")).strip().lower() in ("true", "1", "yes"):
        return True
    if str(fm.get("visibility", "")).strip().lower() == "private":
        return True
    tags = getattr(page, "tags", []) or []
    if any(str(t).lower() in PRIVATE_TAGS for t in tags):
        return True
    stem = (getattr(page, "stem", "") or "").lower()
    folder = (getattr(page, "folder", "") or "").lower()
    if stem in PRIVATE_STEMS:
        return True
    if folder in PRIVATE_FOLDERS:
        return True
    return False


def private_placeholder(page: Any) -> str:
    """Human-readable stand-in when a page is blocked by is_private()."""
    stem = getattr(page, "stem", "unknown")
    folder = getattr(page, "folder", "")
    path = f"{folder}/{stem}.md" if folder else f"{stem}.md"
    return (
        f"*This page (`{path}`) contains sensitive data and is hidden by the "
        f"server's privacy filter. Open the file locally to view it.*"
    )


def safe_content(page: Any) -> str:
    """Return redacted page content, or a placeholder if the page is private."""
    if page is None:
        return ""
    if is_private(page):
        return private_placeholder(page)
    return redact(getattr(page, "content", "") or "")
