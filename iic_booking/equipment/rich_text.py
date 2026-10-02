"""Allow-list HTML sanitizer for OIC-formatted text (e.g. the equipment important instruction).

Stored format is a small HTML subset produced by the OIC editor (TipTap). Colours are palette
tokens (``var(--rt-red)``) so the frontend can pick light/dark shades; anything else is snapped
to the nearest palette entry or dropped. Plain text (legacy instructions) is kept as-is and the
frontend renders its line breaks.

Mirrors ``src/lib/richText.ts`` in the frontend.
"""

from __future__ import annotations

import colorsys
import re
from html import escape
from html import unescape
from html.parser import HTMLParser

import nh3

ALLOWED_TAGS = {
    "p", "br", "strong", "b", "em", "i", "u", "s", "ul", "ol", "li", "h3", "h4", "span", "a", "mark", "sub", "sup",
}
# Point sizes offered by the editor; 11 is the note's normal text size (``--rt-size-*`` in index.css).
FONT_SIZES = (8, 9, 10, 11, 12, 14, 16, 18, 20, 22, 24, 26, 28, 36)
_NORMAL_SIZE_PT = 11
_SIZE_KEYWORDS = {"xx-small": 7, "x-small": 7.5, "small": 10, "medium": 12, "large": 13.5, "x-large": 18, "xx-large": 24}
TEXT_COLORS = ("red", "orange", "amber", "green", "blue", "purple", "pink", "gray")
HIGHLIGHT_COLORS = ("yellow", "orange", "green", "blue", "pink")
TEXT_ALIGNS = ("center", "right")
FONT_FAMILIES = (
    "sans", "serif", "mono", "verdana", "tahoma", "trebuchet", "georgia", "garamond", "courier", "devanagari",
)
# Fonts commonly pasted from Word / Google Docs (or chosen in the older toolbar) → nearest allowed font.
_FONT_ALIASES = {
    **dict.fromkeys(("arial", "helvetica", "helvetica neue", "arial nova", "calibri", "carlito", "aptos",
                     "segoe ui", "roboto", "open sans", "lato", "noto sans", "liberation sans", "sans-serif",
                     "system-ui"), "sans"),
    **dict.fromkeys(("times new roman", "times", "cambria", "caladea", "book antiqua", "palatino linotype",
                     "palatino", "constantia", "noto serif", "liberation serif", "serif"), "serif"),
    **dict.fromkeys(("consolas", "monaco", "menlo", "lucida console", "cascadia code", "roboto mono",
                     "source code pro", "ui-monospace", "monospace"), "mono"),
    **dict.fromkeys(("courier new", "courier", "liberation mono"), "courier"),
    **dict.fromkeys(("verdana", "geneva"), "verdana"),
    "tahoma": "tahoma",
    **dict.fromkeys(("trebuchet ms", "trebuchet"), "trebuchet"),
    "georgia": "georgia",
    **dict.fromkeys(("garamond", "eb garamond", "adobe garamond pro"), "garamond"),
    **dict.fromkeys(("mangal", "nirmala ui", "kokila", "aparajita", "utsaah", "noto sans devanagari",
                     "kohinoor devanagari"), "devanagari"),
}

PLAIN_TEXT_MAX_LENGTH = 5000
HTML_MAX_LENGTH = 20000

_STYLE_PROPS_BY_TAG = {
    "span": ("color", "font-family", "font-size"),
    "mark": ("background-color",),
    "p": ("text-align",),
    "h3": ("text-align",),
    "h4": ("text-align",),
    "li": ("text-align",),
}
_TAG_RENAMES = {"div": "p", "h1": "h3", "h2": "h3", "h5": "h4", "h6": "h4", "blockquote": "p",
                "strike": "s", "del": "s", "ins": "u", "font": "span", "b": "strong", "i": "em"}
_DROP_CONTENT_TAGS = {"script", "style", "iframe", "object", "embed", "noscript", "template", "title", "head"}
_VOID_TAGS = {"br", "img", "hr", "input", "meta", "link", "wbr", "col", "area", "source"}
_ANY_TAG = re.compile(r"</?[a-zA-Z][\w:-]*(\s[^<>]*)?/?>")
_VAR_TOKEN = re.compile(r"^var\(\s*--rt-(hl-)?([a-z]+)\s*(,[^)]*)?\)$")
_FONT_TOKEN = re.compile(r"^var\(\s*--rt-font-([a-z]+)\s*\)$")
_SIZE_TOKEN = re.compile(r"^var\(\s*--rt-size-(\d{1,2})\s*\)$")
_SIZE_VALUE = re.compile(r"^(\d{1,3}(?:\.\d+)?)\s*(pt|px|em|rem|%)$")
_HEX = re.compile(r"^#([0-9a-f]{3}|[0-9a-f]{6})$")
_RGB = re.compile(r"^rgba?\(\s*(\d{1,3})[\s,]+(\d{1,3})[\s,]+(\d{1,3})(?:[\s,/]+([\d.]+%?))?\s*\)$")
_NAMED = {
    "black": (0, 0, 0), "white": (255, 255, 255), "gray": (128, 128, 128), "grey": (128, 128, 128),
    "red": (255, 0, 0), "darkred": (139, 0, 0), "orange": (255, 165, 0), "yellow": (255, 255, 0),
    "gold": (255, 215, 0), "green": (0, 128, 0), "lime": (0, 255, 0), "darkgreen": (0, 100, 0),
    "blue": (0, 0, 255), "navy": (0, 0, 128), "darkblue": (0, 0, 139), "teal": (0, 128, 128),
    "cyan": (0, 255, 255), "purple": (128, 0, 128), "violet": (238, 130, 238), "magenta": (255, 0, 255),
    "pink": (255, 192, 203), "brown": (165, 42, 42), "maroon": (128, 0, 0),
}


def looks_like_html(value: str | None) -> bool:
    return bool(value and _ANY_TAG.search(value))


def _parse_rgb(value: str) -> tuple[int, int, int] | None:
    v = value.strip().lower()
    if v in _NAMED:
        return _NAMED[v]
    m = _HEX.match(v)
    if m:
        h = m.group(1)
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    m = _RGB.match(v)
    if m:
        alpha = m.group(4)
        if alpha is not None:
            a = float(alpha.rstrip("%")) / (100 if alpha.endswith("%") else 1)
            if a < 0.15:
                return None
        return tuple(min(255, int(m.group(i))) for i in (1, 2, 3))  # type: ignore[return-value]
    return None


def _hue_bucket(hue_deg: float, buckets: list[tuple[float, str]]) -> str:
    for upper, name in buckets:
        if hue_deg < upper:
            return name
    return buckets[0][1]


_TEXT_HUES = [(15, "red"), (40, "orange"), (70, "amber"), (170, "green"), (255, "blue"),
              (290, "purple"), (345, "pink"), (361, "red")]
_HIGHLIGHT_HUES = [(15, "pink"), (40, "orange"), (75, "yellow"), (170, "green"), (290, "blue"),
                   (361, "pink")]


def palette_color(value: str, kind: str) -> str | None:
    """Canonical ``var(--rt-…)`` token for a colour, snapping arbitrary colours to the palette.

    Near-black / near-white text colours are dropped so text follows the theme colour.
    """
    palette = TEXT_COLORS if kind == "text" else HIGHLIGHT_COLORS
    prefix = "--rt-" if kind == "text" else "--rt-hl-"
    v = (value or "").strip().lower().replace("!important", "").strip()
    m = _VAR_TOKEN.match(v)
    if m:
        is_hl, name = bool(m.group(1)), m.group(2)
        if is_hl == (kind != "text") and name in palette:
            return f"var({prefix}{name})"
        return None
    rgb = _parse_rgb(v)
    if rgb is None:
        return None
    h, lightness, sat = colorsys.rgb_to_hls(*(c / 255 for c in rgb))
    if sat < 0.25 or lightness < 0.1 or lightness > 0.95:
        if kind == "text" and 0.3 <= lightness <= 0.7:
            return "var(--rt-gray)"
        return None
    name = _hue_bucket(h * 360, _TEXT_HUES if kind == "text" else _HIGHLIGHT_HUES)
    return f"var({prefix}{name})"


def font_token(value: str) -> str | None:
    """``var(--rt-font-…)`` for an allowed font token or a known font name in a stack; otherwise ``None``."""
    v = (value or "").strip().lower().replace("!important", "").strip()
    m = _FONT_TOKEN.match(v)
    if m:
        return f"var(--rt-font-{m.group(1)})" if m.group(1) in FONT_FAMILIES else None
    for name in v.split(","):
        alias = _FONT_ALIASES.get(name.strip().strip("'\"").strip())
        if alias:
            return f"var(--rt-font-{alias})"
    return None


def font_size_token(value: str) -> str | None:
    """``var(--rt-size-N)`` for an allowed size token, or a pasted size snapped to the nearest allowed point size."""
    v = (value or "").strip().lower().replace("!important", "").strip()
    m = _SIZE_TOKEN.match(v)
    if m:
        return f"var(--rt-size-{int(m.group(1))})" if int(m.group(1)) in FONT_SIZES else None
    if v in _SIZE_KEYWORDS:
        points = _SIZE_KEYWORDS[v]
    else:
        m = _SIZE_VALUE.match(v)
        if not m:
            return None
        number, unit = float(m.group(1)), m.group(2)
        points = {"pt": number, "px": number * 0.75, "rem": number * 12, "em": number * _NORMAL_SIZE_PT,
                  "%": number / 100 * _NORMAL_SIZE_PT}[unit]
    if not 6 <= points <= 72:
        return None
    return f"var(--rt-size-{min(FONT_SIZES, key=lambda size: (abs(size - points), size))})"


def _style_decls(raw: str) -> list[tuple[str, str]]:
    out = []
    for decl in (raw or "").split(";"):
        if ":" in decl:
            prop, value = decl.split(":", 1)
            out.append((prop.strip().lower(), value.strip()))
    return out


def clean_style(tag: str, raw: str) -> str:
    """Keep only palette colours, allowed fonts and sizes (span/mark) and centre/right alignment (blocks)."""
    allowed = _STYLE_PROPS_BY_TAG.get(tag, ())
    kept: dict[str, str] = {}
    for raw_prop, value in _style_decls(raw):
        prop = "background-color" if raw_prop == "background" else raw_prop
        if prop not in allowed or len(value) > 200:
            continue
        if prop == "color":
            token = palette_color(value, "text")
        elif prop == "font-family":
            token = font_token(value)
        elif prop == "font-size":
            token = font_size_token(value)
        elif prop == "background-color":
            token = palette_color(value, "highlight")
        else:
            token = value.lower() if value.lower() in TEXT_ALIGNS else None
        if token:
            kept[prop] = token
    return "; ".join(f"{k}: {v}" for k, v in kept.items())


def _style_value(raw: str, *props: str) -> str:
    found = ""
    for prop, value in _style_decls(raw):
        if prop in props:
            found = value
    return found


def _inline_marks_from_style(raw: str) -> list[str]:
    marks = []
    for prop, value in _style_decls(raw):
        v = value.lower()
        if prop == "font-weight" and (v in ("bold", "bolder") or (v.isdigit() and int(v) >= 600)):
            marks.append("strong")
        elif prop == "font-style" and v in ("italic", "oblique"):
            marks.append("em")
        elif prop in ("text-decoration", "text-decoration-line"):
            if "underline" in v:
                marks.append("u")
            if "line-through" in v:
                marks.append("s")
        elif prop == "vertical-align" and v in ("super", "sub"):
            marks.append("sup" if v == "super" else "sub")
    return list(dict.fromkeys(marks))


class _Normalizer(HTMLParser):
    """Rewrites legacy / pasted markup into the editor schema (div→p, font→span, style→marks)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.stack: list[tuple[str, list[str]]] = []
        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in _DROP_CONTENT_TAGS:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag in _VOID_TAGS:
            if tag == "br":
                self.out.append("<br>")
            return
        attr = {(k or "").lower(): v or "" for k, v in attrs}
        style = attr.get("style", "")
        if tag == "font" and attr.get("color"):
            style = f"color: {attr['color']}; {style}"
        if tag == "font" and attr.get("face"):
            style = f"font-family: {attr['face']}; {style}"
        target = _TAG_RENAMES.get(tag, tag)
        if target == "strong" and re.search(r"font-weight\s*:\s*(normal|[1-5]00)\b", style, re.I):
            target = "span"
        if target in ("sub", "sup") or re.search(r"vertical-align\s*:\s*(super|sub)\b", style, re.I):
            # Sub/superscript already shrink the text; Docs adds its own smaller size.
            style = "; ".join(f"{p}: {v}" for p, v in _style_decls(style) if p != "font-size")
        opened: list[str] = []

        def _open(name: str, extra: str = "") -> None:
            self.out.append(f"<{name}{extra}>")
            opened.append(name)

        if target == "span":
            # Highlights are stored as <mark>; colour and font stay on the span inside it.
            span_style = clean_style("span", style)
            background = clean_style("mark", f"background-color: {_style_value(style, 'background-color', 'background')}")
            if background:
                _open("mark", f' style="{escape(background, quote=True)}"')
            if span_style:
                _open("span", f' style="{escape(span_style, quote=True)}"')
        elif target in ALLOWED_TAGS:
            cleaned = clean_style(target, style)
            if target in ("p", "h3", "h4", "li") and not cleaned:
                cleaned = clean_style(target, f"text-align: {attr.get('align', '')}")
            extra = f' style="{escape(cleaned, quote=True)}"' if cleaned else ""
            if target == "a" and attr.get("href"):
                extra += f' href="{escape(attr["href"].strip(), quote=True)}"'
            if target == "ol" and attr.get("start", "").isdigit():
                extra += f' start="{attr["start"][:4]}"'
            _open(target, extra)
            block_font = clean_style(
                "span",
                f"font-family: {_style_value(style, 'font-family')}; font-size: {_style_value(style, 'font-size')}",
            )
            if target in ("p", "h3", "h4", "li") and block_font:
                _open("span", f' style="{escape(block_font, quote=True)}"')
        for mark in _inline_marks_from_style(style):
            if mark != target:
                _open(mark)
        self.stack.append((tag, opened))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag.lower() not in _VOID_TAGS and not self.skip_depth and tag.lower() not in _DROP_CONTENT_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in _DROP_CONTENT_TAGS:
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        if self.skip_depth or tag in _VOID_TAGS:
            return
        if not any(t == tag for t, _ in self.stack):
            return
        while self.stack:
            current, opened = self.stack.pop()
            self.out.extend(f"</{t}>" for t in reversed(opened))
            if current == tag:
                break

    def handle_data(self, data):
        if not self.skip_depth:
            self.out.append(escape(data, quote=False))

    def result(self) -> str:
        while self.stack:
            _tag, opened = self.stack.pop()
            self.out.extend(f"</{t}>" for t in reversed(opened))
        return "".join(self.out)


def _attribute_filter(element: str, attribute: str, value: str) -> str | None:
    if attribute == "style":
        return clean_style(element, value) or None
    if attribute == "start":
        return value if value.isdigit() and len(value) <= 4 else None
    if attribute == "href":
        v = value.strip()
        return v if len(v) <= 2000 and re.match(r"^(https?://|mailto:)", v, re.I) else None
    return value


_STYLED_TAGS = set(_STYLE_PROPS_BY_TAG)


def _nh3_clean(html: str) -> str:
    attributes = {tag: {"style"} for tag in _STYLED_TAGS}
    attributes["a"] = {"href"}
    attributes["ol"] = {"start"}
    return nh3.clean(
        html,
        tags=ALLOWED_TAGS,
        clean_content_tags=_DROP_CONTENT_TAGS,
        attributes=attributes,
        attribute_filter=_attribute_filter,
        url_schemes={"http", "https", "mailto"},
        url_relative="deny",
        link_rel="noopener noreferrer",
        set_tag_attribute_values={"a": {"target": "_blank"}},
        strip_comments=True,
    )


_EMPTY_WRAPPER = re.compile(r"<(span|strong|em|u|s|mark|a|sub|sup)(\s[^>]*)?>\s*</\1>")
_LINK_WITHOUT_HREF = re.compile(r"<a(?![^>]*\shref=)[^>]*>(.*?)</a>", re.S)


def sanitize_rich_text(value: str | None) -> str:
    """Return safe HTML; plain text is returned unchanged (line breaks preserved by the renderer)."""
    text = "" if value is None else str(value).replace("\r\n", "\n").strip()
    if not text or not looks_like_html(text):
        return text
    parser = _Normalizer()
    parser.feed(text)
    parser.close()
    html = _LINK_WITHOUT_HREF.sub(r"\1", _nh3_clean(parser.result()))
    previous = None
    while previous != html:
        previous, html = html, _EMPTY_WRAPPER.sub("", html)
    html = re.sub(r">\s*\n\s*<", "><", html).replace("\n", " ")
    return html.strip()


def clean_important_instruction(raw) -> tuple[str | None, str | None]:
    """Sanitize an instruction for saving. Returns ``(value, error)``; empty text becomes ``""``."""
    html = sanitize_rich_text(raw)
    plain = rich_text_to_plain(html)
    if not plain:
        return "", None
    if len(plain) > PLAIN_TEXT_MAX_LENGTH:
        return None, f"Keep the important instruction under {PLAIN_TEXT_MAX_LENGTH} characters."
    if len(html) > HTML_MAX_LENGTH:
        return None, "The important instruction has too much formatting. Remove some colours or styles and try again."
    return html, None


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


_SCRIPT_CHARS = {
    "sup": dict(zip("0123456789+-−=()ni", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁻⁼⁽⁾ⁿⁱ", strict=True)),
    "sub": dict(zip("0123456789+-−=()aehklmnopstx", "₀₁₂₃₄₅₆₇₈₉₊₋₋₌₍₎ₐₑₕₖₗₘₙₒₚₛₜₓ", strict=True)),
}


def _script_text(text: str, kind: str) -> str:
    """H₂O / cm⁻¹ when every character has a Unicode sub/superscript form, otherwise ``x^(…)`` / ``x_(…)``."""
    chars = _SCRIPT_CHARS[kind]
    if text and all(c in chars for c in text):
        return "".join(chars[c] for c in text)
    return f"{'^' if kind == 'sup' else '_'}({text})" if text.strip() else text


class _PlainText(HTMLParser):
    _BLOCKS = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.lists: list[list] = []
        self.scripts: list[tuple[str, int]] = []
        self.skip_depth = 0

    def _newline(self):
        if self.out and not self.out[-1].endswith("\n"):
            self.out.append("\n")

    def handle_starttag(self, tag, attrs):
        if tag in _DROP_CONTENT_TAGS:
            self.skip_depth += 1
        elif tag in ("ul", "ol"):
            self._newline()
            start = dict(attrs).get("start") or "1"
            self.lists.append([tag, int(start) - 1 if start.isdigit() else 0])
        elif tag == "li":
            self._newline()
            depth = max(0, len(self.lists) - 1)
            marker = "•"
            if self.lists and self.lists[-1][0] == "ol":
                self.lists[-1][1] += 1
                marker = f"{self.lists[-1][1]}."
            self.out.append("  " * depth + marker + " ")
        elif tag == "br":
            self.out.append("\n")
        elif tag in ("sub", "sup"):
            self.scripts.append((tag, len(self.out)))
        elif tag in self._BLOCKS and not (self.out and self.out[-1].endswith(" ") and self.lists):
            self._newline()

    def handle_endtag(self, tag):
        if tag in _DROP_CONTENT_TAGS:
            self.skip_depth = max(0, self.skip_depth - 1)
        elif tag in ("sub", "sup") and self.scripts and self.scripts[-1][0] == tag:
            _, start = self.scripts.pop()
            self.out[start:] = [_script_text("".join(self.out[start:]), tag)]
        elif tag in ("ul", "ol"):
            if self.lists:
                self.lists.pop()
            self._newline()
        elif tag in self._BLOCKS or tag == "li":
            self._newline()

    def handle_data(self, data):
        if not self.skip_depth:
            self.out.append(data)


def rich_text_to_plain(value: str | None) -> str:
    """Plain-text rendering for emails / PDFs / the Booking Assistant (lists become • / 1. lines)."""
    text = "" if value is None else str(value)
    if not looks_like_html(text):
        return text.strip()
    parser = _PlainText()
    parser.feed(text)
    parser.close()
    plain = unescape("".join(parser.out)).replace("\xa0", " ")
    plain = "\n".join(line.rstrip() for line in plain.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", plain).strip()
