"""The screenshot verifier's PNG reader is bounded (Task 33 precondition, N3 decode)."""

from __future__ import annotations

import random
import struct
import zlib

import pytest

from selector_drift.browser import png


def _chunk(tag: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(tag + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)


def _png(width: int, height: int, colour: int, idat: bytes) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, colour, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")
    )


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def _filter(kind: int, line: bytes, prev: bytes, bpp: int) -> bytes:
    out = bytearray(len(line))
    for i, x in enumerate(line):
        a = line[i - bpp] if i >= bpp else 0
        b = prev[i]
        c = prev[i - bpp] if i >= bpp else 0
        pred = {0: 0, 1: a, 2: b, 3: (a + b) >> 1, 4: _paeth(a, b, c)}[kind]
        out[i] = (x - pred) & 0xFF
    return bytes(out)


@pytest.mark.unit
@pytest.mark.parametrize("colour,bpp", [(0, 1), (2, 3), (6, 4)])
def test_every_filter_type_decodes_to_the_original_pixels(colour: int, bpp: int) -> None:
    rnd = random.Random(colour)
    width, height = 37, 25
    rows = [bytes(rnd.randrange(256) for _ in range(width * bpp)) for _ in range(height)]
    raw = bytearray()
    prev = bytes(width * bpp)
    for y, line in enumerate(rows):
        kind = y % 5
        raw += bytes([kind]) + _filter(kind, line, prev, bpp)
        prev = line
    image = png.decode(_png(width, height, colour, zlib.compress(bytes(raw))))
    assert image.rows == rows


@pytest.mark.unit
def test_a_page_past_the_height_ceiling_is_refused_before_inflating() -> None:
    # The IDAT is garbage: a decoder that inflated before checking the size would raise a
    # zlib error instead of TooLarge.
    tall = _png(10, png.MAX_HEIGHT_PX + 1, 0, b"not zlib at all")
    with pytest.raises(png.TooLarge):
        png.decode(tall, rows_needed=1)
    with pytest.raises(png.TooLarge):
        png.header(tall)


@pytest.mark.unit
def test_decoding_stops_at_the_rows_needed() -> None:
    """Only the rows asked for are inflated: the stream after them is never read (here it is
    corrupt, so an unbounded zlib.decompress fails)."""
    width, height = 200, 20_000
    head = b"".join(b"\x00" + bytes([y % 256]) * width for y in range(1_000))
    comp = zlib.compressobj()
    stream = comp.compress(head) + comp.flush(zlib.Z_SYNC_FLUSH) + b"\xff" * 64
    image = png.decode(_png(width, height, 0, stream), rows_needed=10)
    assert len(image.rows) == 10 and image.rows[9] == bytes([9]) * width
    with pytest.raises(ValueError):
        png.decode(_png(width, height, 0, stream))


@pytest.mark.unit
def test_zero_rows_needed_inflates_nothing() -> None:
    image = png.decode(_png(4, 4, 0, b"not zlib"), rows_needed=0)
    assert (image.width, image.height, image.rows) == (4, 4, [])
