"""A minimal, stdlib-only PNG reader for verifying a masked screenshot (Task 19 fix round 3).

Reads what Chromium writes: 8-bit greyscale, RGB or RGBA, non-interlaced. Anything else is a
ValueError -- the caller refuses rather than verify an image it cannot read. Decoding can stop
at a row (`rows_needed`), since verification only needs the rows the mask boxes cover.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_CHANNELS = {0: 1, 2: 3, 6: 4}  # greyscale, RGB, RGBA


@dataclass(frozen=True)
class Image:
    width: int
    height: int
    channels: int
    rows: list[bytes]  # unfiltered scanlines, possibly fewer than `height`

    def is_black(self, x0: int, y0: int, x1: int, y1: int) -> bool:
        """Every pixel in [x0, x1) x [y0, y1) is #000000 (alpha, if any, is not colour)."""
        c = self.channels
        colour = min(c, 3)
        for y in range(y0, y1):
            segment = self.rows[y][x0 * c : x1 * c]
            for k in range(colour):
                channel = segment[k::c]
                if channel.count(0) != len(channel):
                    return False
        return True


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def decode(png: bytes, rows_needed: int | None = None) -> Image:
    if not png.startswith(_SIGNATURE):
        raise ValueError("not a PNG")
    pos, idat, header = len(_SIGNATURE), bytearray(), b""
    try:
        while pos < len(png):
            (length,) = struct.unpack(">I", png[pos : pos + 4])
            tag, data = png[pos + 4 : pos + 8], png[pos + 8 : pos + 8 + length]
            if tag == b"IHDR":
                header = data
            elif tag == b"IDAT":
                idat += data
            elif tag == b"IEND":
                break
            pos += 12 + length
        width, height, depth, colour, _, _, interlace = struct.unpack(">IIBBBBB", header)
    except struct.error as exc:
        raise ValueError("truncated PNG") from exc
    if depth != 8 or colour not in _CHANNELS or interlace != 0:
        raise ValueError(f"unsupported PNG (depth {depth}, colour {colour}, interlace {interlace})")
    c = _CHANNELS[colour]
    stride = width * c
    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error as exc:
        raise ValueError("corrupt PNG data") from exc
    limit = height if rows_needed is None else max(0, min(height, rows_needed))
    if len(raw) < limit * (stride + 1):
        raise ValueError("truncated PNG data")
    rows: list[bytes] = []
    prev = bytes(stride)
    for y in range(limit):
        start = y * (stride + 1)
        kind = raw[start]
        line = bytearray(raw[start + 1 : start + 1 + stride])
        if kind == 0:
            pass
        elif kind == 2:
            line = bytearray((x + b) & 0xFF for x, b in zip(line, prev, strict=True))
        elif kind == 1:
            for i in range(c, stride):
                line[i] = (line[i] + line[i - c]) & 0xFF
        elif kind == 3:
            for i in range(stride):
                left = line[i - c] if i >= c else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif kind == 4:
            for i in range(stride):
                left = line[i - c] if i >= c else 0
                up_left = prev[i - c] if i >= c else 0
                line[i] = (line[i] + _paeth(left, prev[i], up_left)) & 0xFF
        else:
            raise ValueError(f"unknown PNG filter {kind}")
        prev = bytes(line)
        rows.append(prev)
    return Image(width=width, height=height, channels=c, rows=rows)
