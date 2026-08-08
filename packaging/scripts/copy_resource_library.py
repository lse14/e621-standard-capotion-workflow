"""Copy only distributable resource packages into a release tree."""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "core" / "src"))

from anima_core.resource_catalog import ResourceCatalog


RESOURCE_CATEGORIES = (
    "classification-indexes",
    "dropout-models",
    "ocr-models",
    "replacement-indexes",
    "tagging-models",
)
LOCAL_ONLY_RESOURCE_IDS = frozenset({
    "caption-danbooru-cl-tagger-v2-00",
    "caption-danbooru-wd-eva02-large-v3",
})
LOCAL_ONLY_RUNTIME_FORMATS = frozenset({
    "cl-tagger-v2-onnx-v1",
    "wd-eva02-large-tagger-v3-onnx-v1",
})
LOCAL_ONLY_FILE_NAMES = frozenset({
    "model_metadata.json",
    "model_ood_ref.npz",
    "model_tag_metrics.npz",
    "model_vocabulary.json",
    "selected_tags.csv",
})


def assert_no_local_only_leaks(root: Path) -> None:
    root = root.resolve()
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if LOCAL_ONLY_RESOURCE_IDS & set(relative.parts):
            raise ValueError(f"release tree contains a local-only resource path: {relative}")
        if path.name.lower() in LOCAL_ONLY_FILE_NAMES:
            raise ValueError(f"release tree contains a local-only model component: {relative}")
        if path.name != "resource.json":
            continue
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"release resource manifest is unreadable: {relative}") from exc
        if not isinstance(manifest, dict):
            raise ValueError(f"release resource manifest is not an object: {relative}")
        distribution = manifest.get("distribution")
        if isinstance(distribution, dict) and distribution.get("mode") == "local-only":
            raise ValueError(f"release tree contains a local-only resource manifest: {relative}")
        if (
            manifest.get("resourceId") in LOCAL_ONLY_RESOURCE_IDS
            or manifest.get("runtimeFormat") in LOCAL_ONLY_RUNTIME_FORMATS
        ):
            raise ValueError(f"release tree contains a forbidden Danbooru tagging resource: {relative}")


def copy_distributable(source: Path, destination: Path) -> dict[str, object]:
    source = source.resolve()
    destination = destination.resolve()
    if destination.exists():
        raise ValueError(f"resource library destination already exists: {destination}")
    try:
        destination.relative_to(source)
    except ValueError:
        pass
    else:
        raise ValueError("resource library destination must not be inside its source")

    snapshot = ResourceCatalog(source).scan()
    if snapshot.invalid:
        details = "; ".join(f"{item.relative_path}: {item.reason}" for item in snapshot.invalid)
        raise ValueError(f"resource library contains invalid packages: {details}")

    destination.mkdir(parents=True)
    shutil.copy2(source / "defaults.json", destination / "defaults.json")
    for category in RESOURCE_CATEGORIES:
        (destination / category).mkdir()

    copied: list[str] = []
    excluded: list[str] = []
    for package in sorted(snapshot.packages, key=lambda item: (item.kind, item.resource_id)):
        if package.distribution["mode"] == "local-only":
            excluded.append(package.resource_id)
            continue
        relative_root = package.package_root.relative_to(source)
        shutil.copytree(package.package_root, destination / relative_root)
        copied.append(package.resource_id)

    assert_no_local_only_leaks(destination)
    return {
        "schemaVersion": 1,
        "copiedResourceIds": copied,
        "excludedLocalOnlyResourceIds": excluded,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    arguments = parser.parse_args()
    result = copy_distributable(arguments.source, arguments.destination)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
