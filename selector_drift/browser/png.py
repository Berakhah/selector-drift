"""A minimal PNG reader for verifying a masked screenshot (Task 19 fix round 3; Task 33).

Reads what Chromium writes: 8-bit greyscale, RGB or RGBA, non-interlaced. Anything else is a
ValueError -- the caller refuses rather than verify an image it cannot read.

Bounded (Task 33 precondition): an image taller than MAX_HEIGHT_PX or wider than MAX_WIDTH_PX
is refused before anything is inflated (TooLarge), and decoding stops at `rows_needed` -- the
compressed stream is inflated with a decompressobj capped at exactly the bytes those rows
occupy, so a 20 000 px page whose lowest mask box is at y=900 inflates ~900 rows, not 77 MB.
The None/Sub/Up filters are vectorised with numpy; Average and Paeth stay in Python (they are
sequential along the row, and Chromium rarely emits them).
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

import numpy as np

_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_CHANNELS = {0: 1, 2: 3, 6: 4}  # greyscale, RGB, RGBA
# A page taller than this is refused, not verified (decode time is linear in rows).
MAX_HEIGHT_PX = 32_768
MAX_WIDTH_PX = 16_384


class TooLarge(ValueError):
    """The image's declared size is past the decoder's ceiling."""


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


@dataclass(frozen=True)
class Header:
    width: int
    height: int
    channels: int


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def _chunks(png: bytes) -> tuple[bytes, bytes]:
    """(IHDR data, concatenated IDAT data)."""
    if not png.startswith(_SIGNATURE):
        raise ValueError("not a PNG")
    pos, idat, header = len(_SIGNATURE), bytearray(), b""
    try:
        while pos < len(png):
            (length,) = struct.unpack(">I", png[pos : pos + 4])
            if pos + 12 + length > len(png):
                raise ValueError("truncated PNG chunk")
            tag, data = png[pos + 4 : pos + 8], png[pos + 8 : pos + 8 + length]
            if tag == b"IHDR":
                header = data
            elif tag == b"IDAT":
                idat += data
            elif tag == b"IEND":
                break
            pos += 12 + length
    except struct.error as exc:
        raise ValueError("truncated PNG") from exc
    return header, bytes(idat)


def header(png: bytes) -> Header:
    """The image's size and channels, without inflating anything."""
    data, _ = _chunks(png)
    try:
        width, height, depth, colour, _, _, interlace = struct.unpack(">IIBBBBB", data)
    except struct.error as exc:
        raise ValueError("truncated PNG") from exc
    if depth != 8 or colour not in _CHANNELS or interlace != 0:
        raise ValueError(f"unsupported PNG (depth {depth}, colour {colour}, interlace {interlace})")
    if height > MAX_HEIGHT_PX or width > MAX_WIDTH_PX:
        raise TooLarge(
            f"screenshot is {width}x{height} px, past the {MAX_WIDTH_PX}x{MAX_HEIGHT_PX} ceiling"
        )
    return Header(width=width, height=height, channels=_CHANNELS[colour])


def decode(png: bytes, rows_needed: int | None = None) -> Image:
    head = header(png)
    _, idat = _chunks(png)
    c = head.channels
    stride = head.width * c
    limit = head.height if rows_needed is None else max(0, min(head.height, rows_needed))
    if limit == 0:
        return Image(width=head.width, height=head.height, channels=c, rows=[])
    need = limit * (stride + 1)
    try:
        # Bounded: never inflates past the rows asked for (max_length=0 would mean unlimited,
        # which `limit == 0` above excludes).
        raw = zlib.decompressobj().decompress(idat, need)
    except zlib.error as exc:
        raise ValueError("corrupt PNG data") from exc
    if len(raw) < need:
        raise ValueError("truncated PNG data")
    rows: list[bytes] = []
    prev = np.zeros(stride, dtype=np.uint8)
    for y in range(limit):
        start = y * (stride + 1)
        kind = raw[start]
        line = np.frombuffer(raw, dtype=np.uint8, count=stride, offset=start + 1)
        if kind == 0:
            out = line.copy()
        elif kind == 1:
            out = np.cumsum(line.reshape(-1, c), axis=0, dtype=np.uint8).reshape(-1)
        elif kind == 2:
            out = line + prev  # uint8 arithmetic wraps modulo 256
        elif kind in (3, 4):
            buf = bytearray(line.tobytes())
            up = prev.tobytes()
            for i in range(stride):
                left = buf[i - c] if i >= c else 0
                if kind == 3:
                    buf[i] = (buf[i] + ((left + up[i]) >> 1)) & 0xFF
                else:
                    up_left = up[i - c] if i >= c else 0
                    buf[i] = (buf[i] + _paeth(left, up[i], up_left)) & 0xFF
            out = np.frombuffer(bytes(buf), dtype=np.uint8)
        else:
            raise ValueError(f"unknown PNG filter {kind}")
        prev = out
        rows.append(out.tobytes())
    return Image(width=head.width, height=head.height, channels=c, rows=rows)
