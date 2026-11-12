"""XSS-safe rich text for Connect posts and articles.

Server-side allowlist (nh3 / ammonia). The browser editor's output is never trusted.

* Links: absolute http(s) / mailto only, ``rel="noopener noreferrer nofollow ugc"``, opened in a new tab.
* Code: ``<pre><code class="language-x">`` with a strict class pattern.
* Mentions: ``<span data-mention-id="123">@Name</span>``; ids are resolved server-side (``mentioned_user_ids``) and
  only users who can read the thread are notified.
* Images: no ``src`` is ever stored. An inline image is ``<img data-attachment-id="<uuid>" alt="...">``; the
  frontend loads it through the authorised download endpoint. Unknown or foreign attachment ids are stripped by
  the service that binds attachments to the post.
"""

from __future__ import annotations

import re
from html import unescape

import nh3

from iic_booking.equipment.rich_text import rich_text_to_plain

MAX_HTML_LENGTH = 100_000
MAX_TEXT_LENGTH = 30_000

ALLOWED_TAGS = {
    "p", "br", "hr", "strong", "b", "em", "i", "u", "s", "del", "sub", "sup", "mark",
    "h3", "h4", "blockquote", "ul", "ol", "li", "pre", "code", "a", "span", "img",
    "table", "thead", "tbody", "tr", "th", "td",
}
_DROP_CONTENT_TAGS = {"script", "style", "iframe", "object", "embed", "noscript", "template", "title", "head", "svg", "math"}
_LANG = re.compile(r"^language-[a-z0-9+#-]{1,24}$")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_DIGITS = re.compile(r"^\d{1,12}$")
_MENTION_RE = re.compile(r'data-mention-id="(\d{1,12})"')
_ATTACHMENT_RE = re.compile(r'data-attachment-id="([0-9a-f-]{36})"')
_ALLOWED_HREF = re.compile(r"^(https?://|mailto:)", re.I)
LINK_REL = "noopener noreferrer nofollow ugc"
# Input is treated as HTML only when it contains a tag the editor produces; "a < b" or "<two>" stay text.
_KNOWN_TAG = re.compile(
    r"</?(p|br|hr|strong|b|em|i|u|s|del|sub|sup|mark|h[1-6]|blockquote|ul|ol|li|pre|code|a|span|img|div|"
    r"table|thead|tbody|tr|th|td|script|style|iframe|svg|object|embed)(\s[^<>]*)?/?>",
    re.I,
)


def _attribute_filter(element: str, attribute: str, value: str) -> str | None:
    # nh3 also runs the filter on the rel / target values it sets itself (link_rel, set_tag_attribute_values).
    if element == "a" and attribute == "rel":
        return LINK_REL
    if element == "a" and attribute == "target":
        return "_blank"
    if element == "a" and attribute == "href":
        return value if _ALLOWED_HREF.match(value.strip()) else None
    if element == "code" and attribute == "class":
        return value if _LANG.match(value) else None
    if element == "span" and attribute == "data-mention-id":
        return value if _DIGITS.match(value) else None
    if element == "img" and attribute == "data-attachment-id":
        return value.lower() if _UUID.match(value.lower()) else None
    if element == "img" and attribute == "alt":
        return value[:200]
    if element == "ol" and attribute == "start":
        return value if _DIGITS.match(value) else None
    if element in ("th", "td") and attribute in ("colspan", "rowspan"):
        return value if _DIGITS.match(value) and int(value) <= 20 else None
    return None


_ATTRIBUTES = {
    "a": {"href"},
    "code": {"class"},
    "span": {"data-mention-id"},
    "img": {"data-attachment-id", "alt"},
    "ol": {"start"},
    "th": {"colspan", "rowspan"},
    "td": {"colspan", "rowspan"},
}

_IMG_WITHOUT_ID = re.compile(r"<img(?![^>]*\sdata-attachment-id=)[^>]*>", re.I)
_LINK_WITHOUT_HREF = re.compile(r"<a(?![^>]*\shref=)[^>]*>(.*?)</a>", re.S)


class RichTextTooLong(ValueError):
    pass


def sanitize_post_html(value: str | None) -> str:
    """Safe HTML for storage. Plain text input is escaped and wrapped in paragraphs."""
    raw = "" if value is None else str(value).replace("\r\n", "\n").replace("\x00", "")
    if len(raw) > MAX_HTML_LENGTH:
        raise RichTextTooLong(f"The text is too long (maximum {MAX_HTML_LENGTH:,} characters of formatted text).")
    raw = raw.strip()
    if not raw:
        return ""
    if not _KNOWN_TAG.search(raw):
        paragraphs = [p.strip() for p in raw.split("\n\n") if p.strip()]
        raw = "".join(f"<p>{nh3.clean_text(p).replace(chr(10), '<br>')}</p>" for p in paragraphs)
    html = nh3.clean(
        raw,
        tags=ALLOWED_TAGS,
        clean_content_tags=_DROP_CONTENT_TAGS,
        attributes=_ATTRIBUTES,
        attribute_filter=_attribute_filter,
        url_schemes={"http", "https", "mailto"},
        link_rel=LINK_REL,
        set_tag_attribute_values={"a": {"target": "_blank"}},
        strip_comments=True,
    )
    html = _IMG_WITHOUT_ID.sub("", html)
    html = _LINK_WITHOUT_HREF.sub(r"\1", html)
    return html.strip()


def html_to_text(html: str) -> str:
    text = rich_text_to_plain(html) if html else ""
    return unescape(text or "")[:MAX_TEXT_LENGTH]


def mentioned_user_ids(html: str) -> list[int]:
    return list(dict.fromkeys(int(m) for m in _MENTION_RE.findall(html or "")))[:50]


def referenced_attachment_ids(html: str) -> list[str]:
    return list(dict.fromkeys(_ATTACHMENT_RE.findall(html or "")))


def strip_attachment_refs(html: str, allowed: set[str]) -> str:
    """Remove inline images whose attachment id is not in ``allowed`` (foreign / unknown uploads)."""

    def repl(match: re.Match) -> str:
        return match.group(0) if match.group(1) in allowed else ""

    return re.sub(r'<img[^>]*data-attachment-id="([0-9a-f-]{36})"[^>]*>', repl, html or "")


def clean_plain(value, *, max_length: int) -> str:
    """Single-line plain text (titles, labels): no tags, no control characters, collapsed whitespace."""
    text = nh3.clean(str(value or ""), tags=set())
    text = unescape(text)
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_length]
