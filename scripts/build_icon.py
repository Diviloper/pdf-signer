"""Rasterizes images/algorae-logo.svg into a multi-resolution
images/app_icon.ico for use as the Windows executable icon.

Run with: uv run python scripts/build_icon.py
"""

import struct
import sys
from pathlib import Path

from PyQt6.QtCore import QBuffer, QIODevice
from PyQt6.QtGui import QImage, QPainter
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parent.parent
SVG_PATH = ROOT / "images" / "algorae-logo.svg"
ICO_PATH = ROOT / "images" / "app_icon.ico"
SIZES = (16, 32, 48, 64, 128, 256)


def render_png_bytes(renderer: QSvgRenderer, size: int) -> bytes:
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    renderer.render(painter)
    painter.end()
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(buffer.data())


def build_ico() -> None:
    renderer = QSvgRenderer(str(SVG_PATH))
    if not renderer.isValid():
        raise RuntimeError(f"Could not load SVG: {SVG_PATH}")

    frames = [(size, render_png_bytes(renderer, size)) for size in SIZES]

    # ICO container format: a 6-byte header, one 16-byte directory entry per
    # frame, followed by the raw (here, PNG-compressed) image data.
    header = struct.pack("<HHH", 0, 1, len(frames))
    entries = b""
    data = b""
    offset = 6 + 16 * len(frames)
    for size, png in frames:
        wh = size if size < 256 else 0  # 0 means 256 in the ICO format
        entries += struct.pack("<BBBBHHII", wh, wh, 0, 0, 1, 32, len(png), offset)
        data += png
        offset += len(png)

    ICO_PATH.write_bytes(header + entries + data)
    print(f"Wrote {ICO_PATH} ({len(frames)} sizes: {[s for s, _ in frames]})")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    build_ico()
