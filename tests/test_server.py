import asyncio
import base64

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from conftest import A_IMG, B_IMG, ENTRY_IDS, make_png
from everlog_mcp import server as srv


@pytest.fixture(autouse=True)
def injected(store):
    srv.set_store(store)
    yield
    srv.set_store(None)


def call(name, args=None):
    return asyncio.run(srv.server.call_tool(name, args or {}))


def test_seven_read_only_tools():
    tools = asyncio.run(srv.server.list_tools())
    assert {t.name for t in tools} == {"list_journals", "list_tags", "search_entries", "get_entry",
                                       "get_attachment_image", "on_this_day", "journal_stats"}
    for t in tools:
        assert t.annotations.read_only_hint is True and t.annotations.open_world_hint is False
        assert t.description


def test_search_end_to_end():
    r = call("search_entries", {"query": "running", "limit": 5})
    sc = r.structured_content
    assert sc["total"] == 2 and sc["results"][0]["entry_id"] == ENTRY_IDS[5]


def test_get_entry_with_images_returns_json_then_images():
    r = call("get_entry", {"entry_id": ENTRY_IDS[3], "include_images": True, "max_images": 4})
    kinds = [c.type for c in r.content]
    assert kinds[0] == "text" and "whispered gelato" in r.content[0].text
    assert kinds[1:] == ["image"]
    assert r.content[1].mime_type == "image/jpeg"


def test_get_attachment_image_tool():
    r = call("get_attachment_image", {"attachment_id": A_IMG})  # default: jpeg
    assert r.content[0].type == "image" and r.content[0].mime_type == "image/jpeg"
    assert base64.b64decode(r.content[0].data)[:3] == b"\xff\xd8\xff"
    r = call("get_attachment_image", {"attachment_id": B_IMG, "format": "jpeg", "max_size": 32})
    assert r.content[0].mime_type == "image/jpeg"


def test_get_attachment_image_original():
    r = call("get_attachment_image", {"attachment_id": A_IMG, "format": "original"})
    assert r.content[0].mime_type == "image/png"
    assert base64.b64decode(r.content[0].data) == make_png(8, 8)  # untouched bytes
    r = call("get_attachment_image", {"attachment_id": B_IMG, "format": "original", "max_size": 16})
    assert base64.b64decode(r.content[0].data) == make_png(64, 48)  # max_size ignored


def test_get_attachment_image_bad_format():
    with pytest.raises(ToolError):
        call("get_attachment_image", {"attachment_id": A_IMG, "format": "webp"})


def test_tool_errors_reach_model():
    with pytest.raises(ToolError, match="No entry"):
        call("get_entry", {"entry_id": "bogus"})
    with pytest.raises(ToolError, match="Unknown journal"):
        call("search_entries", {"journal": "Nope"})
    with pytest.raises(ToolError, match="not an image"):
        call("get_attachment_image", {"attachment_id": "c3d4e5f6"})


def test_stats_and_otd_tools():
    assert call("journal_stats", {"group_by": "month"}).structured_content["total_entries"] == 4
    r = call("on_this_day", {"date": "2025-03-15"}).structured_content
    assert r["total"] == 3


def test_missing_db_gives_helpful_tool_error(tmp_path, monkeypatch):
    srv.set_store(None)
    monkeypatch.setenv("EVERLOG_DIR", str(tmp_path / "missing"))
    with pytest.raises(ToolError, match="EVERLOG_DIR"):
        call("list_journals")
