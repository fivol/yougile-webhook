"""Turn whatever the agent wrote into what YouGile's chat renders.

YouGile shows two fields per message: `text` verbatim, and `textHtml` rendered.
Markdown in `text` reaches the user as literal `**`, `#` and `-` characters — a
wall instead of a report. The agent is told to write `textHtml` and still drops
it in about half of its replies, because a tool schema sits closer to the call
than any system prompt does and the upstream schema called that field "optional,
rarely needed". Prompting has been tried; this module is what replaces it.

Everything here is one-way and mechanical: markdown in, the narrow subset of
HTML that YouGile renders out (the same subset `prompts/formatting.txt` asks the
agent for). No dependencies — it runs inside the MCP server, which starts on
every turn and must never fail to import.

The tag budget, from formatting.txt:

    structure : <p>, <br>, <hr>
    headings  : <h4>, <h3>          (never <h1>/<h2> — they render enormous)
    emphasis  : <b>, <i>, <s>
    lists     : <ul><li>, <ol><li>  (tight: no <p>, no <br> between items)
    code      : <code>, <pre><code>
    quotes    : <blockquote>
    links     : <a href="…">
    tables    : <table>
"""

import html as _html
import re
from typing import List, Optional, Tuple

# A message that carries a YouGile file marker is left ALONE — marker and all.
# YouGile resolves `/root/#file:<url>` while rendering `text`, and a message that
# also carries `textHtml` renders the HTML instead, so generating HTML here is
# how an uploaded screenshot would stop being an attachment and become a line of
# grey text nobody can open.
FILE_MARKER_RE = re.compile(r"/root/#file:\S+")

# Inline code is pulled out before anything else runs and put back last, so
# `**not bold**` inside backticks survives as literal characters.
_PLACEHOLDER = "\x00chatmd{}\x00"
_PLACEHOLDER_RE = re.compile(r"\x00chatmd(\d+)\x00")

_LINK_RE = re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)\)")
_BOLD_RE = re.compile(r"\*\*(\S(?:[^*]*\S)?)\*\*")
# Deliberately strict: a lone `*` is far more often multiplication or a footnote
# than emphasis, so both markers must hug non-space text on the inside.
_ITALIC_RE = re.compile(r"(?<![\w*])[*_](\S(?:[^*_\n]*\S)?)[*_](?![\w*])")
_STRIKE_RE = re.compile(r"~~(\S(?:[^~]*\S)?)~~")
_CODE_RE = re.compile(r"`([^`\n]+)`")
_BARE_URL_RE = re.compile(r"(?<![\"'=>\w])(https?://[^\s<>\"')\]]+)")

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET_RE = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_ORDERED_RE = re.compile(r"^(\s*)\d+[.)]\s+(.*)$")
_QUOTE_RE = re.compile(r"^\s*>\s?(.*)$")
_HR_RE = re.compile(r"^\s*([-*_])\s*(?:\1\s*){2,}$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)(.*)$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$")

# Structure that proves the agent wrote markdown, whatever the length. A short
# reply with a `code span` or a [link](url) is still worth rendering.
_MARKDOWN_HINTS = (
    _HEADING_RE, _BULLET_RE, _ORDERED_RE, _QUOTE_RE, _FENCE_RE,
)
_INLINE_HINTS = (_LINK_RE, _BOLD_RE, _CODE_RE, _STRIKE_RE)

_HTML_TAG_RE = re.compile(
    r"</?(?:p|br|hr|h[1-6]|b|strong|i|em|u|s|mark|ul|ol|li|code|pre|"
    r"blockquote|a|table|thead|tbody|tr|th|td|div|span)\b[^>]*>",
    re.IGNORECASE,
)

CDATA_OPEN, CDATA_CLOSE = "<![CDATA[", "]]>"


# --------------------------------------------------------------- small helpers

def strip_cdata(value: str) -> str:
    """Drop an XML CDATA wrapper. YouGile stores the string verbatim and its
    renderer reads `<![CDATA[ ... ]]>` as a single non-element node, so a wrapped
    message lands as an EMPTY bubble — the loudest failure this file prevents."""
    if not value or CDATA_OPEN not in value:
        return value
    stripped = value.strip()
    if stripped.startswith(CDATA_OPEN) and stripped.endswith(CDATA_CLOSE):
        return stripped[len(CDATA_OPEN):-len(CDATA_CLOSE)].strip()
    # Wrapped in some other shape. Neither token can legitimately appear in a
    # chat message, so drop them rather than guess at the structure.
    return value.replace(CDATA_OPEN, "").replace(CDATA_CLOSE, "").strip()


def looks_like_html(value: str) -> bool:
    return bool(value) and bool(_HTML_TAG_RE.search(value))


def has_markdown(value: str) -> bool:
    """True when the text carries markdown structure the user would otherwise
    see as literal characters."""
    if not value:
        return False
    for line in value.splitlines():
        if any(pattern.match(line) for pattern in _MARKDOWN_HINTS):
            return True
    return any(pattern.search(value) for pattern in _INLINE_HINTS)


def has_file_marker(value: str) -> bool:
    return bool(value) and bool(FILE_MARKER_RE.search(value))


def _escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ------------------------------------------------------------- inline markdown

def _inline_html(text: str) -> str:
    """One line of markdown as HTML. Code spans first, bare URLs last."""
    spans: List[str] = []

    def stash(match: "re.Match[str]") -> str:
        spans.append(f"<code>{_escape(match.group(1))}</code>")
        return _PLACEHOLDER.format(len(spans) - 1)

    text = _CODE_RE.sub(stash, text)
    text = _escape(text)
    text = _LINK_RE.sub(
        lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', text)
    text = _BOLD_RE.sub(r"<b>\1</b>", text)
    text = _STRIKE_RE.sub(r"<s>\1</s>", text)
    text = _ITALIC_RE.sub(r"<i>\1</i>", text)
    # After links, so a URL already inside an href is not linked twice.
    text = _link_bare_urls(text)
    return _PLACEHOLDER_RE.sub(lambda m: spans[int(m.group(1))], text)


def _inline_text(text: str) -> str:
    """The same line as plain prose: markers removed, information kept."""
    text = _CODE_RE.sub(r"\1", text)
    text = _LINK_RE.sub(lambda m: f"{m.group(1)}: {m.group(2)}", text)
    text = _BOLD_RE.sub(r"\1", text)
    text = _STRIKE_RE.sub(r"\1", text)
    return _ITALIC_RE.sub(r"\1", text)


def _link_bare_urls(fragment: str) -> str:
    """Wrap raw URLs in a piece of HTML, skipping anything already inside a tag,
    a link, or code."""
    out: List[str] = []
    depth = 0  # inside <a>/<code>/<pre>, where a URL must be left alone
    for token in re.split(r"(<[^>]+>)", fragment):
        if token.startswith("<") and token.endswith(">"):
            name = re.match(r"</?\s*([a-zA-Z0-9]+)", token)
            tag = name.group(1).lower() if name else ""
            if tag in ("a", "code", "pre"):
                depth += -1 if token.startswith("</") else 1
                depth = max(depth, 0)
            out.append(token)
        elif depth:
            out.append(token)
        else:
            out.append(_BARE_URL_RE.sub(r'<a href="\1">\1</a>', token))
    return "".join(out)


# -------------------------------------------------------------- block markdown

class _Blocks:
    """Collects HTML and the plain-text twin of the same content, so `text` and
    `textHtml` can never drift apart."""

    def __init__(self) -> None:
        self.html: List[str] = []
        self.text: List[str] = []

    def add(self, html: str, text: str) -> None:
        if html:
            self.html.append(html)
        if text:
            self.text.append(text)


def _render_table(rows: List[str], out: _Blocks) -> None:
    cells = [[c.strip() for c in row.strip().strip("|").split("|")] for row in rows]
    head, body = cells[0], cells[1:]
    html = ["<table><thead><tr>"]
    html += [f"<th>{_inline_html(c)}</th>" for c in head]
    html.append("</tr></thead><tbody>")
    for row in body:
        html.append("<tr>" + "".join(f"<td>{_inline_html(c)}</td>" for c in row) + "</tr>")
    html.append("</tbody></table>")
    out.add("".join(html),
            "\n".join(" | ".join(_inline_text(c) for c in row) for row in cells))


def _render_list(items: List[Tuple[int, str, bool]], out: _Blocks) -> None:
    """Items as (indent, content, ordered). One level of nesting is honoured;
    deeper indents fold into it rather than growing an unreadable tree.

    An item stays open until the next one at its own level, because a nested
    list belongs INSIDE the <li> above it. Closing each <li> as it is written —
    the obvious way — puts the child <ul> between two list items instead, which
    renders as an extra empty bullet.
    """
    html: List[str] = []
    text: List[str] = []
    base = items[0][0]
    tag = "ol" if items[0][2] else "ul"
    number = 0
    open_item = False
    open_child = False
    html.append(f"<{tag}>")
    for indent, content, _ordered in items:
        nested = indent > base
        if nested:
            if not open_child:
                if not open_item:  # a sublist with nothing above it to hang on
                    html.append("<li>")
                    open_item = True
                html.append("<ul>")
                open_child = True
            html.append(f"<li>{_inline_html(content)}</li>")
            text.append("    • " + _inline_text(content))
            continue
        if open_child:
            html.append("</ul>")
            open_child = False
        if open_item:
            html.append("</li>")
        html.append(f"<li>{_inline_html(content)}")
        open_item = True
        number += 1
        marker = f"{number}. " if tag == "ol" else "• "
        text.append(marker + _inline_text(content))
    if open_child:
        html.append("</ul>")
    if open_item:
        html.append("</li>")
    html.append(f"</{tag}>")
    out.add("".join(html), "\n".join(text))


def to_html(markdown: str) -> Tuple[str, str]:
    """(text, html) for a markdown message: the plain twin, and the HTML.

    Headings all land on <h4> — YouGile renders <h1>/<h2> at a size that breaks
    the chat panel on mobile, and a chat message has no use for six levels.
    """
    out = _Blocks()
    para: List[str] = []
    quote: List[str] = []
    bullets: List[Tuple[int, str, bool]] = []

    def flush() -> None:
        if bullets:
            _render_list(list(bullets), out)
            bullets.clear()
        if quote:
            out.add(
                "<blockquote>" + "<br>".join(_inline_html(q) for q in quote) + "</blockquote>",
                "\n".join("> " + _inline_text(q) for q in quote))
            quote.clear()
        if para:
            out.add(
                "<p>" + "<br>".join(_inline_html(p) for p in para) + "</p>",
                "\n".join(_inline_text(p) for p in para))
            para.clear()

    lines = markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    index = 0
    while index < len(lines):
        line = lines[index].rstrip()

        fence = _FENCE_RE.match(line)
        if fence:
            marker = fence.group(1)
            index += 1
            body: List[str] = []
            while index < len(lines) and not lines[index].strip().startswith(marker):
                body.append(lines[index])
                index += 1
            index += 1  # step over the closing fence (or off the end)
            flush()
            code = "\n".join(body)
            out.add(f"<pre><code>{_escape(code)}</code></pre>", code)
            continue

        # A table needs its separator row to be a table at all — otherwise a line
        # with a pipe in it is just a line with a pipe in it.
        if ("|" in line and index + 1 < len(lines)
                and _TABLE_SEP_RE.match(lines[index + 1]) and "|" in lines[index + 1]):
            rows = [line]
            index += 2
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                rows.append(lines[index])
                index += 1
            flush()
            _render_table(rows, out)
            continue

        index += 1

        if not line.strip():
            flush()
            continue

        heading = _HEADING_RE.match(line)
        if heading:
            flush()
            title = heading.group(2).strip()
            out.add(f"<h4>{_inline_html(title)}</h4>", _inline_text(title))
            continue

        if _HR_RE.match(line):
            flush()
            out.add("<hr>", "—")
            continue

        quoted = _QUOTE_RE.match(line)
        if quoted and line.lstrip().startswith(">"):
            if para or bullets:
                flush()
            quote.append(quoted.group(1))
            continue

        bullet = _BULLET_RE.match(line)
        ordered = None if bullet else _ORDERED_RE.match(line)
        if bullet or ordered:
            if para or quote:
                flush()
            match = bullet or ordered
            bullets.append((len(match.group(1)), match.group(2), ordered is not None))
            continue

        if bullets:
            # A plain line under a bullet is that bullet's continuation, not a
            # new paragraph — folding it in keeps the list tight.
            indent, content, is_ordered = bullets[-1]
            bullets[-1] = (indent, f"{content} {line.strip()}", is_ordered)
            continue
        if quote:
            quote[-1] = f"{quote[-1]} {line.strip()}"
            continue
        para.append(line)

    flush()
    return "\n\n".join(out.text), "".join(out.html)


def to_plain(markdown: str) -> str:
    """Markdown with its markers removed — what belongs in `text`."""
    return to_html(markdown)[0]


# ----------------------------------------------------------------- HTML repair

_H12_OPEN_RE = re.compile(r"<h[12](\s[^>]*)?>", re.IGNORECASE)
_H12_CLOSE_RE = re.compile(r"</h[12]>", re.IGNORECASE)
_P_IN_LI_RE = re.compile(r"(<li[^>]*>)\s*<p[^>]*>(.*?)</p>\s*(?=</li>)",
                         re.IGNORECASE | re.DOTALL)
_EMPTY_P_RE = re.compile(r"<p>\s*(?:<br\s*/?>)*\s*</p>", re.IGNORECASE)
_BR_RUN_RE = re.compile(r"(?:<br\s*/?>\s*){2,}", re.IGNORECASE)
_BR_BEFORE_BLOCK_RE = re.compile(
    r"(?:<br\s*/?>\s*)+(?=<(?:p|h[1-6]|ul|ol|hr|table|blockquote|pre)\b)",
    re.IGNORECASE)


def sanitize_html(html: str) -> str:
    """Repair HTML the agent wrote: the failures that survive a good prompt.

    Nothing here is cosmetic — each rule matches a way a message has actually
    reached the chat broken."""
    html = strip_cdata(html)
    html = _H12_OPEN_RE.sub("<h4>", html)
    html = _H12_CLOSE_RE.sub("</h4>", html)
    html = _P_IN_LI_RE.sub(r"\1\2", html)
    html = _EMPTY_P_RE.sub("", html)
    html = _BR_BEFORE_BLOCK_RE.sub("", html)
    html = _BR_RUN_RE.sub("<br>", html)
    return _link_bare_urls(html).strip()


_BLOCK_BREAK_RE = re.compile(
    r"</(?:p|h[1-6]|li|tr|blockquote|pre|div)>|<br\s*/?>|<hr\s*/?>", re.IGNORECASE)
_LI_RE = re.compile(r"<li[^>]*>", re.IGNORECASE)
_CELL_RE = re.compile(r"</t[dh]>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")


def html_to_text(html: str) -> str:
    """Plain prose from HTML — the `text` twin when only HTML was given."""
    text = _LI_RE.sub("• ", html)
    text = _CELL_RE.sub(" | ", text)
    text = _BLOCK_BREAK_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)
    text = _html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# ------------------------------------------------------------------ the entry

def normalize_message(
    text: Optional[str],
    text_html: Optional[str] = None,
    *,
    plain_min_chars: int = 400,
) -> Tuple[str, str, List[str]]:
    """(text, textHtml, notes) for one outgoing chat message.

    The guarantee: anything the user would otherwise read as raw markup leaves
    here as HTML. `notes` names what was repaired — the caller reports it back to
    the agent, which is the only feedback this channel has (the agent never sees
    its own messages rendered).

    A short one-line ack stays plain on purpose: `text` alone renders fine, and
    wrapping "Исследую" in <p> tags buys nothing.
    """
    text = strip_cdata(text or "").strip()
    html = strip_cdata(text_html or "").strip()
    notes: List[str] = []

    # An attachment message is a marker, not prose — see FILE_MARKER_RE.
    if has_file_marker(text) or has_file_marker(html):
        return text, html, notes

    if html:
        repaired = sanitize_html(html)
        if repaired != html:
            notes.append("repaired the HTML (h1/h2 → h4, stray <br>, bare URLs)")
        html = repaired
        # `text` rides along as the notification/search copy. Markdown left in
        # it shows up as literal characters wherever YouGile prints it raw.
        if has_markdown(text):
            text = to_plain(text)
            notes.append("stripped markdown out of `text`")
        elif not text:
            text = html_to_text(html)
        return text, html, notes

    if not text:
        return text, html, notes

    if has_markdown(text) or len(text) >= plain_min_chars:
        plain, rendered = to_html(text)
        if rendered:
            notes.append(
                "rendered your markdown into `textHtml` — YouGile does not "
                "render markdown, so the message would have reached the user "
                "as raw characters")
            return plain, rendered, notes

    return text, html, notes
