"""EXIF capture date for camera images (header read only)."""

from __future__ import annotations

from pathlib import Path

from PIL import ExifTags, Image

EXIF_IFD = 0x8769


def exif_date(path: Path) -> str | None:
    """ISO timestamp from DateTimeOriginal/DateTime, or None if absent or unreadable."""
    try:
        with Image.open(path) as im:
            exif = im.getexif()
            raw = exif.get_ifd(EXIF_IFD).get(ExifTags.Base.DateTimeOriginal.value) or exif.get(
                ExifTags.Base.DateTime.value
            )
    except Exception:
        return None
    if not raw or not isinstance(raw, str):
        return None
    try:
        date, _, time = raw.strip().partition(" ")
        return f"{date.replace(':', '-')}T{time or '00:00:00'}"
    except ValueError:
        return None
