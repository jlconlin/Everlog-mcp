"""Synthetic Everlog data directory (Core Data naming) for tests. No real journal content."""
import sqlite3
import struct
import zlib
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from everlog_mcp.store import COREDATA_EPOCH, EverlogStore

DENVER = ZoneInfo("America/Denver")
A_IMG = "a1b2c3d4" + "0" * 24      # inline image, entry 1
B_IMG = "b2c3d4e5" + "1" * 24      # external-file image, entry 3
C_AUD = "c3d4e5f6" + "2" * 24      # audio with transcription, entry 3
D_FIL = "d4e5f6a7" + "3" * 24      # file, entry 1
EXT_UUID = "11111111-2222-3333-4444-555555555555"

ENTRY_IDS = {
    1: "E0000001-0000-0000-0000-000000000001",
    2: "E0000002-0000-0000-0000-000000000002",
    3: "E0000003-0000-0000-0000-000000000003",
    4: "E0000004-0000-0000-0000-000000000004",
    5: "E0000005-0000-0000-0000-000000000005",
}


def make_png(w: int, h: int) -> bytes:
    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    raw = b"".join(
        b"\x00" + b"".join(bytes((x * 255 // max(w - 1, 1), y * 255 // max(h - 1, 1), 128)) for x in range(w))
        for y in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def cd(y, m, d, hh=12, mm=0) -> float:
    return datetime(y, m, d, hh, mm, tzinfo=DENVER).timestamp() - COREDATA_EPOCH


MAIN_SCHEMA = """
create table ZJOURNAL(Z_PK integer primary key, ZNAME text, ZENCRYPTEDNAME text, ZISHIDDEN integer);
create table ZTAG(Z_PK integer primary key, ZTITLE text, ZENCRYPTEDTITLE text, ZPARENT integer);
create table Z_17TAGS(Z_17ENTRIES1 integer, Z_25TAGS integer);
create table ZPLACE(Z_PK integer primary key, ZNAME text, ZENCRYPTEDNAME text, ZLOCALITY text, ZENCRYPTEDLOCALITY text,
  ZADMINISTRATIVEAREA text, ZENCRYPTEDADMINISTRATIVEAREA text, ZCOUNTRY text, ZENCRYPTEDCOUNTRY text,
  ZLATITUDE real, ZLONGITUDE real, ZENCRYPTEDLATITUDE real, ZENCRYPTEDLONGITUDE real,
  ZTIMEZONE text, ZENCRYPTEDTIMEZONE text);
create table ZWEATHER(Z_PK integer primary key, ZCONDITION text, ZTEMPERATURE real, ZHUMIDITY real,
  ZPRESSURE real, ZWINDSPEED real, ZUVINDEX integer, ZWINDDIRECTION real);
create table ZENTRYVERDICT(Z_PK integer primary key, ZENTRYIDENTIFIER text, ZSUMMARY text);
create table ZATTACHMENT(Z_PK integer primary key, Z_ENT integer, ZENTRY integer, ZIDENTIFIER text, ZORDER integer,
  ZDURATION real, ZTRANSCRIPTION text, ZWIDTH integer, ZHEIGHT integer, ZFILEEXTENSION text, ZNAME text,
  ZTITLE text, ZLATITUDE real, ZLONGITUDE real, ZDATECREATED real);
create table ZENTRY(Z_PK integer primary key, ZIDENTIFIER text, ZTEXT text, ZCONTENT text, ZDATE real,
  ZDATEMODIFIED real, ZJOURNAL integer, ZPLACE integer, ZWEATHER integer, ZISTRASHED integer,
  ZISBOOKMARKED integer, ZPINNED integer, ZWORDCOUNT integer, ZYEARMONTHDAYID integer,
  ZLATITUDE real, ZLONGITUDE real, ZENCRYPTEDLATITUDE real, ZENCRYPTEDLONGITUDE real);
"""


def add_entry(db, pk, text, y, m, d, journal=1, trashed=0, bookmarked=0, **kw):
    db.execute(
        "insert into ZENTRY(Z_PK,ZIDENTIFIER,ZTEXT,ZCONTENT,ZDATE,ZDATEMODIFIED,ZJOURNAL,ZPLACE,ZWEATHER,ZISTRASHED,"
        "ZISBOOKMARKED,ZPINNED,ZWORDCOUNT,ZYEARMONTHDAYID,ZLATITUDE,ZLONGITUDE,ZENCRYPTEDLATITUDE,ZENCRYPTEDLONGITUDE)"
        " values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (pk, ENTRY_IDS.get(pk, f"E{pk:07d}-0000-0000-0000-{pk:012d}"), text, "placeholder", cd(y, m, d),
         cd(y, m, d, 13), journal, kw.get("place"), kw.get("weather"), trashed, bookmarked, kw.get("pinned", 0),
         len(text.split()), y * 10000 + m * 100 + d, 0, 0, kw.get("lat"), kw.get("lon")))


@pytest.fixture
def everlog_dir(tmp_path):
    d = tmp_path / "group.hummingbird"
    ext = d / ".AttachmentData_SUPPORT" / "_EXTERNAL_DATA"
    ext.mkdir(parents=True)
    png_small, png_big = make_png(8, 8), make_png(64, 48)
    (ext / EXT_UUID).write_bytes(png_big)

    db = sqlite3.connect(d / "Hummingbird.sqlite")
    db.execute("pragma journal_mode=wal")
    db.executescript(MAIN_SCHEMA)
    # journals: name only in ENCRYPTED column (plaintext!), only in ZNAME, hidden
    db.executemany("insert into ZJOURNAL values(?,?,?,?)",
                   [(1, None, "Daily Journal", 0), (2, "Travel", None, 0), (3, None, "Secret", 1)])
    db.executemany("insert into ZTAG values(?,?,?,?)",
                   [(1, None, "Health", None), (2, "Running", None, 1), (3, "Work", None, None),
                    (4, None, "Marathon", 2)])
    db.execute("insert into ZPLACE(Z_PK,ZNAME,ZENCRYPTEDNAME,ZLOCALITY,ZENCRYPTEDLOCALITY,ZADMINISTRATIVEAREA,"
               "ZCOUNTRY,ZENCRYPTEDCOUNTRY,ZLATITUDE,ZLONGITUDE,ZENCRYPTEDLATITUDE,ZENCRYPTEDLONGITUDE,"
               "ZTIMEZONE,ZENCRYPTEDTIMEZONE) values(1,'old name','Riverside Park','Testville',NULL,'TS',"
               "'Testland',NULL,0,0,12.5,-45.25,NULL,'America/Denver')")
    db.execute("insert into ZWEATHER values(1,'Clear',21.46,0.5,101325,3.2,4,270.0)")
    db.execute("insert into ZENTRYVERDICT values(1,?,?)", (ENTRY_IDS[1], "Synthetic AI summary."))

    add_entry(db, 1, "# Morning run\n\nI went running along the river.\n\n"
                     f"![attachment]({A_IMG.upper()})\n\nSee ![attachment]({D_FIL}) too.",
              2024, 3, 15, journal=1, bookmarked=1, place=1, weather=1, pinned=1)
    add_entry(db, 2, "Planning the quarterly budget\n\nSpreadsheets and meetings.", 2023, 3, 15, journal=1)
    add_entry(db, 3, f"Hiked in the mountains.\n![attachment]({B_IMG})\n![attachment]({C_AUD})",
              2022, 3, 15, journal=2, lat=40.0, lon=-105.0)
    add_entry(db, 4, "trashed zebra content", 2024, 5, 1, journal=1, trashed=1)
    add_entry(db, 5, "running running running and more running", 2021, 7, 4, journal=3)
    db.executemany("insert into Z_17TAGS values(?,?)", [(1, 4), (2, 3), (5, 2), (4, 1), (1, 2)])
    # tag 4 'Marathon' under Running under Health -> entry1 gets Health/Running/Marathon and Health/Running
    db.executemany("insert into ZATTACHMENT(Z_PK,Z_ENT,ZENTRY,ZIDENTIFIER,ZORDER,ZDURATION,ZTRANSCRIPTION,ZWIDTH,"
                   "ZHEIGHT,ZFILEEXTENSION,ZNAME,ZDATECREATED) values(?,?,?,?,?,?,?,?,?,?,?,?)",
                   [(1, 8, 1, A_IMG, 0, None, None, 8, 8, "png", None, cd(2024, 3, 15, 12, 5)),
                    (2, 6, 1, D_FIL, 1, None, None, None, None, "pdf", "report.pdf", None),
                    (3, 8, 3, B_IMG, 0, None, None, 64, 48, "png", None, None),
                    (4, 3, 3, C_AUD, 1, 151.0, "whispered gelato", None, None, None, None, None)])
    db.commit()
    db.close()

    att = sqlite3.connect(d / "AttachmentData.sqlite")
    att.execute("create table ZATTACHMENTDATA(Z_PK integer primary key, ZIDENTIFIER text, ZDATA blob)")
    att.executemany("insert into ZATTACHMENTDATA(ZIDENTIFIER, ZDATA) values(?,?)", [
        (A_IMG, b"\x01xx"),                       # smaller duplicate row; the larger one must win
        (A_IMG, b"\x01" + png_small),             # inline
        (B_IMG, b"\x02" + EXT_UUID.encode() + b"\x00\x00"),  # external file
    ])
    att.commit()
    att.close()
    return d


@pytest.fixture
def store(everlog_dir):
    return EverlogStore(everlog_dir)
