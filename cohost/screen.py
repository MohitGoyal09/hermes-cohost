"""Fallback frame source when OBS is not reachable: grab the local screen."""

from __future__ import annotations

import io
import logging

from PIL import Image, ImageGrab

logger = logging.getLogger(__name__)


def downscale_jpeg(img: Image.Image, max_width: int) -> bytes:
    if img.width > max_width:
        img = img.resize((max_width, round(img.height * max_width / img.width)))
    out = io.BytesIO()
    img.convert("RGB").save(out, format="JPEG", quality=70)
    return out.getvalue()


def grab_screen(max_width: int = 1024) -> bytes | None:
    try:
        return downscale_jpeg(ImageGrab.grab(), max_width)
    except Exception as exc:  # no permission, headless, unsupported OS
        logger.debug("cohost: screen grab failed: %s", exc)
        return None
