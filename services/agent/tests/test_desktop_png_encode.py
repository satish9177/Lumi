"""The stdlib-only PNG encoder used to turn a captured window into a compact wire payload.

Verified with a small, test-local decoder (not shipped in `app/`) rather than a new dependency: this
suite is the actual proof that a real PNG viewer would decode the encoder's bytes correctly.
"""

import struct
import zlib

import pytest

from app.desktop.png_encode import encode_png_rgb


def _decode_png_rgb(data: bytes) -> tuple[int, int, list[tuple[int, int, int]]]:
    """A minimal, test-only PNG reader: 8-bit RGB, no interlace, one IDAT. Enough to prove round-trip
    correctness without adding an imaging dependency to the project."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    offset = 8
    width = height = None
    idat = b""
    while offset < len(data):
        (length,) = struct.unpack(">I", data[offset : offset + 4])
        kind = data[offset + 4 : offset + 8]
        chunk_data = data[offset + 8 : offset + 8 + length]
        if kind == b"IHDR":
            width, height, bit_depth, color_type = struct.unpack(">IIBB", chunk_data[:10])
            assert bit_depth == 8 and color_type == 2
        elif kind == b"IDAT":
            idat += chunk_data
        offset += 12 + length
    assert width is not None and height is not None
    raw = zlib.decompress(idat)
    stride = width * 3
    pixels: list[tuple[int, int, int]] = []
    for y in range(height):
        row_start = y * (stride + 1)
        filter_byte = raw[row_start]
        assert filter_byte == 0
        row = raw[row_start + 1 : row_start + 1 + stride]
        for x in range(width):
            r, g, b = row[x * 3 : x * 3 + 3]
            pixels.append((r, g, b))
    return width, height, pixels


def test_round_trips_exact_rgb_values() -> None:
    # BGRA input: red, green, blue, white pixels, all fully opaque.
    bgra = bytes(
        [
            0, 0, 255, 255,
            0, 255, 0, 255,
            255, 0, 0, 255,
            255, 255, 255, 255,
        ]
    )
    png = encode_png_rgb(2, 2, bgra)
    width, height, pixels = _decode_png_rgb(png)
    assert (width, height) == (2, 2)
    assert pixels == [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255)]


def test_the_alpha_channel_is_dropped_not_passed_through() -> None:
    """A pixel with alpha=0 (the known PrintWindow/DirectComposition gotcha) still encodes as opaque
    colour: alpha never reaches the PNG at all, so a zero-alpha capture is never invisible."""
    bgra = bytes([10, 20, 30, 0])  # B=10 G=20 R=30, alpha=0
    png = encode_png_rgb(1, 1, bgra)
    _, _, pixels = _decode_png_rgb(png)
    assert pixels == [(30, 20, 10)]


def test_a_larger_realistic_image_round_trips() -> None:
    width, height = 64, 48
    bgra = bytearray(width * height * 4)
    for i in range(width * height):
        bgra[i * 4 : i * 4 + 4] = bytes([(i * 7) % 256, (i * 13) % 256, (i * 3) % 256, 255])
    png = encode_png_rgb(width, height, bytes(bgra))
    decoded_width, decoded_height, pixels = _decode_png_rgb(png)
    assert (decoded_width, decoded_height) == (width, height)
    for i, (r, g, b) in enumerate(pixels):
        assert (r, g, b) == ((i * 3) % 256, (i * 13) % 256, (i * 7) % 256)


@pytest.mark.parametrize(("width", "height"), [(0, 10), (10, 0), (-1, 10)])
def test_a_non_positive_dimension_is_refused(width: int, height: int) -> None:
    with pytest.raises(ValueError):
        encode_png_rgb(width, height, b"\x00" * max(width, 1) * max(height, 1) * 4)


def test_a_mismatched_buffer_length_is_refused() -> None:
    with pytest.raises(ValueError):
        encode_png_rgb(2, 2, b"\x00" * 10)  # needs 16 bytes
