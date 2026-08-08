from __future__ import annotations

import re
from collections.abc import Mapping


MAX_TAGS = 16_384
MAX_TAG_BYTES = 512


class ClassifyTextError(ValueError):
    pass


def normalize_display_tag(value: str) -> str:
    """Recover the tagger identity without applying semantic aliases."""
    unescaped = re.sub(r"\\(?=[^A-Za-z0-9_\s])", "", value)
    # Caption maps each underscore to exactly one space, so the inverse must stay
    # one-to-one; collapsing runs of whitespace loses consecutive-underscore keys.
    result = re.sub(r"\s", "_", unescaped.strip().casefold())
    if len(result.encode("utf-8")) > MAX_TAG_BYTES:
        raise ClassifyTextError("caption tag exceeds 512 UTF-8 bytes")
    return result


def parse_tag_text(content: str, caption_format: Mapping[str, object], txt_provenance: str) -> list[str]:
    if not isinstance(content, str):
        raise ClassifyTextError("caption text must be a string")
    if content.startswith("\ufeff"):
        content = content[1:]
    if not content.strip():
        return []
    # Trigger removal happens before de-duplication: Caption does not de-duplicate
    # its prefix against the model tags, so a genuine tag repeating a trigger term
    # must survive (ROADMAP.md 6.4).
    ordered = [tag for tag in (normalize_display_tag(raw) for raw in content.split(",")) if tag]
    parsed: list[str] = []
    seen: set[str] = set()
    for tag in _without_trigger_prefix(ordered, caption_format, txt_provenance):
        if tag not in seen:
            parsed.append(tag)
            seen.add(tag)
            if len(parsed) > MAX_TAGS:
                raise ClassifyTextError("caption contains more than 16384 unique tags")
    return parsed


def _without_trigger_prefix(
    ordered: list[str], caption_format: Mapping[str, object], txt_provenance: str
) -> list[str]:
    if txt_provenance != "module1_written" or caption_format.get("triggersEnabled") is not True:
        return ordered
    raw_terms = caption_format.get("triggerTerms")
    if not isinstance(raw_terms, list) or not raw_terms:
        return ordered
    terms = [normalize_display_tag(term) for term in raw_terms if isinstance(term, str)]
    if len(terms) != len(raw_terms) or not all(terms):
        raise ClassifyTextError("trigger terms are invalid")
    # Only a complete, ordered prefix has Caption provenance. A matching tag
    # later in a preserved TXT remains genuine input and must not be removed.
    if ordered[: len(terms)] == terms:
        return ordered[len(terms):]
    return ordered
