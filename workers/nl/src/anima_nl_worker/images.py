from __future__ import annotations

import base64
import io
from pathlib import Path

from PIL import Image, ImageOps


MAX_IMAGE_PIXELS = 8_000_000
MAX_IMAGE_SIDE = 4_096
MAX_ENCODED_IMAGE_BYTES = 12_582_912


class NlImageError(ValueError):
    pass


def encode_image_data_url(path: str | Path) -> str:
    try:
        with Image.open(path) as source:
            image = ImageOps.exif_transpose(source)
            if image.width * image.height > MAX_IMAGE_PIXELS or max(image.size) > MAX_IMAGE_SIDE:
                scale = min((MAX_IMAGE_PIXELS / (image.width * image.height)) ** 0.5, MAX_IMAGE_SIDE / max(image.size))
                image = image.resize((max(1, round(image.width * scale)), max(1, round(image.height * scale))), Image.Resampling.LANCZOS)
            if image.mode in {"RGBA", "LA"} or (image.mode == "P" and "transparency" in image.info):
                background = Image.new("RGBA", image.size, "#FFFFFF")
                background.alpha_composite(image.convert("RGBA"))
                image = background.convert("RGB")
            else:
                image = image.convert("RGB")
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=95, optimize=True)
    except (OSError, ValueError) as exc:
        raise NlImageError("image cannot be decoded and encoded") from exc
    data = output.getvalue()
    if len(data) > MAX_ENCODED_IMAGE_BYTES:
        raise NlImageError("encoded image exceeds 12 MiB")
    return "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")
