"""Several views of a model on one PNG sheet: run_script's picture.

Pure Python, no imaging library. SOLIDWORKS' IModelDoc2.SaveBMP writes the
view as an uncompressed 24-bit BMP, rows bottom-up (SP4.1, verified: 600 x 450
in about 0.01 s), which is cheap to read; zlib writes the PNG.
"""

import struct
import zlib

_GUTTER = 2                  # pixels between the views
_GUTTER_RGB = b"\x80\x80\x80"


def read_bmp(data: bytes) -> tuple:
    """(width, height, rows) of an uncompressed 24- or 32-bit BMP, rows top
    down as RGB bytes; pure, unit-tested."""
    if data[:2] != b"BM":
        raise ValueError("not a BMP file")
    offset = struct.unpack_from("<I", data, 10)[0]
    width, height, _planes, bits, compression = struct.unpack_from("<iiHHI", data, 18)
    if bits not in (24, 32) or compression not in (0, 3):
        raise ValueError(f"a {bits}-bit BMP with compression {compression} is not supported")
    step = bits // 8
    stride = (width * step + 3) // 4 * 4
    rows = []
    for index in range(abs(height)):
        start = offset + index * stride
        bgr = data[start:start + width * step]
        rgb = bytearray(width * 3)
        rgb[0::3], rgb[1::3], rgb[2::3] = bgr[2::step], bgr[1::step], bgr[0::step]
        rows.append(bytes(rgb))
    if height > 0:  # a positive height stores the bottom row first
        rows.reverse()
    return width, abs(height), rows


def tile(images: list, columns: int = 2) -> tuple:
    """(width, height, rows) of equally sized RGB images laid out row by row,
    `columns` across, with a grey gutter between them; pure, unit-tested."""
    if not images:
        raise ValueError("no images to tile")
    width, height = images[0][0], images[0][1]
    if any((w, h) != (width, height) for w, h, _ in images):
        raise ValueError("the images differ in size")
    columns = min(columns, len(images))
    count = -(-len(images) // columns)
    blank = b"\xff" * (width * 3)
    gutter = _GUTTER_RGB * _GUTTER
    full = _GUTTER_RGB * (columns * width + (columns - 1) * _GUTTER)
    rows = []
    for band in range(count):
        if band:
            rows.extend([full] * _GUTTER)
        group = images[band * columns:(band + 1) * columns]
        for y in range(height):
            rows.append(gutter.join([image[2][y] for image in group]
                                    + [blank] * (columns - len(group))))
    return columns * width + (columns - 1) * _GUTTER, len(rows), rows


def png_bytes(width: int, height: int, rows: list) -> bytes:
    """An 8-bit RGB PNG of top-down RGB rows; pure, unit-tested."""
    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    raw = b"".join(b"\x00" + row for row in rows)  # filter 0 on every row
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6))
            + chunk(b"IEND", b""))
