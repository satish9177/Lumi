"""A minimal, dependency-free PNG encoder: standard library only (`zlib`, `struct`, `binascii`).

The S5 capture pipeline needs to turn a raw pixel buffer into a compact, standard image format before
it crosses any process boundary, and the project's own conservative dependency policy (no new native
binary, nothing the packaging/signing gate has not already reviewed -- see `docs/PACKAGING.md` and the
S4 review's package-impact section) rules out Pillow, `mss` or any other imaging library. PNG's format
is simple enough that a correct encoder is a few dozen lines over `zlib.compress`, which is already
part of the Python standard library and already bundled.

Deliberately narrow: 8-bit RGB (truecolor, no palette, no interlacing), one filter (`None`), one zlib
call. This is not a general-purpose PNG writer; it exists to encode exactly one thing, a captured
window's pixels, and nothing else in this codebase should reach for it.
"""

import struct
import zlib
from typing import Final

_SIGNATURE: Final = b"\x89PNG\r\n\x1a\n"
_COLOR_TYPE_RGB: Final = 2
_BIT_DEPTH: Final = 8
_FILTER_NONE: Final = 0


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def encode_png_rgb(width: int, height: int, bgra: bytes) -> bytes:
    """Encode a top-down, tightly-packed 32bpp BGRA buffer (exactly what `capture_win32.capture`
    returns) as an 8-bit RGB PNG.

    The alpha channel is dropped, not passed through: `PrintWindow`'s captured alpha is unreliable for
    some GPU-composited windows (frequently zero even though the colour channels are correct), so
    treating every pixel as fully opaque is the safe, well-known mitigation rather than a lossy choice.
    """
    if width <= 0 or height <= 0:
        raise ValueError("width and height must be positive")
    if len(bgra) != width * height * 4:
        raise ValueError("the pixel buffer does not match width * height * 4 bytes")
    row_stride = width * 4
    raw = bytearray()
    for y in range(height):
        raw.append(_FILTER_NONE)
        row = bgra[y * row_stride : (y + 1) * row_stride]
        # BGRA -> RGB, alpha dropped: swap B and R, one row at a time.
        rgb = bytearray(width * 3)
        rgb[0::3] = row[2::4]  # R <- B-channel-position's actual red is index 2 (B,G,R,A order)
        rgb[1::3] = row[1::4]
        rgb[2::3] = row[0::4]
        raw.extend(rgb)
    ihdr = struct.pack(">IIBBBBB", width, height, _BIT_DEPTH, _COLOR_TYPE_RGB, 0, 0, 0)
    idat = zlib.compress(bytes(raw), level=6)
    return _SIGNATURE + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")
