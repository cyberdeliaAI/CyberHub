"""In-app help: render the user manual (Markdown) to HTML.

The canonical manual lives at resources/help/cyberhub-manual.md. Legacy Lite
manual paths remain as read-only fallbacks so an incremental update over an old
installation does not leave the help page empty.

The help modal shows one module's
`### <name>` section; the /help page renders the whole manual. A small
purpose-built Markdown renderer keeps this dependency-free (no `markdown` pip
package). It covers the constructs the manual actually uses: headings, bold,
italic, inline code, fenced code, links, tables, ordered/unordered lists, and
horizontal rules. Images and HTML comments are stripped (local screenshot paths
don't resolve in the browser).
"""
import os
import re
from html import escape

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MANUAL_PATHS = (
    os.path.join(_ROOT, "resources", "help", "cyberhub-manual.md"),
    os.path.join(_ROOT, "cyberhub-manual.md"),
    os.path.join(_ROOT, "resources", "help", "cyberhub-lite-manual.md"),
    os.path.join(_ROOT, "cyberhub-lite-manual.md"),
    # Backward compatibility for older Lite installations.
    os.path.join(_ROOT, "resources", "help", "cyberhub-light-manual.md"),
    os.path.join(_ROOT, "cyberhub-light-manual.md"),
)


def _load_manual():
    for path in _MANUAL_PATHS:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        except OSError:
            continue
    return ""


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _inline(text):
    """Apply inline Markdown (code, links, bold, italic) to a line of text."""
    spans = []

    def _stash(m):
        spans.append(m.group(1))
        return f"\x00{len(spans) - 1}\x00"

    text = re.sub(r"`([^`]+)`", _stash, text)          # protect inline code
    text = escape(text)                                # escape the rest
    text = re.sub(                                     # links [text](url)
        r"\[([^\]]+)\]\(([^)]+)\)",
        lambda m: f'<a href="{escape(m.group(2), quote=True)}">{m.group(1)}</a>',
        text,
    )
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<!\*)\*([^*\s][^*]*?)\*(?!\*)", r"<em>\1</em>", text)
    text = re.sub(r"\x00(\d+)\x00",
                  lambda m: f"<code>{escape(spans[int(m.group(1))])}</code>", text)
    return text


def _render_table(rows):
    """rows: list of raw '| a | b |' strings (incl. the |---| separator)."""
    def cells(row):
        row = row.strip()
        if row.startswith("|"):
            row = row[1:]
        if row.endswith("|"):
            row = row[:-1]
        return [c.strip() for c in row.split("|")]

    if not rows:
        return ""
    header = cells(rows[0])
    body = [cells(r) for r in rows[2:]] if len(rows) > 2 else []
    out = ["<table><thead><tr>"]
    out += [f"<th>{_inline(c)}</th>" for c in header]
    out.append("</tr></thead><tbody>")
    for r in body:
        out.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>")
    out.append("</tbody></table>")
    return "".join(out)


def _build_list(items):
    """items: list of (indent, ordered, text). Nested by indent."""
    html = []
    stack = []  # (indent, tag)

    def close_to(indent):
        while stack and stack[-1][0] > indent:
            html.append(f"</{stack.pop()[1]}>")

    for indent, ordered, text in items:
        tag = "ol" if ordered else "ul"
        if not stack or indent > stack[-1][0]:
            html.append(f"<{tag}>")
            stack.append((indent, tag))
        else:
            close_to(indent)
        html.append(f"<li>{_inline(text)}</li>")
    while stack:
        html.append(f"</{stack.pop()[1]}>")
    return "".join(html)


def render_markdown(md, heading_ids=True):
    md = re.sub(r"<!--.*?-->", "", md, flags=re.DOTALL)        # strip comments
    md = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", md)               # strip images
    lines = md.split("\n")
    out = []
    para = []
    i, n = 0, len(lines)

    def flush_para():
        if para:
            out.append("<p>" + _inline(" ".join(para).strip()) + "</p>")
            para.clear()

    while i < n:
        line = lines[i]
        stripped = line.strip()

        if stripped.startswith("```"):
            flush_para()
            i += 1
            code = []
            while i < n and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            i += 1
            out.append("<pre><code>" + escape("\n".join(code)) + "</code></pre>")
            continue

        if stripped.startswith("|") and "|" in stripped[1:]:
            flush_para()
            tbl = []
            while i < n and lines[i].strip().startswith("|"):
                tbl.append(lines[i])
                i += 1
            out.append(_render_table(tbl))
            continue

        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            flush_para()
            level = len(m.group(1))
            txt = m.group(2).strip()
            idattr = f' id="{_slug(txt)}"' if heading_ids else ""
            out.append(f"<h{level}{idattr}>{_inline(txt)}</h{level}>")
            i += 1
            continue

        if re.match(r"^(-{3,}|\*{3,}|_{3,})$", stripped):
            flush_para()
            out.append("<hr>")
            i += 1
            continue

        if re.match(r"^\s*([-*]|\d+\.)\s+", line):
            flush_para()
            items = []
            while i < n:
                lm = re.match(r"^(\s*)([-*]|\d+\.)\s+(.*)$", lines[i])
                if lm:
                    items.append((len(lm.group(1)),
                                  bool(re.match(r"\d+\.", lm.group(2))),
                                  lm.group(3).strip()))
                    i += 1
                elif lines[i].strip() == "" and i + 1 < n and \
                        re.match(r"^\s*([-*]|\d+\.)\s+", lines[i + 1]):
                    i += 1  # blank line between items
                else:
                    break
            out.append(_build_list(items))
            continue

        if stripped == "":
            flush_para()
            i += 1
            continue

        para.append(stripped)
        i += 1

    flush_para()
    return "\n".join(out)


def section_html(name):
    """HTML for the `### <name>` section of the manual, body only (no heading).
    Returns '' if the section isn't found."""
    md = _load_manual()
    if not md:
        return ""
    lines = md.split("\n")
    target = "### " + name
    start = None
    for idx, ln in enumerate(lines):
        if ln.strip() == target:
            start = idx + 1
            break
    if start is None:
        return ""
    end = len(lines)
    for idx in range(start, len(lines)):
        s = lines[idx].lstrip()
        if s.startswith("### ") or s.startswith("## "):
            end = idx
            break
    body = "\n".join(lines[start:end]).strip()
    return render_markdown(body, heading_ids=False)


def full_html():
    """HTML for the entire manual (for the /help page)."""
    md = _load_manual()
    if not md:
        return "<p>Manual not found.</p>"
    return render_markdown(md, heading_ids=True)
