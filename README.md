# everlog-mcp

A **read-only** [MCP](https://modelcontextprotocol.io) server for the [Everlog](https://everlog.app) journal app on macOS. It lets an MCP client (Claude Code, Claude Desktop, ...) search and read your journal entries, tags, photos and statistics straight from Everlog's local database. It never writes to or copies Everlog's data.

## Tools

| Tool | Purpose |
| --- | --- |
| `list_journals` | Journals with entry count, first/last date, hidden flag |
| `list_tags` | Tag paths (`Parent/Child`) with entry counts |
| `search_entries` | Full-text (FTS5) search plus journal / tag (incl. descendants) / date / bookmarked filters, paging, sorting |
| `get_entry` | Full markdown text, metadata, location, weather, AI summary, attachments; optional inline photos |
| `get_attachment_image` | An image attachment: `format="jpeg"` (default; re-encoded, shrunk to `max_size`, default 1568) or `format="original"` (untouched bytes, real MIME type) |
| `on_this_day` | Entries from earlier years on the same month/day |
| `journal_stats` | Entries and words per year or month |

Dates are Everlog's own local days (America/Denver), `YYYY-MM-DD`.

## Install / run

Requires macOS, Python 3.13 and [uv](https://docs.astral.sh/uv/).

```sh
uv run everlog-mcp          # speaks MCP over stdio
uv run pytest -q            # tests (synthetic data only)
```

Configuration (environment variable):

- `EVERLOG_DIR` - Everlog data directory. Default: `~/Library/Group Containers/group.hummingbird`.

### Register with a client (examples; not done for you)

```sh
claude mcp add everlog -- uv --directory /path/to/Everlog-mcp run everlog-mcp
```

Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "everlog": {
      "command": "uv",
      "args": ["--directory", "/path/to/Everlog-mcp", "run", "everlog-mcp"]
    }
  }
}
```

## macOS permission

Everlog's data lives in a Group Container. The app that launches the server (Claude Desktop, your terminal, ...) may need **Full Disk Access** or the "access data from other apps" permission in System Settings > Privacy & Security. If the database cannot be opened, tools return an error explaining this.

## Privacy

Whatever the tools return (entry text, summaries, photos, locations) is sent to your MCP client's model provider as part of the conversation. Only connect this server to clients and models you are comfortable sharing your journal with. The server itself makes no network connections and logs only counts/timings to stderr.

## How it works / data-format notes

- Everlog stores Core Data SQLite files `Hummingbird.sqlite` (entries, journals, tags, places, weather, AI summaries) and `AttachmentData.sqlite` (attachment bytes), in WAL mode. Both are opened with `mode=ro` (never `immutable`, so recent WAL content is visible).
- At first use the server builds an in-memory SQLite FTS5 index (porter stemming) of entry text plus audio transcriptions, and re-checks `PRAGMA data_version` before every call; if Everlog committed changes, the index is rebuilt (about a second for ~10k entries).
- `ZTEXT` is the real markdown (`ZCONTENT` is a placeholder). Columns named `ZENCRYPTED*` actually hold plaintext and are preferred over their unprefixed twins. Trashed entries are excluded.
- Timestamps are Core Data epoch (Unix + 978307200). Day filtering uses `ZYEARMONTHDAYID` (Everlog's own America/Denver day bucket). Coordinates of 0/0 mean "none".
- Attachments are referenced in text as `![attachment](<32-hex id>)`. Blob bytes in `ZATTACHMENTDATA.ZDATA` start with `0x01` (inline bytes) or `0x02` (ASCII UUID of a file under `.AttachmentData_SUPPORT/_EXTERNAL_DATA/`). When several rows share an identifier the largest payload is used.
- Images are converted to JPEG (quality 80, longest side <= 1568 px by default) with macOS `/usr/bin/sips`, which also handles HEIC. `get_attachment_image(format="original")` instead returns the untouched stored bytes, with the MIME type sniffed from them; `max_size` applies to the JPEG format only.
- Weather: temperature in deg C, pressure converted from Pa to hPa, humidity 0 means "no data".

## License

[MIT](LICENSE)

## Using the store as a library

`everlog_mcp.store` has no dependency on `mcp` or the server module, so other tools can import it directly. `EverlogStore(directory)` opens the data directory read-only; the methods below return raw, unshaped data (frozen dataclasses defined in `store.py`).

- `store.iter_entries()` yields an `EntryRecord` for every non-trashed entry in chronological order (ZDATE, then pk). Fields: `id`, `created` and `modified` (tz-aware America/Denver datetimes), `journal`, `text` (raw markdown, attachment references intact), `tags` (raw `Parent/Child` paths), `place` (`PlaceRecord` or None), `latitude`/`longitude`, `weather` (`WeatherRecord` or None, raw units), `bookmarked`, `pinned`, `word_count`, `year_month_day_id`, `attachments` (`AttachmentRecord` list in order).
- `store.attachment_bytes(identifier)` returns the raw bytes of an attachment (the largest copy if several rows share the identifier), or `None` if missing.
- `store.journal_infos()` returns a list of `JournalInfo(name, hidden)`.
