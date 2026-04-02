"""Screenshot capture via Minescript's native screenshot() API.

Minescript.screenshot() triggers MC's built-in F2 screenshot which reads
the real OpenGL framebuffer. The PNG is saved to the screenshots/ dir,
then we read it, optionally convert to JPEG, and return base64.
"""

from __future__ import annotations

import base64
import glob
import io
import logging
import os
import time
from uuid import uuid4

logger = logging.getLogger("bridge")

GAME_DIR = "/headlessmc/HeadlessMC/run"
SCREENSHOTS_DIR = os.path.join(GAME_DIR, "screenshots")


def capture_screenshot(format: str = "jpeg", quality: int = 80) -> dict:
    """Take a screenshot and return base64-encoded image data.

    Blocking — must be called via run_in_executor().

    Returns:
        dict with keys: image (base64 str), format, width, height
    """
    import minescript

    filename = f"bridge_{uuid4().hex[:8]}"
    minescript.screenshot(filename)

    # Minescript appends .png if not present
    png_path = os.path.join(SCREENSHOTS_DIR, f"{filename}.png")

    # Wait for the file to appear (MC writes it asynchronously)
    for _ in range(20):
        if os.path.exists(png_path) and os.path.getsize(png_path) > 0:
            break
        time.sleep(0.1)
    else:
        raise RuntimeError(f"Screenshot file not found: {png_path}")

    from PIL import Image

    img = Image.open(png_path)
    width, height = img.size

    buf = io.BytesIO()
    if format == "png":
        img.save(buf, format="PNG")
    else:
        img = img.convert("RGB")  # JPEG doesn't support alpha
        img.save(buf, format="JPEG", quality=quality)

    image_bytes = buf.getvalue()
    image_b64 = base64.b64encode(image_bytes).decode("ascii")

    # Clean up screenshot file to avoid disk bloat
    try:
        os.remove(png_path)
    except OSError:
        pass

    return {
        "image": image_b64,
        "format": format,
        "width": width,
        "height": height,
        "size_bytes": len(image_bytes),
    }


def cleanup_old_screenshots(max_age_seconds: int = 300) -> int:
    """Remove old bridge screenshots. Returns count removed."""
    removed = 0
    now = time.time()
    for path in glob.glob(os.path.join(SCREENSHOTS_DIR, "bridge_*.png")):
        try:
            if now - os.path.getmtime(path) > max_age_seconds:
                os.remove(path)
                removed += 1
        except OSError:
            pass
    return removed
