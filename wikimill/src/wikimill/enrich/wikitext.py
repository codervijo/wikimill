"""Extracting a link's context from an article's wikitext.

This is the payload the whole pipeline is ordered to defer: which section cited
the URL, what the anchor text said, and whether it sat in a `<ref>`, an
`{{Official website}}`, or a bare "External links" list. Knowing *what a domain
was cited for* is what separates a candidate worth acquiring from a dead URL.

Two honest outcomes are first-class here, not failures:

* **`url_not_found_in_wikitext`** — the link came from template expansion (a URL
  pulled from Wikidata, say) and has no literal occurrence in the source. The
  link is real; only its surface context is absent. Recording that is more
  useful than a blank field with no explanation.
* **`page_missing`** — the page is not in the block the index pointed at,
  usually because the article was deleted or moved between dump runs.

Wikipedia's own maintenance templates are harvested too: `{{dead link}}` means
an editor already noticed, and `{{webarchive}}` / `archive-url=` records what
the citation was replaced with.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

CONTEXT_CHARS = 300
LEAD_SECTION = "(lead)"

# Section headings: == Title ==, === Sub ===, …
_HEADING = re.compile(r"^(={2,6})\s*(.+?)\s*\1\s*$", re.MULTILINE)
# [url anchor text] and [url]
_EXT_LINK = re.compile(r"\[(?P<url>(?:https?:)?//[^\s\]]+)(?:\s+(?P<text>[^\]]*))?\]")
_DEAD_LINK = re.compile(r"\{\{\s*(dead[ _]link|dl|broken[ _]link)", re.IGNORECASE)
_CITE_OPEN = re.compile(r"\{\{\s*([A-Za-z][\w\s\-]*)", re.IGNORECASE)
_REF_OPEN = re.compile(r"<ref(?P<attrs>[^>/]*)/?>", re.IGNORECASE)
_REF_NAME = re.compile(r'name\s*=\s*"?([^">/]+)"?', re.IGNORECASE)
_ARCHIVE_URL_PARAM = re.compile(
    r"\|\s*archive-?url\s*=\s*(?P<url>\S+)", re.IGNORECASE
)
_ARCHIVE_DATE_PARAM = re.compile(
    r"\|\s*archive-?date\s*=\s*(?P<date>[^|}\n]+)", re.IGNORECASE
)

# Section titles that mark a non-citation link list.
_EXTERNAL_SECTIONS = {"external links", "further reading", "sources", "bibliography"}


@dataclass
class LinkContext:
    section: str | None = None
    section_level: int | None = None
    anchor_text: str | None = None
    link_kind: str | None = None
    ref_name: str | None = None
    template_name: str | None = None
    context_excerpt: str | None = None
    dead_link_tagged: bool = False
    archive_url: str | None = None
    archive_date: str | None = None
    found: bool = False


def _headings(text: str) -> list[tuple[int, int, str]]:
    """(position, level, title) for every heading, in document order."""
    return [
        (m.start(), len(m.group(1)), m.group(2).strip())
        for m in _HEADING.finditer(text)
    ]


def section_at(headings: list[tuple[int, int, str]], position: int) -> tuple[str, int | None]:
    """The nearest heading above `position`, or the lead."""
    current, level = LEAD_SECTION, None
    for start, depth, title in headings:
        if start > position:
            break
        current, level = title, depth
    return current, level


def _comparable(url: str) -> str:
    """A loose key for matching a stored URL against wikitext.

    Deliberately loose: the stored URL has been normalized (v1.D) while the
    wikitext holds whatever the editor typed. Comparing scheme-less, `www`-less,
    slash-trimmed forms finds the occurrence without re-implementing
    normalization, which would only drift from it.
    """
    parts = urlsplit(url if "//" in url else f"//{url}")
    host = (parts.hostname or "").lower().removeprefix("www.")
    path = parts.path.rstrip("/")
    query = f"?{parts.query}" if parts.query else ""
    return f"{host}{path}{query}"


def find_link(wikitext: str, target_url: str) -> tuple[int, int, str | None] | None:
    """Locate the URL in the wikitext. Returns (start, end, anchor) or None."""
    want = _comparable(target_url)
    if not want:
        return None

    # Bracketed links first — they carry anchor text.
    for match in _EXT_LINK.finditer(wikitext):
        if _comparable(match.group("url")) == want:
            anchor = (match.group("text") or "").strip() or None
            return match.start(), match.end(), anchor

    # Then a bare occurrence (template parameters, plain text).
    idx = wikitext.find(target_url)
    if idx == -1:
        # Retry without the scheme: `|url=example.com/x` is common.
        stripped = target_url.split("://", 1)[-1]
        idx = wikitext.find(stripped)
        if idx == -1:
            return None
        return idx, idx + len(stripped), None
    return idx, idx + len(target_url), None


def _enclosing_template(wikitext: str, position: int) -> str | None:
    """The name of the innermost `{{template}}` containing `position`."""
    depth = 0
    for i in range(position, max(0, position - 4000), -1):
        if wikitext.startswith("}}", i):
            depth += 1
        elif wikitext.startswith("{{", i):
            if depth == 0:
                match = _CITE_OPEN.match(wikitext[i:])
                return match.group(1).strip().lower() if match else None
            depth -= 1
    return None


def _enclosing_ref(wikitext: str, position: int) -> tuple[bool, str | None]:
    """Whether `position` sits inside a `<ref>`, and that ref's name."""
    open_idx = wikitext.rfind("<ref", 0, position)
    if open_idx == -1:
        return False, None
    close_idx = wikitext.find("</ref>", open_idx)
    self_closed = wikitext.find("/>", open_idx, position)
    if close_idx != -1 and close_idx < position and self_closed == -1:
        return False, None  # the ref closed before our link
    match = _REF_OPEN.match(wikitext[open_idx:])
    if not match:
        return False, None
    name_match = _REF_NAME.search(match.group("attrs") or "")
    return True, name_match.group(1).strip() if name_match else None


def _classify_kind(section: str, in_ref: bool, template: str | None) -> str:
    if in_ref:
        return "citation"
    if template:
        if template.startswith("cite") or template.startswith("citation"):
            return "citation"
        if "infobox" in template:
            return "infobox"
        return "template"
    if section.strip().lower() in _EXTERNAL_SECTIONS:
        return (
            "further_reading"
            if section.strip().lower() == "further reading"
            else "external_links_section"
        )
    return "inline"


def extract(wikitext: str, target_url: str) -> LinkContext:
    """Pull everything we can say about one URL's placement in one article."""
    context = LinkContext()
    located = find_link(wikitext, target_url)
    if located is None:
        # Expected for template-expanded links — information, not an error.
        return context

    start, end, anchor = located
    context.found = True
    context.anchor_text = anchor

    section, level = section_at(_headings(wikitext), start)
    context.section = section
    context.section_level = level

    in_ref, ref_name = _enclosing_ref(wikitext, start)
    template = _enclosing_template(wikitext, start)
    context.ref_name = ref_name
    context.template_name = template
    context.link_kind = _classify_kind(section, in_ref, template)

    window_start = max(0, start - CONTEXT_CHARS // 2)
    window = wikitext[window_start : end + CONTEXT_CHARS // 2]
    context.context_excerpt = re.sub(r"\s+", " ", window).strip()[:CONTEXT_CHARS]

    # Maintenance templates near the link: an editor may already have flagged it.
    neighbourhood = wikitext[max(0, start - 500) : end + 500]
    context.dead_link_tagged = bool(_DEAD_LINK.search(neighbourhood))
    archive_match = _ARCHIVE_URL_PARAM.search(neighbourhood)
    if archive_match:
        context.archive_url = archive_match.group("url").strip()
        date_match = _ARCHIVE_DATE_PARAM.search(neighbourhood)
        if date_match:
            context.archive_date = date_match.group("date").strip()
    return context
