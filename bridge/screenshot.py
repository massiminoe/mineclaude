"""Screenshot capture via ffmpeg x11grab from the Xvfb display.

Captures the virtual framebuffer directly — bypasses MC's Java screenshot
code which fails on ARM64 Mesa (NativeImage.writeTo() produces 0-byte PNGs).
"""

from __future__ import annotations

import base64
import io
import logging
import subprocess

logger = logging.getLogger("bridge")

DISPLAY = ":99"
DEFAULT_SIZE = "854x480"


def capture_screenshot(format: str = "jpeg", quality: int = 80) -> dict:
    """Capture a single frame from Xvfb via ffmpeg.

    Blocking — must be called via run_in_executor().

    Returns:
        dict with keys: image (base64 str), format, width, height
    """
    fmt_flag = "png" if format == "png" else "mjpeg"
    quality_args = [] if format == "png" else ["-q:v", str(max(1, min(31, 32 - int(quality * 31 / 100))))]

    cmd = [
        "ffmpeg", "-y",
        "-f", "x11grab",
        "-video_size", DEFAULT_SIZE,
        "-i", DISPLAY,
        "-frames:v", "1",
        *quality_args,
        "-f", "image2",
        "-vcodec", fmt_flag,
        "pipe:1",
    ]

    result = subprocess.run(
        cmd,
        capture_output=True,
        timeout=5,
    )

    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace")[-500:]
        raise RuntimeError(f"ffmpeg screenshot failed (rc={result.returncode}): {stderr}")

    image_bytes = result.stdout
    if not image_bytes:
        raise RuntimeError("ffmpeg produced no output")

    image_b64 = base64.b64encode(image_bytes).decode("ascii")

    # Parse dimensions from ffmpeg (we know the input size)
    parts = DEFAULT_SIZE.split("x")
    width, height = int(parts[0]), int(parts[1])

    return {
        "image": image_b64,
        "format": format,
        "width": width,
        "height": height,
        "size_bytes": len(image_bytes),
    }
