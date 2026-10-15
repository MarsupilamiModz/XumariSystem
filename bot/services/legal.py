"""Legal documents (terms, privacy policy, cancellation policy) stored as Markdown files.

Files: ``<legal_dir>/<doc>.<locale>.md`` e.g. ``legal/terms.de.md``.
"""

from __future__ import annotations

import asyncio
import html
import re
from pathlib import Path

DOCUMENTS: tuple[str, ...] = ("terms", "privacy", "withdrawal")
_INLINE_BOLD = re.compile(r"\*\*(.+?)\*\*")
_INLINE_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")


async def load_document(directory: Path, doc: str, locale: str) -> str | None:
    if doc not in DOCUMENTS:
        return None
    for candidate in (directory / f"{doc}.{locale}.md", directory / f"{doc}.de.md", directory / f"{doc}.en.md"):
        if candidate.is_file():
            return await asyncio.to_thread(candidate.read_text, "utf-8")
    return None


def _inline(text: str) -> str:
    escaped = html.escape(text)
    escaped = _INLINE_LINK.sub(r'<a href="\2">\1</a>', escaped)
    return _INLINE_BOLD.sub(r"<strong>\1</strong>", escaped)


def markdown_to_html(markdown: str) -> str:
    """Small, safe Markdown subset: headings, paragraphs, bullet lists, bold, links."""
    out: list[str] = []
    in_list = False
    paragraph: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            out.append(f"<p>{_inline(' '.join(paragraph))}</p>")
            paragraph.clear()

    for raw in markdown.splitlines():
        line = raw.rstrip()
        if line.startswith(("- ", "* ")):
            flush_paragraph()
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{_inline(line[2:])}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        heading = re.match(r"^(#{1,4})\s+(.*)$", line)
        if heading:
            flush_paragraph()
            level = len(heading.group(1))
            out.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
        elif not line.strip():
            flush_paragraph()
        else:
            paragraph.append(line.strip())
    flush_paragraph()
    if in_list:
        out.append("</ul>")
    return "\n".join(out)


def html_page(title: str, body_html: str, accent: str) -> str:
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)}</title><style>"
        "body{margin:0;font:16px/1.6 system-ui,'Segoe UI',Arial,sans-serif;color:#1f2328;background:#f6f8fa}"
        f"header{{background:{accent};height:6px}}main{{max-width:780px;margin:32px auto;background:#fff;"
        "padding:32px 40px;border-radius:12px;box-shadow:0 1px 3px rgba(0,0,0,.08)}h1,h2,h3{line-height:1.25}"
        "a{color:#0969da}@media(prefers-color-scheme:dark){body{background:#0d1117;color:#e6edf3}"
        "main{background:#161b22}a{color:#58a6ff}}</style></head>"
        f"<body><header></header><main>{body_html}</main></body></html>"
    )


def strip_markdown(markdown: str) -> str:
    """Plain-ish text for Discord (keeps bold/links, turns headings into bold lines)."""
    lines = []
    for line in markdown.splitlines():
        heading = re.match(r"^#{1,4}\s+(.*)$", line)
        lines.append(f"**{heading.group(1)}**" if heading else line)
    return "\n".join(lines).strip()
