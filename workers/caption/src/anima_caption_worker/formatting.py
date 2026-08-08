from __future__ import annotations

import re
from typing import Sequence

from .model import CaptionPrediction


MAX_FORMATTED_TXT_BYTES = 262_144
ESCAPE_PATTERN = re.compile(r"([\\()])")


class CaptionFormattingError(RuntimeError):
    pass


def display_tag(raw_tag: str, policy: dict[str, object]) -> str:
    value = raw_tag.replace("_", " ") if policy["replaceUnderscoresWithSpaces"] else raw_tag
    if policy["preserveEscapes"]:
        value = ESCAPE_PATTERN.sub(r"\\\1", value)
    if not value or value != value.strip() or any(character in value for character in ",\r\n\x00"):
        raise CaptionFormattingError("a caption tag is not representable in the frozen TXT format")
    return value


def format_caption(predictions: Sequence[CaptionPrediction], policy: dict[str, object]) -> str:
    if not predictions:
        raise CaptionFormattingError("caption formatting requires at least one model tag")
    values: list[str] = []
    if policy["triggersEnabled"]:
        values.extend(display_tag(str(term), policy) for term in policy["triggerTerms"])
    values.extend(display_tag(prediction.raw_tag, policy) for prediction in predictions)
    result = ", ".join(values)
    if len(result.encode("utf-8")) > MAX_FORMATTED_TXT_BYTES:
        raise CaptionFormattingError("formatted caption exceeds 256 KiB")
    return result
