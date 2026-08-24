from __future__ import annotations

import hashlib
import re


LENGTH_SENTENCES = {"short": (2, 3), "medium": (4, 5), "long": (6, 8)}


def stable_length_tier(*, seed: str, relative_image_path: str, distribution: dict[str, int]) -> str:
    identity = relative_image_path.replace("\\", "/")
    bucket = int.from_bytes(
        hashlib.sha256((seed + "\0" + identity).encode("utf-8")).digest()[:8],
        "big",
    ) % 100
    short_end = distribution["short"]
    medium_end = short_end + distribution["medium"]
    return "short" if bucket < short_end else "medium" if bucket < medium_end else "long"


def character_name(relative_image_path: str) -> str:
    parts = relative_image_path.replace("\\", "/").split("/")
    if len(parts) < 2 or not parts[1]:
        raise ValueError("character preset requires first-level directories named <digits>_<character>")
    first = parts[0]
    match = re.fullmatch(r"[0-9]+_(.+)", first)
    if match is None or not match.group(1).strip():
        raise ValueError("character preset requires first-level directories named <digits>_<character>")
    return match.group(1).strip()
