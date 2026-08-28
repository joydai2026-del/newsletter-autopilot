"""Cover rendering, and the gate that proves the render is not blank.

WHY THE GATE EXISTS

Exit code 0, "the file exists", and "the dimensions are correct" are not proof
that a rendered image looks right. A completely blank render passes all three.
The pipeline this was distilled from shipped five blank PDF pages and, on
another day, a cover that was a solid empty circle at exactly the right size,
both with a green exit code.

So every render is checked on its OUTPUT: ink coverage in the interior, more
than one distinct colour, and the encoded file's own IHDR dimensions read back
from the bytes that were written. A render that fails the gate raises before the
image can reach a draft.

The renderer is standard library only (zlib plus a 5x7 bitmap font). A real
deployment swaps in whatever design tool it likes; the gate is the part worth
keeping.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

from .errors import AutopilotError


class RenderGateFailed(AutopilotError):
    """The rendered image is blank, broken, or not what was asked for."""


# A 5x7 bitmap font. Each glyph is 7 rows of 5 bits, high bit leftmost.
_FONT: dict[str, tuple[int, ...]] = {
    " ": (0, 0, 0, 0, 0, 0, 0),
    "-": (0, 0, 0, 0x1F, 0, 0, 0),
    ".": (0, 0, 0, 0, 0, 0x0C, 0x0C),
    ":": (0, 0x0C, 0x0C, 0, 0x0C, 0x0C, 0),
    "0": (0x0E, 0x11, 0x13, 0x15, 0x19, 0x11, 0x0E),
    "1": (0x04, 0x0C, 0x04, 0x04, 0x04, 0x04, 0x0E),
    "2": (0x0E, 0x11, 0x01, 0x02, 0x04, 0x08, 0x1F),
    "3": (0x1F, 0x02, 0x04, 0x02, 0x01, 0x11, 0x0E),
    "4": (0x02, 0x06, 0x0A, 0x12, 0x1F, 0x02, 0x02),
    "5": (0x1F, 0x10, 0x1E, 0x01, 0x01, 0x11, 0x0E),
    "6": (0x06, 0x08, 0x10, 0x1E, 0x11, 0x11, 0x0E),
    "7": (0x1F, 0x01, 0x02, 0x04, 0x08, 0x08, 0x08),
    "8": (0x0E, 0x11, 0x11, 0x0E, 0x11, 0x11, 0x0E),
    "9": (0x0E, 0x11, 0x11, 0x0F, 0x01, 0x02, 0x0C),
    "A": (0x0E, 0x11, 0x11, 0x1F, 0x11, 0x11, 0x11),
    "B": (0x1E, 0x11, 0x11, 0x1E, 0x11, 0x11, 0x1E),
    "C": (0x0E, 0x11, 0x10, 0x10, 0x10, 0x11, 0x0E),
    "D": (0x1C, 0x12, 0x11, 0x11, 0x11, 0x12, 0x1C),
    "E": (0x1F, 0x10, 0x10, 0x1E, 0x10, 0x10, 0x1F),
    "F": (0x1F, 0x10, 0x10, 0x1E, 0x10, 0x10, 0x10),
    "G": (0x0E, 0x11, 0x10, 0x17, 0x11, 0x11, 0x0F),
    "H": (0x11, 0x11, 0x11, 0x1F, 0x11, 0x11, 0x11),
    "I": (0x0E, 0x04, 0x04, 0x04, 0x04, 0x04, 0x0E),
    "J": (0x07, 0x02, 0x02, 0x02, 0x02, 0x12, 0x0C),
    "K": (0x11, 0x12, 0x14, 0x18, 0x14, 0x12, 0x11),
    "L": (0x10, 0x10, 0x10, 0x10, 0x10, 0x10, 0x1F),
    "M": (0x11, 0x1B, 0x15, 0x15, 0x11, 0x11, 0x11),
    "N": (0x11, 0x19, 0x15, 0x13, 0x11, 0x11, 0x11),
    "O": (0x0E, 0x11, 0x11, 0x11, 0x11, 0x11, 0x0E),
    "P": (0x1E, 0x11, 0x11, 0x1E, 0x10, 0x10, 0x10),
    "Q": (0x0E, 0x11, 0x11, 0x11, 0x15, 0x12, 0x0D),
    "R": (0x1E, 0x11, 0x11, 0x1E, 0x14, 0x12, 0x11),
    "S": (0x0F, 0x10, 0x10, 0x0E, 0x01, 0x01, 0x1E),
    "T": (0x1F, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04),
    "U": (0x11, 0x11, 0x11, 0x11, 0x11, 0x11, 0x0E),
    "V": (0x11, 0x11, 0x11, 0x11, 0x11, 0x0A, 0x04),
    "W": (0x11, 0x11, 0x11, 0x15, 0x15, 0x15, 0x0A),
    "X": (0x11, 0x11, 0x0A, 0x04, 0x0A, 0x11, 0x11),
    "Y": (0x11, 0x11, 0x0A, 0x04, 0x04, 0x04, 0x04),
    "Z": (0x1F, 0x01, 0x02, 0x04, 0x08, 0x10, 0x1F),
}

Colour = tuple[int, int, int]


class Raster:
    """A tiny RGB pixel buffer. Rows of (r, g, b) bytes."""

    def __init__(self, width: int, height: int, background: Colour):
        if width < 8 or height < 8:
            raise RenderGateFailed(f"a {width}x{height} cover is not a cover")
        self.width, self.height = width, height
        self.px = bytearray(bytes(background) * width * height)

    def _set(self, x: int, y: int, colour: Colour) -> None:
        if 0 <= x < self.width and 0 <= y < self.height:
            i = (y * self.width + x) * 3
            self.px[i:i + 3] = bytes(colour)

    def rect(self, x: int, y: int, w: int, h: int, colour: Colour) -> None:
        for yy in range(y, y + h):
            for xx in range(x, x + w):
                self._set(xx, yy, colour)

    def text(self, x: int, y: int, message: str, colour: Colour, scale: int = 1) -> int:
        """Draw uppercase text. Returns the x position after the last glyph."""
        cursor = x
        for ch in message.upper():
            glyph = _FONT.get(ch)
            if glyph is None:
                cursor += 6 * scale
                continue
            for row, bits in enumerate(glyph):
                for col in range(5):
                    if bits & (1 << (4 - col)):
                        self.rect(cursor + col * scale, y + row * scale,
                                  scale, scale, colour)
            cursor += 6 * scale
        return cursor

    # --- output-side measurements the gate uses --------------------------

    def distinct_colours(self) -> int:
        return len({bytes(self.px[i:i + 3]) for i in range(0, len(self.px), 3)})

    def ink_fraction(self, inset: int = 0) -> float:
        """Fraction of INTERIOR pixels that differ from the interior's own
        dominant colour.

        Two choices here, both learned from renders that passed a weaker check:

        * Sample the INTERIOR, not the whole frame. A render can have a
          decorated border and an empty middle, which is exactly what "correct
          dimensions, nothing in it" looks like in practice.
        * Compare against the interior's MOST COMMON colour, not against the
          top-left pixel. If the top-left happens to sit on a border stripe,
          every empty interior pixel "differs from it" and a blank middle scores
          100% ink.
        """
        counts: dict[bytes, int] = {}
        for y in range(inset, self.height - inset):
            row = (y * self.width) * 3
            for x in range(inset, self.width - inset):
                i = row + x * 3
                key = bytes(self.px[i:i + 3])
                counts[key] = counts.get(key, 0) + 1
        total = sum(counts.values())
        if not total:
            return 0.0
        dominant = max(counts.values())
        return (total - dominant) / total


def encode_png(raster: Raster) -> bytes:
    """Minimal PNG encoder: 8-bit RGB, filter type 0 on every row."""
    rows = bytearray()
    stride = raster.width * 3
    for y in range(raster.height):
        rows.append(0)
        rows += raster.px[y * stride:(y + 1) * stride]

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", raster.width, raster.height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(rows), 9)) + chunk(b"IEND", b""))


def png_dimensions(data: bytes) -> tuple[int, int]:
    """Read width and height out of the encoded bytes, not out of our intent."""
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        raise RenderGateFailed("the rendered bytes are not a PNG")
    return struct.unpack(">II", data[16:24])


def decode_png(data: bytes) -> Raster:
    """Decode our own PNG back into a pixel buffer.

    The gate measures THIS, not the raster it was handed. Measuring the
    in-memory buffer proves the drawing code worked; it says nothing about the
    encoder, and "the render succeeded and the file is blank" is a failure of
    the step after drawing at least as often as the step before it. Round-trip
    or the check is only half a check.

    Handles the subset this module emits: 8-bit RGB, filter type 0 on every row.
    Anything else raises rather than guessing.
    """
    width, height = png_dimensions(data)
    depth, colour_type = data[24], data[25]
    if (depth, colour_type) != (8, 2):
        raise RenderGateFailed(
            f"expected an 8-bit RGB PNG, the file declares depth {depth} type "
            f"{colour_type}")

    idat, pos = bytearray(), 8
    while pos + 8 <= len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        tag = data[pos + 4:pos + 8]
        if tag == b"IDAT":
            idat += data[pos + 8:pos + 8 + length]
        elif tag == b"IEND":
            break
        pos += 12 + length

    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error as exc:
        raise RenderGateFailed(f"the PNG pixel data would not decompress: {exc}") from None

    stride = width * 3
    if len(raw) != (stride + 1) * height:
        raise RenderGateFailed(
            f"the PNG carries {len(raw)} bytes of pixel data, expected "
            f"{(stride + 1) * height}")

    out = Raster(width, height, (0, 0, 0))
    for y in range(height):
        start = y * (stride + 1)
        if raw[start] != 0:
            raise RenderGateFailed(
                f"row {y} uses PNG filter {raw[start]}; this decoder only reads "
                "the unfiltered rows this module writes")
        out.px[y * stride:(y + 1) * stride] = raw[start + 1:start + 1 + stride]
    return out


def render_cover(date: str, title: str, *, width: int = 1200, height: int = 630,
                 accent: Colour | None = None) -> bytes:
    """Render an issue cover and return PNG bytes that passed the gate."""
    seed = sum(ord(c) * (i + 1) for i, c in enumerate(date))
    accent = accent or (60 + seed % 120, 90 + seed % 90, 170 - seed % 70)
    ink: Colour = (250, 250, 248)
    background: Colour = (18, 20, 26)

    r = Raster(width, height, background)
    r.rect(0, 0, width, 14, accent)
    r.rect(72, 120, 260, 8, accent)
    r.text(72, 168, date, ink, scale=6)
    words = _wrap(title.upper(), 26)[:3]
    for n, line in enumerate(words):
        r.text(72, 300 + n * 60, line, ink, scale=4)
    r.rect(72, height - 96, 160, 6, accent)

    data = encode_png(r)
    assert_render_ok(r, data, expected=(width, height))
    return data


def assert_render_ok(raster: Raster, data: bytes, *, expected: tuple[int, int],
                     min_ink: float = 0.01, min_colours: int = 3) -> None:
    """The visual render gate. Raises rather than shipping a blank cover.

    `raster` is accepted for the caller's convenience and deliberately ignored:
    every measurement below comes from `data`, the bytes that will actually be
    uploaded. A gate that reads the in-memory buffer cannot see an encoder that
    wrote an empty file at the right dimensions.
    """
    got = png_dimensions(data)
    if got != expected:
        raise RenderGateFailed(f"expected a {expected[0]}x{expected[1]} cover, the "
                               f"encoded file says {got[0]}x{got[1]}")
    decoded = decode_png(data)
    colours = decoded.distinct_colours()
    if colours < min_colours:
        raise RenderGateFailed(f"the cover has only {colours} distinct colours, which "
                               "is what a blank render looks like")
    inset = min(decoded.width, decoded.height) // 8
    ink = decoded.ink_fraction(inset=inset)
    if ink < min_ink:
        raise RenderGateFailed(
            f"only {ink:.3%} of the cover's interior differs from its background. "
            "Correct dimensions with an empty middle is the classic blank render.")


def write_cover(path: Path | str, data: bytes) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def _wrap(text: str, width: int) -> list[str]:
    lines: list[str] = []
    line = ""
    for word in text.split():
        candidate = f"{line} {word}".strip()
        if len(candidate) > width and line:
            lines.append(line)
            line = word
        else:
            line = candidate
    if line:
        lines.append(line)
    return lines
