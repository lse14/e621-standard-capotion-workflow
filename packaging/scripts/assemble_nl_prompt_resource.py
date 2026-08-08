"""Copy and hash-pin one frozen NL prompt into a release install tree."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


REQUIRED_SNIPPETS = {
    "nl-default-prompt-v1": (
        "Do not output JSON, XML, Markdown, code fences",
        "Describe visible adult content objectively",
        "120-180+ words",
    ),
    "nl-default-prompt-v2": (
        "surrounding fixed JSON protocol",
        "same character",
        "non-human characters and creatures",
        "120-180+ words",
    ),
    "nl-default-prompt-v3": (
        "short, medium, or long",
        "approximate glyph style",
        "carrier",
        "bubble",
        "untrusted data",
    ),
}
V4_FRAGMENT_NAMES = ("base", "general", "style", "character", "short", "medium", "long")
V4_FRAGMENT_SNIPPETS = {
    "base": ("exactly these keys: nl, count, layout, sameCharacterRepeated", "untrusted data", "complete visible text"),
    "general": ("subjects", "fixed appearance", "visible text"),
    "style": ("only observable picture content and composition", "Do not describe artist, style, medium, rendering, quality, lighting", "visible color belonging to an object"),
    "character": ("structured primaryCharacterName", "Do not describe the main character's fixed appearance", "Other character names"),
    "short": ("exactly 2-3 sentences",),
    "medium": ("exactly 4-5 sentences",),
    "long": ("exactly 6-8 sentences",),
}
MAX_PROMPT_BYTES = 65_536


def _resource_id(source: Path) -> str:
    resource_id = source.stem
    if source.suffix != ".txt" or resource_id not in REQUIRED_SNIPPETS:
        raise ValueError("NL prompt source filename is not a supported frozen resource")
    return resource_id


def _validate(data: bytes, *, resource_id: str) -> None:
    if not data or len(data) > MAX_PROMPT_BYTES:
        raise ValueError("default NL prompt is empty or exceeds 64 KiB")
    try:
        prompt = data.decode("utf-8-sig").replace("\r\n", "\n").strip()
    except UnicodeDecodeError as exc:
        raise ValueError("default NL prompt must be UTF-8") from exc
    if "\x00" in prompt:
        raise ValueError("default NL prompt contains NUL")
    missing = [snippet for snippet in REQUIRED_SNIPPETS[resource_id] if snippet not in prompt]
    if missing:
        raise ValueError("default NL prompt lost frozen constraints: " + "; ".join(missing))


def _validate_v4(data: bytes, *, fragment_name: str) -> None:
    if data.startswith(b"\xef\xbb\xbf"):
        raise ValueError("NL v4 prompt fragment must not contain a BOM")
    if not data or len(data) > MAX_PROMPT_BYTES or b"\x00" in data:
        raise ValueError("NL v4 prompt fragment is empty, contains NUL, or exceeds 64 KiB")
    try:
        text = data.decode("utf-8").replace("\r\n", "\n").strip()
    except UnicodeDecodeError as exc:
        raise ValueError("NL v4 prompt fragment must be strict UTF-8") from exc
    missing = [snippet for snippet in V4_FRAGMENT_SNIPPETS[fragment_name] if snippet not in text]
    if missing:
        raise ValueError(f"NL v4 fragment {fragment_name} lost frozen constraints: " + "; ".join(missing))


def assemble(source: Path, install_root: Path) -> dict[str, object]:
    resolved_source = source.resolve(strict=True)
    resource_id = _resource_id(resolved_source)
    resource_relative_path = Path(f"resources/{resource_id}.txt")
    manifest_relative_path = Path(f"manifests/resources/{resource_id}.json")
    data = resolved_source.read_bytes()
    _validate(data, resource_id=resource_id)
    root = install_root.resolve()
    destination = root / resource_relative_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
    manifest = {
        "schemaVersion": 1,
        "resourceId": resource_id,
        "owner": "core",
        "relativePath": str(resource_relative_path).replace("/", "\\"),
        "sizeBytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    manifest_path = root / manifest_relative_path
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return manifest


def assemble_v4(source_root: Path, install_root: Path) -> tuple[dict[str, object], ...]:
    """Assemble the seven v4 layers without maintaining prose in Python."""
    resolved_root = source_root.resolve(strict=True)
    root = install_root.resolve()
    manifests: list[dict[str, object]] = []
    for fragment_name in V4_FRAGMENT_NAMES:
        resource_id = f"nl-default-prompt-v4-{fragment_name}"
        source = (resolved_root / f"{resource_id}.txt").resolve(strict=True)
        if source.parent != resolved_root:
            raise ValueError("NL v4 fragment escaped its source root")
        data = source.read_bytes()
        _validate_v4(data, fragment_name=fragment_name)
        resource_relative_path = Path(f"resources/{resource_id}.txt")
        manifest_relative_path = Path(f"manifests/resources/{resource_id}.json")
        destination = root / resource_relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        manifest = {
            "schemaVersion": 1,
            "resourceId": resource_id,
            "owner": "nl",
            "relativePath": str(resource_relative_path).replace("/", "\\"),
            "sizeBytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        manifest_path = root / manifest_relative_path
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        manifests.append(manifest)
    return tuple(manifests)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path)
    parser.add_argument("--v4-source-root", type=Path)
    parser.add_argument("--install-root", type=Path, required=True)
    arguments = parser.parse_args()
    if (arguments.source is None) == (arguments.v4_source_root is None):
        parser.error("exactly one of --source or --v4-source-root is required")
    if arguments.v4_source_root is not None:
        assemble_v4(arguments.v4_source_root, arguments.install_root)
    else:
        assemble(arguments.source, arguments.install_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
