"""Read-only access to Everlog's Core Data stores plus an in-memory FTS5 index."""
from __future__ import annotations

import os
import re
import sqlite3
import sys
import threading
import time
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date as _date, datetime, timezone
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

from .images import DEFAULT_MAX_PX, to_jpeg

DEFAULT_DIR = Path.home() / "Library/Group Containers/group.hummingbird"
TZ = ZoneInfo("America/Denver")
COREDATA_EPOCH = 978307200
ENT_AUDIO, ENT_FILE, ENT_IMAGE, ENT_VIDEO = 3, 6, 8, 13
ENT_NAMES = {ENT_AUDIO: "audio", ENT_FILE: "file", ENT_IMAGE: "image", ENT_VIDEO: "video"}
ATTACHMENT_RE = re.compile(r"!\[[^\]]*\]\(([0-9A-Fa-f]{32})\)")


class EverlogError(Exception):
    """Anticipated, user-presentable error."""


def log(msg: str) -> None:
    print(f"[everlog-mcp] {msg}", file=sys.stderr, flush=True)


def default_dir() -> Path:
    env = os.environ.get("EVERLOG_DIR")
    return Path(env).expanduser() if env else DEFAULT_DIR


def _ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def from_coredata(ts) -> datetime | None:
    return None if ts is None else datetime.fromtimestamp(ts + COREDATA_EPOCH, timezone.utc)


def local_iso(ts) -> str | None:
    d = from_coredata(ts)
    return d.astimezone(TZ).isoformat(timespec="seconds") if d else None


def ymd_to_iso(ymd: int) -> str:
    return f"{ymd // 10000:04d}-{ymd // 100 % 100:02d}-{ymd % 100:02d}"


def parse_date(s: str | None, name: str = "date") -> int | None:
    """ISO YYYY-MM-DD -> YYYYMMDD int."""
    if s is None or s == "":
        return None
    try:
        d = _date.fromisoformat(s.strip())
    except ValueError:
        raise EverlogError(f"Invalid {name} {s!r}: use ISO format YYYY-MM-DD.") from None
    return d.year * 10000 + d.month * 100 + d.day


def clean_line(line: str) -> str:
    line = re.sub(r"^#+\s*", "", line.strip())
    line = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", line)
    line = re.sub(r"[*_`>\[\]]", "", line)
    return re.sub(r"\s+", " ", line).strip()


def title_of(text: str, maxlen: int = 80) -> str:
    for line in text.splitlines():
        line = clean_line(line)
        if line:
            return line[:maxlen].rstrip()
    return ""


def preview_of(text: str, n: int = 200) -> str:
    out = re.sub(r"\s+", " ", " ".join(clean_line(l) for l in text.splitlines())).strip()
    return out if len(out) <= n else out[:n].rstrip() + "…"


def fmt_duration(sec) -> str:
    if not sec:
        return ""
    sec = int(round(sec))
    return f"{sec // 60}:{sec % 60:02d}"


def _nonzero_coord(lat, lon):
    if lat is None or lon is None or (lat == 0 and lon == 0):
        return None
    return round(lat, 6), round(lon, 6)


def _fts_quote_fallback(query: str) -> str:
    return " AND ".join('"' + tok.replace('"', '""') + '"' for tok in query.split())


SCHEMA = """
create table meta(pk integer primary key, identifier text, identifier_lc text, journal_pk integer,
  journal text, ymd integer, ts real, created text, title text, preview text,
  bookmarked integer, pinned integer, word_count integer);
create index meta_ymd on meta(ymd); create index meta_id on meta(identifier_lc);
create table entry_tag(pk integer, path text, path_lc text);
create index entry_tag_pk on entry_tag(pk);
create virtual table fts using fts5(text, tokenize='porter unicode61 remove_diacritics 2');
"""


# ------------------------------------------------------------------ export API
# Public, raw (no shaping/truncation/rounding) records for programmatic consumers such as exporters.
@dataclass(frozen=True)
class JournalInfo:
    name: str
    hidden: bool


@dataclass(frozen=True)
class AttachmentRecord:
    identifier: str            # ZIDENTIFIER as stored (32 hex)
    kind: str                  # "audio" | "file" | "image" | "video" | "other"
    extension: str | None      # ZFILEEXTENSION as stored (no dot), may be None
    name: str | None           # ZNAME
    title: str | None          # ZTITLE
    duration: float | None     # seconds
    latitude: float | None     # raw, may be 0.0
    longitude: float | None
    created: datetime | None   # ZDATECREATED, tz-aware America/Denver
    order: int | None          # ZORDER


@dataclass(frozen=True)
class PlaceRecord:
    name: str | None
    locality: str | None
    administrative_area: str | None
    country: str | None
    timezone: str | None
    latitude: float | None     # raw (encrypted column preferred when truthy), may be 0.0
    longitude: float | None


@dataclass(frozen=True)
class WeatherRecord:
    condition: str | None
    temperature_c: float | None
    humidity: float | None     # raw 0-1; 0.0 means no data
    pressure_pa: float | None  # raw Pa
    wind_speed: float | None
    wind_direction: float | None  # raw ZWINDDIRECTION, degrees
    uv_index: int | None


@dataclass(frozen=True)
class EntryRecord:
    id: str                    # ZIDENTIFIER
    created: datetime          # exact ZDATE, tz-aware America/Denver (None-date rows get the epoch)
    modified: datetime | None
    journal: str
    text: str                  # raw markdown; ![attachment](<32-hex>) references intact
    tags: list[str]            # raw "Parent/Child" paths, sorted
    place: PlaceRecord | None
    latitude: float | None     # entry's own coordinates, raw, may be 0.0
    longitude: float | None
    weather: WeatherRecord | None
    bookmarked: bool
    pinned: bool
    word_count: int
    year_month_day_id: int     # ZYEARMONTHDAYID as stored (0 if NULL)
    attachments: list[AttachmentRecord]


def _col(row, name):
    return row[name] if row is not None and name in row.keys() else None


class EverlogStore:
    def __init__(self, directory: str | Path):
        self.dir = Path(directory).expanduser()
        main, att = self.dir / "Hummingbird.sqlite", self.dir / "AttachmentData.sqlite"
        if not main.exists():
            raise EverlogError(
                f"Everlog database not found at {main}. Set the EVERLOG_DIR environment variable to the "
                f"Everlog data directory (default: {DEFAULT_DIR}). On macOS the MCP host app (Claude, "
                "Terminal, ...) may also need Full Disk Access or the 'access data from other apps' "
                "permission in System Settings > Privacy & Security.")
        try:
            self.db = _ro(main)
            self.db.execute("select 1 from ZENTRY limit 1")
            self.att = _ro(att) if att.exists() else None
        except sqlite3.Error as e:
            raise EverlogError(
                f"Could not open Everlog database at {main} read-only ({e}). On macOS the MCP host app may "
                "need Full Disk Access / 'access data from other apps' permission; EVERLOG_DIR overrides the path.") from e
        self.lock = threading.RLock()
        self.mem: sqlite3.Connection | None = None
        self._version: int | None = None
        self.last_build_seconds = 0.0
        self.rebuild()

    # ------------------------------------------------------------------ index
    def _data_version(self) -> int:
        return self.db.execute("pragma data_version").fetchone()[0]

    def ensure_fresh(self) -> None:
        with self.lock:
            if self._data_version() != self._version:
                self.rebuild()

    def rebuild(self) -> None:
        with self.lock:
            t0 = time.perf_counter()
            version = self._data_version()
            db = self.db
            journals = {r["Z_PK"]: r["ZENCRYPTEDNAME"] or r["ZNAME"] or f"Journal {r['Z_PK']}"
                        for r in db.execute("select * from ZJOURNAL")}
            self.journals = journals
            self.journal_hidden = {r["Z_PK"]: bool(r["ZISHIDDEN"]) for r in db.execute("select * from ZJOURNAL")}
            tag_rows = {r["Z_PK"]: r for r in db.execute("select * from ZTAG")}

            def tag_path(pk, depth=0):
                r = tag_rows.get(pk)
                if r is None or depth > 10:
                    return ""
                title = (r["ZENCRYPTEDTITLE"] or r["ZTITLE"] or "").strip()
                parent = tag_path(r["ZPARENT"], depth + 1) if r["ZPARENT"] else ""
                return f"{parent}/{title}" if parent and title else (title or parent)

            tag_names = {pk: tag_path(pk) for pk in tag_rows}
            transcripts = defaultdict(list)
            for r in db.execute("select ZENTRY, ZTRANSCRIPTION from ZATTACHMENT where ZTRANSCRIPTION is not null "
                                "and ZTRANSCRIPTION != '' order by ZORDER"):
                transcripts[r["ZENTRY"]].append(r["ZTRANSCRIPTION"])

            mem = sqlite3.connect(":memory:", check_same_thread=False)
            mem.row_factory = sqlite3.Row
            mem.executescript(SCHEMA)
            meta_rows, fts_rows = [], []
            for r in db.execute("select * from ZENTRY where coalesce(ZISTRASHED,0)=0"):
                text = r["ZTEXT"] or ""
                local = from_coredata(r["ZDATE"])
                local = local.astimezone(TZ) if local else None
                ymd = r["ZYEARMONTHDAYID"] or (int(local.strftime("%Y%m%d")) if local else 0)
                clean = ATTACHMENT_RE.sub("", text)
                tr = transcripts.get(r["Z_PK"])
                if tr:
                    clean += "\n" + "\n".join(tr)
                ident = r["ZIDENTIFIER"] or ""
                meta_rows.append((r["Z_PK"], ident, ident.lower(), r["ZJOURNAL"],
                                  journals.get(r["ZJOURNAL"], "Unfiled"), ymd,
                                  (r["ZDATE"] or 0) + COREDATA_EPOCH,
                                  local.isoformat(timespec="seconds") if local else None,
                                  title_of(text), preview_of(text), int(bool(r["ZISBOOKMARKED"])),
                                  int(bool(r["ZPINNED"])), r["ZWORDCOUNT"] or 0))
                fts_rows.append((r["Z_PK"], clean))
            mem.executemany("insert into meta values(?,?,?,?,?,?,?,?,?,?,?,?,?)", meta_rows)
            mem.executemany("insert into fts(rowid, text) values(?,?)", fts_rows)
            live = {m[0] for m in meta_rows}
            seen, tag_rows_out = set(), []
            for r in db.execute("select Z_17ENTRIES1 e, Z_25TAGS t from Z_17TAGS"):
                name = tag_names.get(r["t"])
                if name and r["e"] in live and (r["e"], name) not in seen:
                    seen.add((r["e"], name))
                    tag_rows_out.append((r["e"], name, name.lower()))
            mem.executemany("insert into entry_tag values(?,?,?)", tag_rows_out)
            mem.commit()
            if self.mem is not None:
                self.mem.close()
            self.mem = mem
            self._version = version
            self.last_build_seconds = time.perf_counter() - t0
            log(f"index built: {len(meta_rows)} entries in {self.last_build_seconds:.2f}s")

    # ---------------------------------------------------------------- helpers
    def _tags_for(self, pks: list[int]) -> dict[int, list[str]]:
        out: dict[int, list[str]] = defaultdict(list)
        if pks:
            q = ",".join("?" * len(pks))
            for r in self.mem.execute(f"select pk, path from entry_tag where pk in ({q}) order by path", pks):
                out[r["pk"]].append(r["path"])
        return out

    def _resolve_journal(self, name: str) -> int:
        for pk, n in self.journals.items():
            if n.lower() == name.strip().lower():
                return pk
        import difflib
        close = difflib.get_close_matches(name, list(self.journals.values()), n=5, cutoff=0.4)
        hint = f" Close matches: {close}." if close else f" Available: {sorted(self.journals.values())}."
        raise EverlogError(f"Unknown journal {name!r}.{hint}")

    def _check_tag(self, tag: str) -> str:
        t = tag.strip().strip("/").lower()
        paths = {r[0] for r in self.mem.execute("select distinct path from entry_tag")}
        if not any(p.lower() == t or p.lower().startswith(t + "/") for p in paths):
            import difflib
            close = difflib.get_close_matches(tag, sorted(paths), n=5, cutoff=0.4)
            raise EverlogError(f"Unknown tag {tag!r}." + (f" Close matches: {close}." if close else
                                                          " Use list_tags to see available tags."))
        return t

    def _filters(self, journal=None, tag=None, start=None, end=None, bookmarked=None):
        where, params = [], []
        if journal:
            where.append("m.journal_pk = ?")
            params.append(self._resolve_journal(journal))
        if tag:
            t = self._check_tag(tag)
            where.append("exists (select 1 from entry_tag et where et.pk = m.pk and "
                         "(et.path_lc = ? or substr(et.path_lc, 1, ?) = ?))")
            params += [t, len(t) + 1, t + "/"]
        if start is not None:
            where.append("m.ymd >= ?"); params.append(start)
        if end is not None:
            where.append("m.ymd <= ?"); params.append(end)
        if bookmarked is not None:
            where.append("m.bookmarked = ?"); params.append(int(bookmarked))
        return where, params

    def _shape(self, rows, snippets: dict | None = None):
        tags = self._tags_for([r["pk"] for r in rows])
        return [{
            "entry_id": r["identifier"], "date": ymd_to_iso(r["ymd"]), "created": r["created"],
            "journal": r["journal"], "title": r["title"], "tags": tags.get(r["pk"], []),
            "word_count": r["word_count"], "bookmarked": bool(r["bookmarked"]),
            "snippet": (snippets or {}).get(r["pk"]) or r["preview"],
        } for r in rows]

    # ----------------------------------------------------------------- search
    def search(self, query=None, journal=None, tag=None, start_date=None, end_date=None, bookmarked=None,
               sort=None, limit=20, offset=0, extra_where=None, extra_params=()):
        with self.lock:
            self.ensure_fresh()
            limit = max(1, min(int(limit), 100))
            offset = max(0, int(offset))
            query = (query or "").strip() or None
            sort = sort or ("relevance" if query else "newest")
            if sort not in ("relevance", "newest", "oldest"):
                raise EverlogError("sort must be 'relevance', 'newest' or 'oldest'.")
            if sort == "relevance" and not query:
                sort = "newest"
            where, params = self._filters(journal, tag, parse_date(start_date, "start_date"),
                                          parse_date(end_date, "end_date"), bookmarked)
            if extra_where:
                where.append(extra_where); params += list(extra_params)
            order = {"relevance": "fts.rank, m.ts desc", "newest": "m.ts desc, m.pk desc",
                     "oldest": "m.ts asc, m.pk asc"}[sort]
            wsql = (" and " + " and ".join(where)) if where else ""
            if query:
                def run(q):
                    total = self.mem.execute(
                        f"select count(*) from fts join meta m on m.pk = fts.rowid where fts match ?{wsql}",
                        [q, *params]).fetchone()[0]
                    rows = self.mem.execute(
                        f"select m.*, snippet(fts, 0, '«', '»', '…', 24) as snip from fts join meta m "
                        f"on m.pk = fts.rowid where fts match ?{wsql} order by {order} limit ? offset ?",
                        [q, *params, limit, offset]).fetchall()
                    return total, rows
                try:
                    total, rows = run(query)
                except sqlite3.OperationalError:
                    try:
                        total, rows = run(_fts_quote_fallback(query))
                    except sqlite3.OperationalError as e:
                        raise EverlogError(f"Could not parse search query {query!r}: {e}") from e
                snips = {r["pk"]: re.sub(r"\s+", " ", r["snip"]).strip() for r in rows}
            else:
                w = (" where " + " and ".join(where)) if where else ""
                total = self.mem.execute(f"select count(*) from meta m{w}", params).fetchone()[0]
                rows = self.mem.execute(f"select m.* from meta m{w} order by {order} limit ? offset ?",
                                        [*params, limit, offset]).fetchall()
                snips = None
            return {"total": total, "offset": offset, "limit": limit, "results": self._shape(rows, snips)}

    def on_this_day(self, date=None, journal=None, limit=50):
        with self.lock:
            ymd = parse_date(date) if date else int(datetime.now(TZ).strftime("%Y%m%d"))
            year, md = ymd // 10000, ymd % 10000
            res = self.search(journal=journal, sort="newest", limit=limit,
                              extra_where="m.ymd % 10000 = ? and m.ymd / 10000 < ?", extra_params=(md, year))
            for r in res["results"]:
                r["years_ago"] = year - int(r["date"][:4])
            res["date"] = ymd_to_iso(ymd)
            return res

    # -------------------------------------------------------------- listings
    def list_journals(self):
        with self.lock:
            self.ensure_fresh()
            stats = {r["journal_pk"]: r for r in self.mem.execute(
                "select journal_pk, count(*) n, min(ymd) lo, max(ymd) hi from meta group by journal_pk")}
            out = []
            for pk, name in self.journals.items():
                s = stats.get(pk)
                out.append({"name": name, "entry_count": s["n"] if s else 0,
                            "first_date": ymd_to_iso(s["lo"]) if s else None,
                            "last_date": ymd_to_iso(s["hi"]) if s else None,
                            "hidden": self.journal_hidden.get(pk, False)})
            return sorted(out, key=lambda j: -j["entry_count"])

    def list_tags(self):
        with self.lock:
            self.ensure_fresh()
            return [{"tag": r["path"], "entry_count": r["n"]} for r in self.mem.execute(
                "select path, count(*) n from entry_tag group by path order by n desc, path")]

    def stats(self, group_by="year", journal=None, start_date=None, end_date=None):
        with self.lock:
            self.ensure_fresh()
            if group_by not in ("year", "month"):
                raise EverlogError("group_by must be 'year' or 'month'.")
            where, params = self._filters(journal, None, parse_date(start_date, "start_date"),
                                          parse_date(end_date, "end_date"))
            w = (" where " + " and ".join(where)) if where else ""
            div = 10000 if group_by == "year" else 100
            periods = []
            for r in self.mem.execute(f"select m.ymd / {div} p, count(*) n, sum(m.word_count) w from meta m{w} "
                                      "group by p order by p", params):
                label = f"{r['p']:04d}" if group_by == "year" else f"{r['p'] // 100:04d}-{r['p'] % 100:02d}"
                periods.append({"period": label, "entries": r["n"], "words": r["w"] or 0})
            return {"group_by": group_by, "periods": periods,
                    "total_entries": sum(p["entries"] for p in periods),
                    "total_words": sum(p["words"] for p in periods)}

    # ----------------------------------------------------------------- entry
    def get_entry(self, entry_id: str, with_attachments: bool = True):
        with self.lock:
            self.ensure_fresh()
            m = self.mem.execute("select pk from meta where identifier_lc = ?", (entry_id.strip().lower(),)).fetchone()
            if not m:
                raise EverlogError(f"No entry with id {entry_id!r}. Entry ids come from search_entries / on_this_day.")
            row = self.db.execute("select * from ZENTRY where Z_PK = ?", (m["pk"],)).fetchone()
            if row is None or row["ZISTRASHED"]:
                raise EverlogError(f"No entry with id {entry_id!r}.")
            atts = self.db.execute("select * from ZATTACHMENT where ZENTRY = ? order by ZORDER, Z_PK",
                                   (row["Z_PK"],)).fetchall()
            by_id = {a["ZIDENTIFIER"].lower(): a for a in atts if a["ZIDENTIFIER"]}

            def placeholder(mo):
                a = by_id.get(mo.group(1).lower())
                short = mo.group(1)[:8].lower()
                if a is None:
                    return f"[attachment {short} (missing)]"
                kind = ENT_NAMES.get(a["Z_ENT"], "attachment")
                if kind == "file":
                    return f"[file {a['ZNAME'] or a['ZTITLE'] or short}]"
                dur = f" {fmt_duration(a['ZDURATION'])}" if kind == "audio" and a["ZDURATION"] else ""
                return f"[{kind} {short}{dur}]"

            text = ATTACHMENT_RE.sub(placeholder, row["ZTEXT"] or "")
            place = self.db.execute("select * from ZPLACE where Z_PK = ?", (row["ZPLACE"],)).fetchone() \
                if row["ZPLACE"] else None
            loc_text, place_coord = _build_location(place)
            att_coord = next((c for a in atts if (c := _nonzero_coord(a["ZLATITUDE"], a["ZLONGITUDE"]))), None)
            coord = _nonzero_coord(row["ZENCRYPTEDLATITUDE"] or row["ZLATITUDE"],
                                   row["ZENCRYPTEDLONGITUDE"] or row["ZLONGITUDE"]) or place_coord or att_coord
            weather = self.db.execute("select * from ZWEATHER where Z_PK = ?", (row["ZWEATHER"],)).fetchone() \
                if row["ZWEATHER"] else None
            verdict = self.db.execute("select ZSUMMARY from ZENTRYVERDICT where ZENTRYIDENTIFIER = ?",
                                      (row["ZIDENTIFIER"],)).fetchone()
            md = self.mem.execute("select * from meta where pk = ?", (row["Z_PK"],)).fetchone()
            tags = self._tags_for([row["Z_PK"]]).get(row["Z_PK"], [])
            return {
                "entry_id": row["ZIDENTIFIER"], "date": ymd_to_iso(md["ymd"]), "created": md["created"],
                "modified": local_iso(row["ZDATEMODIFIED"]), "journal": md["journal"], "tags": tags,
                "location": loc_text or None,
                "latitude": coord[0] if coord else None, "longitude": coord[1] if coord else None,
                "timezone": ((place["ZENCRYPTEDTIMEZONE"] or place["ZTIMEZONE"]) if place is not None else None),
                "weather": _weather_fields(weather) or None,
                "bookmarked": bool(row["ZISBOOKMARKED"]), "pinned": bool(row["ZPINNED"]),
                "word_count": row["ZWORDCOUNT"],
                "summary": (verdict["ZSUMMARY"] if verdict and verdict["ZSUMMARY"] else None),
                "attachments": [_attachment_info(a) for a in atts],
                "text": text,
            }

    # ----------------------------------------------------------- attachments
    def _find_attachment(self, attachment_id: str):
        aid = attachment_id.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{8,32}", aid):
            raise EverlogError(f"Invalid attachment id {attachment_id!r}; expected the 32-hex id (8+ hex chars accepted).")
        rows = self.db.execute("select * from ZATTACHMENT where lower(ZIDENTIFIER) like ? || '%'", (aid,)).fetchall()
        if not rows:
            raise EverlogError(f"No attachment with id {attachment_id!r}. Attachment ids are listed by get_entry.")
        if len(rows) > 1:
            raise EverlogError(f"Attachment id prefix {attachment_id!r} is ambiguous; give more characters.")
        return rows[0]

    def _attachment_bytes(self, identifier: str) -> bytes | None:
        """Largest decoded payload among all rows for this identifier."""
        if self.att is None:
            return None
        best = None
        for row in self.att.execute("select ZDATA from ZATTACHMENTDATA where ZIDENTIFIER = ? order by Z_PK",
                                    (identifier,)):
            data = self._decode_blob(row["ZDATA"]) if row["ZDATA"] is not None else None
            if data is not None and (best is None or len(data) > len(best)):
                best = data
        return best

    def _decode_blob(self, blob) -> bytes | None:
        blob = bytes(blob)
        if blob[:1] == b"\x01":
            return blob[1:]
        if blob[:1] == b"\x02":
            name = blob[1:].rstrip(b"\x00").decode("ascii", "ignore")
            if not re.fullmatch(r"[\w.-]+", name):
                return None
            path = self.dir / ".AttachmentData_SUPPORT/_EXTERNAL_DATA" / name
            return path.read_bytes() if path.exists() else None
        return None

    # ------------------------------------------------------------- export API
    def journal_infos(self) -> list[JournalInfo]:
        """All journals (name, hidden flag), ordered by primary key."""
        with self.lock:
            self.ensure_fresh()
            return [JournalInfo(n, self.journal_hidden.get(pk, False)) for pk, n in self.journals.items()]

    def iter_entries(self) -> Iterator[EntryRecord]:
        """Every non-trashed entry, chronological (ZDATE, then primary key), as raw EntryRecords."""
        with self.lock:
            self.ensure_fresh()
            tags = defaultdict(list)
            for r in self.mem.execute("select pk, path from entry_tag order by path"):
                tags[r["pk"]].append(r["path"])
            atts = defaultdict(list)
            for a in self.db.execute("select * from ZATTACHMENT order by ZENTRY, ZORDER, Z_PK"):
                atts[a["ZENTRY"]].append(a)
            places = {r["Z_PK"]: r for r in self.db.execute("select * from ZPLACE")}
            weathers = {r["Z_PK"]: r for r in self.db.execute("select * from ZWEATHER")}
            rows = self.db.execute("select * from ZENTRY where coalesce(ZISTRASHED,0)=0 "
                                   "order by ZDATE, Z_PK").fetchall()
            journals = dict(self.journals)
        for r in rows:
            yield EntryRecord(
                id=r["ZIDENTIFIER"] or "",
                created=_local_dt(r["ZDATE"] or 0),
                modified=_local_dt(r["ZDATEMODIFIED"]),
                journal=journals.get(r["ZJOURNAL"], "Unfiled"),
                text=r["ZTEXT"] or "",
                tags=tags.get(r["Z_PK"], []),
                place=_place_record(places.get(r["ZPLACE"])),
                latitude=r["ZENCRYPTEDLATITUDE"] or r["ZLATITUDE"],
                longitude=r["ZENCRYPTEDLONGITUDE"] or r["ZLONGITUDE"],
                weather=_weather_record(weathers.get(r["ZWEATHER"])),
                bookmarked=bool(r["ZISBOOKMARKED"]),
                pinned=bool(r["ZPINNED"]),
                word_count=r["ZWORDCOUNT"] or 0,
                year_month_day_id=r["ZYEARMONTHDAYID"] or 0,
                attachments=[_attachment_record(a) for a in atts.get(r["Z_PK"], [])],
            )

    def attachment_bytes(self, identifier: str) -> bytes | None:
        """Raw attachment bytes (largest decoded copy if several rows share the identifier), or None."""
        with self.lock:
            return self._attachment_bytes(identifier)

    def _image_bytes(self, attachment_id: str) -> bytes:
        with self.lock:
            a = self._find_attachment(attachment_id)
            if a["Z_ENT"] != ENT_IMAGE:
                kind = ENT_NAMES.get(a["Z_ENT"], "non-image")
                raise EverlogError(f"Attachment {attachment_id!r} is a {kind} attachment, not an image.")
            data = self._attachment_bytes(a["ZIDENTIFIER"])
        if data is None:
            raise EverlogError(f"Image bytes for attachment {attachment_id!r} are missing from the Everlog store.")
        return data

    def attachment_original(self, attachment_id: str) -> bytes:
        """Original, unmodified bytes of an image attachment (id or unambiguous 8+ hex prefix)."""
        return self._image_bytes(attachment_id)

    def attachment_jpeg(self, attachment_id: str, max_size: int = DEFAULT_MAX_PX) -> bytes:
        return to_jpeg(self._image_bytes(attachment_id), max_size)


def _build_location(place):
    if place is None:
        return "", None
    parts = [place["ZENCRYPTEDNAME"] or place["ZNAME"],
             place["ZENCRYPTEDLOCALITY"] or place["ZLOCALITY"],
             place["ZENCRYPTEDADMINISTRATIVEAREA"] or place["ZADMINISTRATIVEAREA"],
             place["ZENCRYPTEDCOUNTRY"] or place["ZCOUNTRY"]]
    seen, out = set(), []
    for p in parts:
        if p and p not in seen:
            seen.add(p); out.append(p)
    coord = _nonzero_coord(place["ZENCRYPTEDLATITUDE"] or place["ZLATITUDE"],
                           place["ZENCRYPTEDLONGITUDE"] or place["ZLONGITUDE"])
    return ", ".join(out), coord


def _weather_fields(w):
    if w is None:
        return {}
    r = lambda v, n=1: None if v is None else round(v, n)
    f = {"condition": w["ZCONDITION"], "temperature_c": r(w["ZTEMPERATURE"]),
         "humidity_pct": round(w["ZHUMIDITY"] * 100) if w["ZHUMIDITY"] else None,
         "wind_speed": r(w["ZWINDSPEED"]),
         "pressure_hpa": None if w["ZPRESSURE"] is None else round(w["ZPRESSURE"] / 100, 1),
         "uv_index": w["ZUVINDEX"]}
    return {k: v for k, v in f.items() if v is not None}


def _attachment_info(a):
    kind = ENT_NAMES.get(a["Z_ENT"], "other")
    info = {"attachment_id": a["ZIDENTIFIER"], "type": kind}
    if kind in ("image", "video") and a["ZWIDTH"] and a["ZHEIGHT"]:
        info["width"], info["height"] = a["ZWIDTH"], a["ZHEIGHT"]
    if kind in ("audio", "video") and a["ZDURATION"]:
        info["duration"] = fmt_duration(a["ZDURATION"])
    if kind == "file":
        info["name"] = a["ZNAME"] or a["ZTITLE"]
        info["extension"] = a["ZFILEEXTENSION"]
    if a["ZTRANSCRIPTION"]:
        info["transcription"] = a["ZTRANSCRIPTION"]
    return info


def _local_dt(ts) -> datetime | None:
    d = from_coredata(ts)
    return d.astimezone(TZ) if d else None


def _place_record(p) -> PlaceRecord | None:
    if p is None:
        return None
    return PlaceRecord(
        name=p["ZENCRYPTEDNAME"] or p["ZNAME"], locality=p["ZENCRYPTEDLOCALITY"] or p["ZLOCALITY"],
        administrative_area=p["ZENCRYPTEDADMINISTRATIVEAREA"] or p["ZADMINISTRATIVEAREA"],
        country=p["ZENCRYPTEDCOUNTRY"] or p["ZCOUNTRY"], timezone=p["ZENCRYPTEDTIMEZONE"] or p["ZTIMEZONE"],
        latitude=p["ZENCRYPTEDLATITUDE"] or p["ZLATITUDE"], longitude=p["ZENCRYPTEDLONGITUDE"] or p["ZLONGITUDE"])


def _weather_record(w) -> WeatherRecord | None:
    if w is None:
        return None
    return WeatherRecord(condition=w["ZCONDITION"], temperature_c=w["ZTEMPERATURE"], humidity=w["ZHUMIDITY"],
                         pressure_pa=w["ZPRESSURE"], wind_speed=w["ZWINDSPEED"], wind_direction=_col(w, "ZWINDDIRECTION"), uv_index=w["ZUVINDEX"])


def _attachment_record(a) -> AttachmentRecord:
    return AttachmentRecord(
        identifier=a["ZIDENTIFIER"] or "", kind=ENT_NAMES.get(a["Z_ENT"], "other"),
        extension=a["ZFILEEXTENSION"], name=a["ZNAME"], title=a["ZTITLE"], duration=a["ZDURATION"],
        latitude=a["ZLATITUDE"], longitude=a["ZLONGITUDE"], created=_local_dt(_col(a, "ZDATECREATED")),
        order=a["ZORDER"])
