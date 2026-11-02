"""Read painted waiting-spinner frames from disposable WeChat browser tests.

Code version: v1.0.0-codex.0
"""

from io import BytesIO

from PIL import Image


def _painted_spinner_frame(screenshot, box, path):
    """Inspect compositor pixels without accessing a provisional document context."""
    path.write_bytes(screenshot)
    center_x = box["left"] + box["width"] / 2
    center_y = box["top"] + box["height"] / 2
    crop = (
        Image.open(BytesIO(screenshot))
        .convert("RGB")
        .crop(
            (
                round(center_x - 26),
                round(center_y - 26),
                round(center_x + 26),
                round(center_y + 26),
            )
        )
    )
    pixels = crop.tobytes()
    blue_pixels = sum(
        blue > 120 and blue > red + 50 and blue > green + 20
        for red, green, blue in zip(
            pixels[::3], pixels[1::3], pixels[2::3], strict=True
        )
    )
    return blue_pixels, pixels
