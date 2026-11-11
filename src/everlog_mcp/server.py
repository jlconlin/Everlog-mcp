"""MCP server exposing a read-only view of the Everlog journal."""
from __future__ import annotations

import functools
from typing import Any, Literal

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations

from .images import DEFAULT_MAX_PX, ImageError, sniff_format
from .store import EverlogError, EverlogStore, default_dir

INSTRUCTIONS = """Read-only access to the user's private Everlog journal (macOS journaling app).
- Entry ids come from search_entries / on_this_day; pass them to get_entry for the full text.
- Dates are Everlog's own local days (America/Denver), formatted YYYY-MM-DD.
- search_entries uses SQLite FTS5 syntax: bare words are ANDed, "exact phrase", prefix*, OR, NOT, and
  stemming is on (run matches running). Omit `query` to browse by filter/date only.
- get_entry shows attachments as placeholders like [image a1b2c3d4]; use get_attachment_image with the
  attachment_id from the entry's attachments list to view a photo (or pass include_images=true).
- Journal content is personal; quote only what is needed to answer the question."""

server = MCPServer(name="everlog", instructions=INSTRUCTIONS)
_store: EverlogStore | None = None

READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True, destructive_hint=False, open_world_hint=False)


def set_store(store: EverlogStore | None) -> None:
    """Inject a store (tests) or reset to lazy construction."""
    global _store
    _store = store


def get_store() -> EverlogStore:
    global _store
    if _store is None:
        try:
            _store = EverlogStore(default_dir())
        except EverlogError as e:
            raise ToolError(str(e)) from e
    return _store


def _tool(fn):
    """Register fn as a read-only tool, converting anticipated errors into ToolError."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (EverlogError, ImageError) as e:
            raise ToolError(str(e)) from e
    return server.tool(annotations=READ_ONLY)(wrapper)


@_tool
def list_journals() -> list[dict]:
    """List all journals with entry_count, first_date, last_date (YYYY-MM-DD) and a hidden flag.
    Trashed entries are excluded. Use the names with the `journal` filter of other tools."""
    return get_store().list_journals()


@_tool
def list_tags() -> list[dict]:
    """List all tags (hierarchical tags appear as 'Parent/Child') with entry_count, most used first.
    A tag filter on a parent also matches its descendants."""
    return get_store().list_tags()


@_tool
def search_entries(query: str | None = None, journal: str | None = None, tag: str | None = None,
                   start_date: str | None = None, end_date: str | None = None,
                   bookmarked: bool | None = None,
                   sort: Literal["relevance", "newest", "oldest"] | None = None,
                   limit: int = 20, offset: int = 0) -> dict[str, Any]:
    """Search and browse journal entries. Returns total match count and a page of results, each with
    entry_id, date, journal, title, tags, word_count and a snippet (matches wrapped in «»).

    query: full-text (SQLite FTS5): words are ANDed; "exact phrase"; prefix*; OR; NOT; stemmed. Invalid
      syntax falls back to matching all words. Omit to filter without text search.
    journal: journal name (case-insensitive). tag: tag path; also matches child tags.
    start_date / end_date: inclusive YYYY-MM-DD bounds on the entry's local day.
    sort: relevance (default with query) | newest (default without) | oldest.
    limit: 1-100 (default 20); offset for paging. Fetch full text with get_entry."""
    return get_store().search(query, journal, tag, start_date, end_date, bookmarked, sort, limit, offset)


def _images(store: EverlogStore, entry: dict, max_images: int) -> list[Image]:
    out = []
    for a in entry["attachments"]:
        if a["type"] == "image" and len(out) < max_images:
            try:
                out.append(Image(data=store.attachment_jpeg(a["attachment_id"]), format="jpeg"))
            except (EverlogError, ImageError):
                continue
    return out


@_tool
def get_entry(entry_id: str, include_images: bool = False, max_images: int = 4):
    """Get one entry in full: markdown text (attachments shown as placeholders such as [image a1b2c3d4],
    [audio a1b2c3d4 2:31]) plus date, modified time, journal, tags, location/coordinates/timezone,
    weather, bookmarked/pinned, word_count, the Everlog AI summary (if any) and an attachments list
    (attachment_id, type, size/duration, transcription). With include_images=true, up to max_images
    photos are returned as images after the entry."""
    store = get_store()
    entry = store.get_entry(entry_id)
    if include_images:
        imgs = _images(store, entry, max(0, min(max_images, 10)))
        if imgs:
            return [entry, *imgs]
    return entry


@_tool
def get_attachment_image(attachment_id: str, format: Literal["jpeg", "original"] = "jpeg",
                         max_size: int = DEFAULT_MAX_PX):
    """Return an image attachment. attachment_id is the id from get_entry's attachments list (the 8-char
    prefix in placeholders also works if unambiguous). Errors if the attachment is not an image or its
    bytes are missing.

    format: "jpeg" (default) re-encodes to JPEG, shrunk so its longest side is at most max_size pixels;
      works for HEIC and is much cheaper in tokens. "original" returns the untouched stored bytes in their
      real format (PNG, JPEG, HEIC, ...), full size; some clients/models cannot display HEIC.
    max_size: pixels, default 1568; applies to format="jpeg" only and is ignored for "original"."""
    store = get_store()
    if format == "original":
        data = store.attachment_original(attachment_id)
        return Image(data=data, format=sniff_format(data))
    return Image(data=store.attachment_jpeg(attachment_id, max_size), format="jpeg")


@_tool
def on_this_day(date: str | None = None, journal: str | None = None, limit: int = 50) -> dict[str, Any]:
    """Entries written on the same month/day in earlier years, newest first, with years_ago.
    date: YYYY-MM-DD reference day (default: today in America/Denver); only years before it are included.
    Same result shape as search_entries."""
    return get_store().on_this_day(date, journal, limit)


@_tool
def journal_stats(group_by: Literal["year", "month"] = "year", journal: str | None = None,
                  start_date: str | None = None, end_date: str | None = None) -> dict[str, Any]:
    """Writing statistics: entries and total words per year (or month, 'YYYY-MM'), plus totals.
    Optionally restricted to one journal and/or an inclusive YYYY-MM-DD date range."""
    return get_store().stats(group_by, journal, start_date, end_date)
