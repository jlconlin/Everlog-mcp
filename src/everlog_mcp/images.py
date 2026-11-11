"""Attachment image conversion using macOS ``sips`` (handles HEIC, PNG, JPEG, ...)."""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

SIPS = "/usr/bin/sips"
DEFAULT_MAX_PX = 1568


class ImageError(Exception):
    pass


def _dimensions(path: Path) -> tuple[int, int] | None:
    r = subprocess.run([SIPS, "-g", "pixelWidth", "-g", "pixelHeight", str(path)],
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        return None
    vals = {}
    for line in r.stdout.splitlines():
        k, _, v = line.strip().partition(":")
        if k in ("pixelWidth", "pixelHeight") and v.strip().isdigit():
            vals[k] = int(v)
    if len(vals) == 2:
        return vals["pixelWidth"], vals["pixelHeight"]
    return None


def to_jpeg(data: bytes, max_px: int = DEFAULT_MAX_PX, quality: int = 80) -> bytes:
    """Convert image bytes to JPEG, shrinking so the longest side is <= max_px."""
    if max_px < 16:
        raise ImageError("max_size must be at least 16 pixels")
    with tempfile.TemporaryDirectory(prefix="everlog-img-") as t:
        src, dst = Path(t) / "in.img", Path(t) / "out.jpg"
        src.write_bytes(data)
        dims = _dimensions(src)
        if dims is None:
            raise ImageError("attachment bytes could not be decoded as an image")
        cmd = [SIPS, "-s", "format", "jpeg", "-s", "formatOptions", str(quality)]
        if max(dims) > max_px:
            cmd += ["-Z", str(max_px)]
        cmd += [str(src), "--out", str(dst)]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired as e:
            raise ImageError("image conversion timed out") from e
        if r.returncode != 0 or not dst.exists():
            raise ImageError("image conversion failed")
        return dst.read_bytes()


def sniff_format(data: bytes) -> str:
    """Image format name for ``Image(format=...)`` (-> ``image/<name>``), from magic bytes."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[4:8] == b"ftyp" and data[8:12] in (b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"):
        return "heic"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    return "octet-stream"
