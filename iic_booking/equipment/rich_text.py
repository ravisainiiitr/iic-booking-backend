"""Allow-list HTML sanitizer for OIC-formatted text (e.g. the equipment important instruction)."""

from __future__ import annotations

import re
from html import escape
from html.parser import HTMLParser

ALLOWED_TAGS = {
    "p", "div", "span", "br", "b", "strong", "i", "em", "u", "s", "strike",
    "font", "ul", "ol", "li", "h1", "h2", "h3", "h4", "blockquote",
}
VOID_TAGS = {"br"}
ALLOWED_STYLE_PROPS = {
    "color", "background-color", "font-family", "font-size", "font-weight",
    "font-style", "text-decoration", "text-decoration-line", "text-align",
}
_SAFE_STYLE_VALUE = re.compile(r"^[#(),.%\-\w\s'\"]+$")
_FONT_SIZE_ATTR = re.compile(r"^[1-7]$")
_HTML_TAG = re.compile(r"</?(p|div|span|br|b|strong|i|em|u|s|strike|font|ul|ol|li|h[1-4]|blockquote)\b", re.I)


def looks_like_html(value: str | None) -> bool:
    return bool(value and _HTML_TAG.search(value))


def _clean_style(raw: str) -> str:
    kept = []
    for decl in (raw or "").split(";"):
        if ":" not in decl:
            continue
        prop, value = decl.split(":", 1)
        prop, value = prop.strip().lower(), value.strip()
        if prop not in ALLOWED_STYLE_PROPS or not value or len(value) > 80:
            continue
        lowered = value.lower()
        if "url(" in lowered or "expression" in lowered or "javascript" in lowered or not _SAFE_STYLE_VALUE.match(value):
            continue
        kept.append(f"{prop}: {value}")
    return "; ".join(kept)


class _Sanitizer(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.open: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in ("script", "style"):
            self.skip_depth += 1
            return
        if self.skip_depth or tag not in ALLOWED_TAGS:
            return
        parts = [tag]
        for name, value in attrs:
            name = (name or "").lower()
            value = value or ""
            if name == "style":
                style = _clean_style(value)
                if style:
                    parts.append(f'style="{escape(style, quote=True)}"')
            elif tag == "font" and name == "color" and _SAFE_STYLE_VALUE.match(value) and len(value) <= 30:
                parts.append(f'color="{escape(value, quote=True)}"')
            elif tag == "font" and name == "face" and _SAFE_STYLE_VALUE.match(value) and len(value) <= 80:
                parts.append(f'face="{escape(value, quote=True)}"')
            elif tag == "font" and name == "size" and _FONT_SIZE_ATTR.match(value):
                parts.append(f'size="{value}"')
        self.out.append("<" + " ".join(parts) + ">")
        if tag not in VOID_TAGS:
            self.open.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag.lower() in ALLOWED_TAGS and tag.lower() not in VOID_TAGS and self.open and self.open[-1] == tag.lower():
            self.open.pop()
            self.out.append(f"</{tag.lower()}>")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ("script", "style"):
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        if self.skip_depth or tag not in ALLOWED_TAGS or tag in VOID_TAGS or tag not in self.open:
            return
        while self.open:
            current = self.open.pop()
            self.out.append(f"</{current}>")
            if current == tag:
                break

    def handle_data(self, data):
        if not self.skip_depth:
            self.out.append(escape(data, quote=False))

    def result(self) -> str:
        while self.open:
            self.out.append(f"</{self.open.pop()}>")
        return "".join(self.out)


def sanitize_rich_text(value: str | None) -> str:
    """Return safe HTML; plain text is returned unchanged (line breaks preserved by the renderer)."""
    text = "" if value is None else str(value).replace("\r\n", "\n").strip()
    if not text or not looks_like_html(text):
        return text
    parser = _Sanitizer()
    parser.feed(text)
    parser.close()
    return parser.result().strip()


def instruction_user_type_choices() -> list[tuple[str, str]]:
    """User types that can be given their own important instruction (booking users)."""
    from iic_booking.users.models.user_type import UserType

    return [
        (code, str(label))
        for code, label in UserType.get_choices()
        if UserType.is_end_user_booking_type(code) or code == UserType.OTHER
    ]


def resolve_important_instruction(equipment, user_type: str | None) -> str:
    """Instruction for the user type, falling back to the default instruction."""
    default = getattr(equipment, "important_instruction", None) or ""
    per_type = getattr(equipment, "important_instruction_by_user_type", None) or {}
    if user_type and isinstance(per_type, dict):
        specific = per_type.get(str(user_type)) or per_type.get(str(user_type).lower())
        if isinstance(specific, str) and specific.strip():
            return specific
    return default


def rich_text_to_plain(value: str | None) -> str:
    """Plain-text rendering for emails / PDFs."""
    text = "" if value is None else str(value)
    if not looks_like_html(text):
        return text.strip()
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|li|h[1-4]|blockquote)>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    from html import unescape

    return re.sub(r"\n{3,}", "\n\n", unescape(text)).strip()
