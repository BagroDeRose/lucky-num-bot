"""Converts arbitrary text (in particular, AI-generated report text) into
safe, valid Telegram HTML (see main.py — the bot's global parse_mode).

Lives at the top level (not app.bot) because it's used by app.ai.report_
generator: the AI layer must not depend on the bot/UI layer, but it does
need to guarantee its output is safe Telegram markup before that text is
cached in the database and sent to users.

The report generator's system prompt instructs the model to use Telegram
HTML tags directly and never Markdown, but LLMs habitually reach for
Markdown (`**bold**`) regardless of instructions. Rather than trust prompt
compliance alone, this module deterministically:

1. Escapes the entire input for HTML safety (so any stray `<`, `>`, `&`
   the model writes — e.g. "5 < 8" — can never break parsing or inject
   markup).
2. Re-enables exactly the tags we support, whichever form the model used:
   real HTML tags (`<b>`), or common Markdown equivalents (`**bold**`,
   `__bold__`, `*italic*`, `` `code` ``).
3. If the result would still have unbalanced tags (e.g. the model forgot a
   closing tag), strips all formatting and falls back to plain text —
   a paid report failing to send at all (Telegram rejects malformed HTML
   outright) is a worse outcome than losing bold styling.
"""

from __future__ import annotations

import html
import re

_ALLOWED_TAGS = ("b", "i", "code")

# After html.escape(), a literal "<b>" the model wrote becomes "&lt;b&gt;".
# Un-escape exactly these known-safe tag tokens back to real tags.
_ESCAPED_TAG_TOKENS = {
    f"&lt;{tag}&gt;": f"<{tag}>" for tag in _ALLOWED_TAGS
} | {f"&lt;/{tag}&gt;": f"</{tag}>" for tag in _ALLOWED_TAGS}

# Markdown fallbacks, applied after escaping (asterisks/backticks are not
# touched by html.escape, so these patterns still match). Bold before
# italic, so "**x**" is fully consumed before the single-asterisk pattern
# would otherwise treat one of its own asterisks as an italic delimiter.
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_MD_BOLD_UNDERSCORE = re.compile(r"__(.+?)__", re.DOTALL)
_MD_ITALIC = re.compile(r"(?<!\*)\*([^*\n]*[^\s*][^*\n]*)\*(?!\*)")
_MD_CODE = re.compile(r"`([^`\n]+?)`")

_STRIP_TAGS_RE = re.compile(r"</?(?:" + "|".join(_ALLOWED_TAGS) + r")>")

# Telegram's hard limit for a text message is 4096 characters; sending
# anything longer fails outright. Leave headroom below that.
MAX_TELEGRAM_TEXT_LENGTH = 4096
_TRUNCATE_TARGET = MAX_TELEGRAM_TEXT_LENGTH - 100


def truncate_telegram_html(text: str, limit: int = _TRUNCATE_TARGET) -> str:
    """Trim already-safe Telegram HTML down to `limit` characters, cutting at
    a paragraph boundary where possible and re-checking tag balance (a cut
    can split a tag pair even though the pre-truncation text was balanced).
    """
    if len(text) <= limit:
        return text

    truncated = text[:limit]
    paragraph_break = truncated.rfind("\n\n")
    if paragraph_break > limit * 0.5:
        truncated = truncated[:paragraph_break]

    if not _tags_balanced(truncated):
        truncated = _strip_tags(truncated)
    return truncated.rstrip()


def to_telegram_html(text: str) -> str:
    """Return `text` as safe Telegram HTML: real tags preserved/converted,
    everything else escaped, never malformed.
    """
    escaped = html.escape(text, quote=False)

    for token, tag in _ESCAPED_TAG_TOKENS.items():
        escaped = escaped.replace(token, tag)

    escaped = _MD_BOLD.sub(r"<b>\1</b>", escaped)
    escaped = _MD_BOLD_UNDERSCORE.sub(r"<b>\1</b>", escaped)
    escaped = _MD_ITALIC.sub(r"<i>\1</i>", escaped)
    escaped = _MD_CODE.sub(r"<code>\1</code>", escaped)

    if not _tags_balanced(escaped):
        return _strip_tags(escaped)
    return escaped


def _tags_balanced(text: str) -> bool:
    return all(text.count(f"<{tag}>") == text.count(f"</{tag}>") for tag in _ALLOWED_TAGS)


def _strip_tags(text: str) -> str:
    return _STRIP_TAGS_RE.sub("", text)
