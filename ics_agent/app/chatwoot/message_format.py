"""Normalize legacy FAQ image tags for Chatwoot's Markdown-only renderer."""
from __future__ import annotations

import html
import ipaddress
import re
from html.parser import HTMLParser
from urllib.parse import quote, urlsplit


# Preserve code samples while converting only image markup and encoded spaces.
_PARTS = re.compile(
    r"(?P<code>(?P<fence>`{3,}|~{3,})[^\n]*\n[\s\S]*?(?P=fence)"
    r"|(?P<ticks>`+)[\s\S]*?(?P=ticks)"
    r"|(?m:^(?: {4}|\t)[^\n]*(?:\n(?: {4}|\t)[^\n]*)*))"
    r"|(?P<image>\\?<img\b[^>]*>|\\?&lt;img\b.*?&gt;)"
    r"|(?P<space>(?:&#(?:x0*20|0*32);|&nbsp;)(?=\s*$))",
    re.IGNORECASE,
)
_SMART_QUOTES = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'"})
_MARKDOWN_LINK = re.compile(r"\[[^\]\n]*\]\((https?://[^\n]+)\)", re.IGNORECASE)


class _ImageTagParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.attributes: dict[str, str | None] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "img":
            self.attributes = dict(attrs)


def _markdown_image(tag: str) -> str | None:
    parser = _ImageTagParser()
    tag = tag.lstrip("\\")
    if tag.lower().startswith("&lt;"):
        tag = html.unescape(tag)
    # Let HTMLParser decode entities inside normal quoted attributes. Decoding
    # &quot; before parsing would prematurely terminate a valid src value.
    parser.feed(tag.translate(_SMART_QUOTES))
    source = (parser.attributes.get("src") or "").strip()
    wrapped_link = _MARKDOWN_LINK.fullmatch(source)
    if wrapped_link:
        source = wrapped_link.group(1).strip()
    if source.startswith("<") and source.endswith(">"):
        source = source[1:-1]
    if any(ord(character) < 32 for character in source):
        return None
    try:
        url = urlsplit(source)
        if url.scheme.lower() not in {"http", "https"} or not url.hostname:
            return None
        if url.username is not None or url.password is not None:
            return None
        # Chatwoot calls new URL() while rendering an image; an invalid host
        # or port can otherwise prevent the whole message from rendering.
        if url.port is not None and not 0 <= url.port <= 65535:
            return None
        host = url.hostname.encode("idna").decode("ascii")
        if ":" in host or re.fullmatch(r"[0-9.]+", host):
            ipaddress.ip_address(host)
        elif not re.fullmatch(r"[A-Za-z0-9._-]+", host):
            return None
    except (ValueError, UnicodeError):
        return None

    # Parentheses and Chinese paths must not break the Markdown destination.
    source = quote(source, safe=":/?#[]@!$&'*+,;=%~-._")
    description = parser.attributes.get("alt") or "图片"
    description = " ".join(description.split())
    description = description.replace("&", "&amp;").translate(
        str.maketrans({"\\": "&#92;", "[": "&#91;", "]": "&#93;"})
    )
    return f"![{description}]({source})"


def normalize_chatwoot_message(content: str) -> str:
    """Render supported image tags without enabling arbitrary HTML in Chatwoot."""
    def replace(match: re.Match[str]) -> str:
        if match.group("code") is not None:
            return match.group(0)
        if match.group("space") is not None:
            return " "
        image = _markdown_image(match.group(0))
        return f"\n\n{image}\n\n" if image is not None else match.group(0)

    return _PARTS.sub(replace, content)
