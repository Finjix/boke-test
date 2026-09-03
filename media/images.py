"""Small dependency-free image validation helpers."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

from utils.errors import ValidationError


@dataclass(frozen=True)
class ImageInfo:
    path: Path
    width: int
    height: int
    format: str


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    if not data.startswith(b"\xff\xd8"):
        return None
    offset = 2
    while offset + 9 < len(data):
        if data[offset] != 0xFF:
            offset += 1
            continue
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        if offset >= len(data):
            break
        marker = data[offset]
        offset += 1
        if marker in {0xD8, 0xD9}:
            continue
        if offset + 2 > len(data):
            break
        length = int.from_bytes(data[offset : offset + 2], "big")
        if length < 2 or offset + length > len(data):
            break
        if marker in set(range(0xC0, 0xC4)) | set(range(0xC5, 0xC8)) | set(range(0xC9, 0xCC)) | set(range(0xCD, 0xD0)):
            if length >= 7:
                height = int.from_bytes(data[offset + 3 : offset + 5], "big")
                width = int.from_bytes(data[offset + 5 : offset + 7], "big")
                return width, height
        offset += length
    return None


def inspect_image(path: Path) -> ImageInfo:
    """Validate a downloaded PNG/JPEG/WebP and return its dimensions."""

    path = Path(path)
    if not path.is_file() or path.stat().st_size <= 0:
        raise ValidationError(f"image output is missing or empty: {path}")
    data = path.read_bytes()
    width = height = 0
    image_format = ""
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        width, height = struct.unpack(">II", data[16:24])
        image_format = "png"
    elif data.startswith(b"RIFF") and data[8:12] == b"WEBP" and len(data) >= 30:
        image_format = "webp"
        if data[12:16] == b"VP8X" and len(data) >= 30:
            width = 1 + int.from_bytes(data[24:27], "little")
            height = 1 + int.from_bytes(data[27:30], "little")
        elif data[12:16] == b"VP8 " and len(data) >= 30:
            width, height = struct.unpack("<HH", data[26:30])
        elif data[12:16] == b"VP8L" and len(data) >= 25:
            bits = int.from_bytes(data[21:25], "little")
            width = (bits & 0x3FFF) + 1
            height = ((bits >> 14) & 0x3FFF) + 1
    elif data.startswith(b"\xff\xd8"):
        dimensions = _jpeg_dimensions(data)
        if dimensions:
            width, height = dimensions
            image_format = "jpeg"
    if not image_format or width <= 0 or height <= 0:
        raise ValidationError(f"unsupported or corrupt image output: {path.name}")
    return ImageInfo(path=path, width=width, height=height, format=image_format)
