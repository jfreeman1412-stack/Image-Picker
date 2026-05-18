"""Image read helper that quiets libpng's iCCP warnings.

Background: cv2.imread() routes PNG decoding through libpng. Many camera-
produced PNGs embed a slightly malformed sRGB ICC profile, and libpng
prints `libpng warning: iCCP: known incorrect sRGB profile` for every one.
The warnings go to stderr from C code, so Python logging filters don't help.

Workaround: read PNGs via Pillow (which tolerates bad ICC profiles silently),
then convert to a cv2-compatible BGR numpy array. Other formats still take
the cv2 fast path.
"""
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
from PIL import Image as PILImage


def read_bgr(image_path: Path) -> Optional[np.ndarray]:
    """Return a BGR numpy array like cv2.imread, or None if unreadable.

    PNGs go through Pillow to avoid noisy libpng ICC-profile warnings on stderr.
    Everything else uses cv2.imread directly.
    """
    suffix = str(image_path).lower()
    if suffix.endswith(".png"):
        try:
            with PILImage.open(image_path) as img:
                img = img.convert("RGB")
                rgb = np.asarray(img)
        except (OSError, PILImage.UnidentifiedImageError):
            return None
        return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    return cv2.imread(str(image_path))
