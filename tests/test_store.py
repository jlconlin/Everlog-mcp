import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import A_IMG, B_IMG, C_AUD, D_FIL, ENTRY_IDS, add_entry, make_png
from everlog_mcp.store import EverlogError, EverlogStore


def ids(res):
    return [r["entry_id"] for r in res["results"]]


def test_missing_dir_error_mentions_remedies(tmp_path):
    with pytest.raises(EverlogError) as e:
        EverlogStore(tmp_path / "nope")
    assert "EVERLOG_DIR" in str(e.value) and "Full Disk Access" in str(e.value)


def test_trashed_excluded(store):
    assert store.search()["total"] == 4
    assert store.search("zebra")["total"] == 0


def test_encrypted_columns_used(store):
    names = {j["name"]: j for j in store.list_journals()}
    assert set(names) == {"Daily Journal", "Travel", "Secret"}  # ZENCRYPTEDNAME and ZNAME both work
    assert names["Daily Journal"]["entry_count"] == 2
    assert names["Daily Journal"]["first_date"] == "2023-03-15"
    assert names["Daily Journal"]["last_date"] == "2024-03-15"
    assert names["Secret"]["hidden"] is True
    e = store.get_entry(ENTRY_IDS[1])
    assert e["location"] == "Riverside Park, Testville, TS, Testland"  # encrypted name preferred
    assert (e["latitude"], e["longitude"]) == (12.5, -45.25)  # 0/0 entry coords ignored -> place coords
    assert e["timezone"] == "America/Denver"


def test_tag_hierarchy(store):
    tags = {t["tag"]: t["entry_count"] for t in store.list_tags()}
    # 'Health' itself is only on the trashed entry, so it is not listed
    assert tags == {"Health/Running": 2, "Health/Running/Marathon": 1, "Work": 1}
    assert [t["tag"] for t in store.list_tags()][0] == "Health/Running"  # count desc
    assert set(ids(store.search(tag="health"))) == {ENTRY_IDS[1], ENTRY_IDS[5]}  # descendants
    assert set(ids(store.search(tag="Health/Running"))) == {ENTRY_IDS[1], ENTRY_IDS[5]}
    assert ids(store.search(tag="health/running/marathon")) == [ENTRY_IDS[1]]
    assert ids(store.search(tag="Work")) == [ENTRY_IDS[2]]
    with pytest.raises(EverlogError, match="Unknown tag"):
        store.search(tag="Wrok")


def test_journal_filter_and_unknown(store):
    assert ids(store.search(journal="travel")) == [ENTRY_IDS[3]]
    with pytest.raises(EverlogError, match="Close matches"):
        store.search(journal="Travl")


def test_date_range_and_sort(store):
    r = store.search(start_date="2022-01-01", end_date="2023-12-31", sort="oldest")
    assert ids(r) == [ENTRY_IDS[3], ENTRY_IDS[2]]
    assert ids(store.search())[0] == ENTRY_IDS[1]  # newest default
    with pytest.raises(EverlogError, match="YYYY-MM-DD"):
        store.search(start_date="03/15/2024")


def test_bookmarked_and_paging(store):
    assert ids(store.search(bookmarked=True)) == [ENTRY_IDS[1]]
    p = store.search(limit=2, offset=2)
    assert p["total"] == 4 and len(p["results"]) == 2


def test_fts_ranking_snippet_stemming(store):
    r = store.search("running")  # stems: run/running
    assert r["total"] == 2
    assert ids(r)[0] == ENTRY_IDS[5]  # more occurrences ranks first
    top = r["results"][0]
    assert "«" in top["snippet"] and "»" in top["snippet"]
    assert r["results"][1]["title"] == "Morning run"
    assert "attachment" not in r["results"][1]["snippet"].lower()  # refs stripped from index


def test_transcription_indexed(store):
    assert ids(store.search("gelato")) == [ENTRY_IDS[3]]


def test_fts_syntax_fallback(store):
    assert ids(store.search("quarterly budget")) == [ENTRY_IDS[2]]
    # unbalanced quote / hyphen / bad column filter are FTS5 syntax errors -> quoted-token fallback
    assert ids(store.search('"quarterly budget')) == [ENTRY_IDS[2]]
    assert ids(store.search("quarterly-budget")) == [ENTRY_IDS[2]]
    assert ids(store.search("foo:bar quarterly")) == []
    assert ids(store.search("budget NOT zzz")) == [ENTRY_IDS[2]]
    assert ids(store.search("budg*")) == [ENTRY_IDS[2]]


def test_browse_snippet_is_cleaned_preview(store):
    r = store.search(journal="Daily Journal", sort="newest")["results"][0]
    assert r["snippet"].startswith("Morning run I went running")
    assert "![" not in r["snippet"] and "#" not in r["snippet"]
    assert r["date"] == "2024-03-15" and r["created"].endswith("-06:00")


def test_on_this_day(store):
    r = store.on_this_day("2025-03-15")
    assert ids(r) == [ENTRY_IDS[1], ENTRY_IDS[2], ENTRY_IDS[3]]
    assert [x["years_ago"] for x in r["results"]] == [1, 2, 3]
    assert ids(store.on_this_day("2024-03-15")) == [ENTRY_IDS[2], ENTRY_IDS[3]]  # prior years only
    assert ids(store.on_this_day("2025-03-15", journal="Travel")) == [ENTRY_IDS[3]]
    assert store.on_this_day("2025-12-25")["total"] == 0
    assert "date" in store.on_this_day()  # default = today


def test_stats(store):
    s = store.stats()
    assert [(p["period"], p["entries"]) for p in s["periods"]] == [("2021", 1), ("2022", 1), ("2023", 1), ("2024", 1)]
    assert s["total_entries"] == 4 and s["total_words"] == sum(p["words"] for p in s["periods"])
    m = store.stats("month", journal="Daily Journal")
    assert [p["period"] for p in m["periods"]] == ["2023-03", "2024-03"]
    assert store.stats(start_date="2023-01-01")["total_entries"] == 2
    with pytest.raises(EverlogError):
        store.stats("week")


def test_get_entry_placeholders_and_metadata(store):
    e = store.get_entry(ENTRY_IDS[1].lower())
    assert "[image a1b2c3d4]" in e["text"] and "[file report.pdf]" in e["text"]
    assert "![attachment]" not in e["text"]
    assert e["summary"] == "Synthetic AI summary."
    assert e["weather"] == {"condition": "Clear", "temperature_c": 21.5, "humidity_pct": 50,
                            "wind_speed": 3.2, "pressure_hpa": 1013.2, "uv_index": 4}
    assert e["bookmarked"] and e["pinned"]
    assert e["tags"] == ["Health/Running", "Health/Running/Marathon"]
    assert e["modified"].startswith("2024-03-15T13:00:00")
    types = {a["attachment_id"]: a for a in e["attachments"]}
    assert types[A_IMG]["type"] == "image" and types[A_IMG]["width"] == 8
    e3 = store.get_entry(ENTRY_IDS[3])
    assert "[audio c3d4e5f6 2:31]" in e3["text"] and "[image b2c3d4e5]" in e3["text"]
    assert [a for a in e3["attachments"] if a["type"] == "audio"][0]["transcription"] == "whispered gelato"
    assert (e3["latitude"], e3["longitude"]) == (40.0, -105.0)
    assert e3["summary"] is None and e3["weather"] is None
    with pytest.raises(EverlogError, match="No entry"):
        store.get_entry("nope")
    with pytest.raises(EverlogError):
        store.get_entry(ENTRY_IDS[4])  # trashed


def png_dims(path):
    out = subprocess.run(["/usr/bin/sips", "-g", "pixelWidth", "-g", "pixelHeight", str(path)],
                         capture_output=True, text=True).stdout
    return tuple(int(l.split(":")[1]) for l in out.splitlines() if "pixel" in l)


def test_image_inline_and_external_and_resize(store, tmp_path):
    inline = store.attachment_jpeg(A_IMG)  # larger duplicate row selected, 0x01 inline
    assert inline[:3] == b"\xff\xd8\xff"
    (tmp_path / "a.jpg").write_bytes(inline)
    assert png_dims(tmp_path / "a.jpg") == (8, 8)  # not upscaled
    ext = store.attachment_jpeg(B_IMG.upper())  # 0x02 external, case-insensitive
    (tmp_path / "b.jpg").write_bytes(ext)
    assert png_dims(tmp_path / "b.jpg") == (64, 48)
    small = store.attachment_jpeg(B_IMG[:8], max_size=32)  # prefix + resize
    (tmp_path / "c.jpg").write_bytes(small)
    assert max(png_dims(tmp_path / "c.jpg")) == 32


def test_attachment_original_is_unmodified(store):
    assert store.attachment_original(A_IMG) == make_png(8, 8)
    assert store.attachment_original(B_IMG[:8].upper()) == make_png(64, 48)


def test_sniff_format():
    from everlog_mcp.images import sniff_format
    assert sniff_format(make_png(2, 2)) == "png"
    assert sniff_format(b"\xff\xd8\xff\xe0xxxx") == "jpeg"
    assert sniff_format(b"\x00\x00\x00\x18ftypheic....") == "heic"
    assert sniff_format(b"???") == "octet-stream"


def test_image_errors(store, everlog_dir):
    with pytest.raises(EverlogError, match="not an image"):
        store.attachment_original(C_AUD)
        store.attachment_jpeg(C_AUD)
    with pytest.raises(EverlogError, match="not an image"):
        store.attachment_jpeg(D_FIL)
    with pytest.raises(EverlogError, match="No attachment"):
        store.attachment_jpeg("f" * 32)
    with pytest.raises(EverlogError, match="Invalid"):
        store.attachment_jpeg("xyz")
    (everlog_dir / ".AttachmentData_SUPPORT/_EXTERNAL_DATA/11111111-2222-3333-4444-555555555555").unlink()
    with pytest.raises(EverlogError, match="missing"):
        store.attachment_jpeg(B_IMG)


def test_data_version_rebuild(store, everlog_dir):
    assert store.search("pineapple")["total"] == 0
    rw = sqlite3.connect(everlog_dir / "Hummingbird.sqlite")
    add_entry(rw, 99, "A new entry about pineapple", 2025, 1, 2, journal=1)
    rw.commit()
    rw.close()
    r = store.search("pineapple")
    assert r["total"] == 1 and r["results"][0]["entry_id"].startswith("E0000099")
    assert store.stats()["total_entries"] == 5
    # unchanged DB -> no rebuild
    mem = store.mem
    store.search("pineapple")
    assert store.mem is mem


def test_store_is_read_only(store):
    with pytest.raises(sqlite3.OperationalError):
        store.db.execute("delete from ZENTRY")


# ------------------------------------------------------------------ public export API
def test_import_without_mcp_or_server():
    code = ("import sys; sys.modules['mcp'] = None; import everlog_mcp.store as s; "
            "assert 'everlog_mcp.server' not in sys.modules; s.EverlogStore")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       env={**os.environ, "PYTHONPATH": str(Path(__file__).parent.parent / "src")})
    assert r.returncode == 0, r.stderr


def test_iter_entries_order_and_trash(store):
    recs = list(store.iter_entries())
    assert [r.id for r in recs] == [ENTRY_IDS[5], ENTRY_IDS[3], ENTRY_IDS[2], ENTRY_IDS[1]]  # chronological, no trashed


def test_iter_entries_entry1_fields(store):
    e = next(r for r in store.iter_entries() if r.id == ENTRY_IDS[1])
    assert e.created.isoformat() == "2024-03-15T12:00:00-06:00"
    assert e.modified.isoformat() == "2024-03-15T13:00:00-06:00"
    assert e.journal == "Daily Journal"
    assert f"![attachment]({A_IMG.upper()})" in e.text and f"![attachment]({D_FIL})" in e.text
    assert e.tags == ["Health/Running", "Health/Running/Marathon"]
    assert (e.bookmarked, e.pinned, e.word_count, e.year_month_day_id) == (True, True, e.word_count, 20240315)
    p = e.place
    assert (p.name, p.locality, p.administrative_area, p.country, p.timezone) == \
        ("Riverside Park", "Testville", "TS", "Testland", "America/Denver")
    assert (p.latitude, p.longitude) == (12.5, -45.25)
    w = e.weather
    assert (w.condition, w.temperature_c, w.humidity, w.pressure_pa, w.wind_speed, w.uv_index) == \
        ("Clear", 21.46, 0.5, 101325, 3.2, 4)
    assert [a.identifier for a in e.attachments] == [A_IMG, D_FIL]
    img, fil = e.attachments
    assert (img.kind, img.extension, img.order) == ("image", "png", 0)
    assert img.created.isoformat() == "2024-03-15T12:05:00-06:00"
    assert (fil.kind, fil.extension, fil.name, fil.created) == ("file", "pdf", "report.pdf", None)


def test_iter_entries_entry3_coords_and_audio(store):
    e = next(r for r in store.iter_entries() if r.id == ENTRY_IDS[3])
    assert (e.latitude, e.longitude, e.place, e.weather) == (40.0, -105.0, None, None)
    aud = next(a for a in e.attachments if a.identifier == C_AUD)
    assert (aud.kind, aud.duration) == ("audio", 151.0)


def test_journal_infos(store):
    assert {(j.name, j.hidden) for j in store.journal_infos()} == \
        {("Daily Journal", False), ("Travel", False), ("Secret", True)}


def test_attachment_bytes_public(store):
    inline = store.attachment_bytes(A_IMG)
    assert inline == make_png(8, 8)                      # larger duplicate row wins, raw bytes
    assert store.attachment_bytes(B_IMG) == make_png(64, 48)  # external file blob
    assert store.attachment_bytes(D_FIL) is None         # no data row
    assert store.attachment_bytes("f" * 32) is None
