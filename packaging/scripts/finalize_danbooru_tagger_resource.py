"""Finalize manually installed Danbooru tagger files without network access."""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "core" / "src"))

from anima_core.contracts import canonical_json
from anima_core.path_safety import sha256_file
from anima_core.resource_catalog import ResourcePackage


WD_PREPROCESS = {
    "inputSize": [448, 448],
    "layout": "NHWC",
    "padToSquare": True,
    "backgroundColor": "#FFFFFF",
    "interpolation": "bicubic",
    "channelOrder": "BGR",
    "valueRange": [0, 255],
}


class TaggerResourceFinalizationError(ValueError):
    pass


@dataclass(frozen=True)
class TaggerSpec:
    key: str
    resource_id: str
    resource_version: str
    runtime_format: str
    raw_files: tuple[str, ...]
    entrypoints: dict[str, str]
    vocabulary_file: str
    tag_count: int
    model_categories: tuple[str, ...]
    adjustable_categories: tuple[str, ...]
    excluded_categories: tuple[str, ...]
    generated_json: dict[str, object]
    source_url: str
    license_url: str
    display_name: dict[str, str]
    description: dict[str, str]

    @property
    def runtime_files(self) -> tuple[str, ...]:
        return tuple(sorted((*self.raw_files, *self.generated_json)))


SPECS = {
    "cl": TaggerSpec(
        key="cl",
        resource_id="caption-danbooru-cl-tagger-v2-00",
        resource_version="cella110n/cl_tagger_v2:v2_00",
        runtime_format="cl-tagger-v2-onnx-v1",
        raw_files=("model.onnx", "model.onnx.data", "model_metadata.json", "model_vocabulary.json"),
        entrypoints={
            "model": "model.onnx",
            "modelData": "model.onnx.data",
            "metadata": "model_metadata.json",
            "vocabulary": "model_vocabulary.json",
            "thresholds": "thresholds.json",
        },
        vocabulary_file="model_vocabulary.json",
        tag_count=106_536,
        model_categories=("General", "Character", "Copyright", "Meta", "Rating", "Quality"),
        adjustable_categories=("general", "character", "copyright"),
        excluded_categories=("meta", "rating", "quality"),
        generated_json={"thresholds.json": {"general": 0.55, "character": 0.55, "copyright": 0.55}},
        source_url="https://huggingface.co/cella110n/cl_tagger_v2/tree/main/v2_00",
        license_url="https://huggingface.co/cella110n/cl_tagger_v2/blob/main/LICENSE.md",
        display_name={"zh-CN": "CL Tagger v2（v2_00）", "en": "CL Tagger v2 (v2_00)"},
        description={
            "zh-CN": "Danbooru 默认 Tagger；由用户从官方受限仓库手动安装。",
            "en": "Default Danbooru tagger, manually installed from the official gated repository.",
        },
    ),
    "wd": TaggerSpec(
        key="wd",
        resource_id="caption-danbooru-wd-eva02-large-v3",
        resource_version="SmilingWolf/wd-eva02-large-tagger-v3",
        runtime_format="wd-eva02-large-tagger-v3-onnx-v1",
        raw_files=("model.onnx", "selected_tags.csv"),
        entrypoints={
            "model": "model.onnx",
            "selectedTags": "selected_tags.csv",
            "preprocess": "preprocess.json",
            "thresholds": "thresholds.json",
        },
        vocabulary_file="selected_tags.csv",
        tag_count=10_861,
        model_categories=("general", "character", "rating"),
        adjustable_categories=("general", "character"),
        excluded_categories=("rating",),
        generated_json={
            "preprocess.json": WD_PREPROCESS,
            "thresholds.json": {"general": 0.5296, "character": 0.5296},
        },
        source_url="https://huggingface.co/SmilingWolf/wd-eva02-large-tagger-v3",
        license_url="https://www.apache.org/licenses/LICENSE-2.0",
        display_name={"zh-CN": "WD EVA02-Large v3", "en": "WD EVA02-Large v3"},
        description={
            "zh-CN": "可选 Danbooru Tagger；由用户从官方仓库手动安装。",
            "en": "Optional Danbooru tagger, manually installed from the official repository.",
        },
    ),
}


def _json_bytes(value: object) -> bytes:
    return (canonical_json(value) + "\n").encode("utf-8")


def _read_json(path: Path, field: str) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TaggerResourceFinalizationError(f"{field} is not strict UTF-8 JSON") from exc


def _valid_tag(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value.encode("utf-8")) > 512
        or any(character in value for character in ",\r\n\x00")
    ):
        raise TaggerResourceFinalizationError(f"{field} contains an invalid tag")
    return value


def _indexed_names(value: object, expected_count: int) -> tuple[str, ...]:
    if isinstance(value, list):
        raw_names = value
    elif isinstance(value, dict) and set(value) == {str(index) for index in range(expected_count)}:
        raw_names = [value[str(index)] for index in range(expected_count)]
    else:
        raise TaggerResourceFinalizationError("CL vocabulary idx_to_tag is invalid")
    if len(raw_names) != expected_count:
        raise TaggerResourceFinalizationError("CL vocabulary tag count is invalid")
    names = tuple(_valid_tag(name, "CL vocabulary") for name in raw_names)
    if len(set(names)) != len(names):
        raise TaggerResourceFinalizationError("CL vocabulary contains duplicate tags")
    return names


def _validate_cl_inputs(package_root: Path, spec: TaggerSpec) -> None:
    metadata = _read_json(package_root / "model_metadata.json", "model_metadata.json")
    if not isinstance(metadata, dict) or not metadata:
        raise TaggerResourceFinalizationError("model_metadata.json must be a non-empty object")
    for key in ("tag_count", "num_tags", "num_classes"):
        if key in metadata and metadata[key] != spec.tag_count:
            raise TaggerResourceFinalizationError(f"model_metadata.json {key} does not match v2_00")
    for key in ("image_size", "input_size"):
        if key in metadata and metadata[key] not in (384, "384", [384, 384], [3, 384, 384]):
            raise TaggerResourceFinalizationError(f"model_metadata.json {key} does not describe 384px input")

    vocabulary = _read_json(package_root / spec.vocabulary_file, spec.vocabulary_file)
    required = {"idx_to_tag", "tag_to_idx", "tag_to_category"}
    if not isinstance(vocabulary, dict) or not required.issubset(vocabulary):
        raise TaggerResourceFinalizationError("CL vocabulary fields are incomplete")
    names = _indexed_names(vocabulary["idx_to_tag"], spec.tag_count)
    indexes = vocabulary["tag_to_idx"]
    categories = vocabulary["tag_to_category"]
    if not isinstance(indexes, dict) or set(indexes) != set(names):
        raise TaggerResourceFinalizationError("CL vocabulary tag_to_idx is invalid")
    if any(type(indexes[name]) is not int or indexes[name] != index for index, name in enumerate(names)):
        raise TaggerResourceFinalizationError("CL vocabulary indices are inconsistent")
    if not isinstance(categories, dict) or set(categories) != set(names):
        raise TaggerResourceFinalizationError("CL vocabulary tag_to_category is invalid")
    allowed = {category.lower() for category in spec.model_categories}
    if any(not isinstance(categories[name], str) or categories[name].lower() not in allowed for name in names):
        raise TaggerResourceFinalizationError("CL vocabulary contains an invalid category")


def _validate_wd_inputs(package_root: Path, spec: TaggerSpec) -> None:
    names: list[str] = []
    try:
        with (package_root / spec.vocabulary_file).open("r", encoding="utf-8-sig", newline="") as source:
            reader = csv.DictReader(source)
            if reader.fieldnames is None or not {"name", "category"}.issubset(reader.fieldnames):
                raise TaggerResourceFinalizationError("selected_tags.csv columns are incomplete")
            for row in reader:
                names.append(_valid_tag(row.get("name"), "selected_tags.csv"))
                if row.get("category") not in {"0", "4", "9"}:
                    raise TaggerResourceFinalizationError("selected_tags.csv category is invalid")
    except (OSError, UnicodeError, csv.Error) as exc:
        raise TaggerResourceFinalizationError("selected_tags.csv is unreadable") from exc
    if len(names) != spec.tag_count or len(set(names)) != len(names):
        raise TaggerResourceFinalizationError("selected_tags.csv tag count or uniqueness is invalid")


def _validate_raw_inputs(package_root: Path, spec: TaggerSpec) -> None:
    for name in spec.raw_files:
        path = package_root / name
        if not path.is_file() or path.stat().st_size < 1:
            raise TaggerResourceFinalizationError(f"required manual file is missing or empty: {name}")
    if spec.key == "cl":
        _validate_cl_inputs(package_root, spec)
    else:
        _validate_wd_inputs(package_root, spec)


def _record(path: Path) -> dict[str, object]:
    return {"sizeBytes": path.stat().st_size, "sha256": sha256_file(path)}


def _manifest(package_root: Path, spec: TaggerSpec) -> dict[str, object]:
    records = {name: _record(package_root / name) for name in spec.runtime_files}
    return {
        "schemaVersion": 2,
        "kind": "tagging-model",
        "resourceId": spec.resource_id,
        "resourceVersion": spec.resource_version,
        "profile": "danbooru",
        "displayName": spec.display_name,
        "description": spec.description,
        "runtimeFormat": spec.runtime_format,
        "distribution": {
            "mode": "local-only",
            "sourceUrl": spec.source_url,
            "licenseUrl": spec.license_url,
        },
        "entrypoints": spec.entrypoints,
        "files": records,
        "metadata": {
            "tagCount": spec.tag_count,
            "modelCategories": list(spec.model_categories),
            "adjustableCategories": list(spec.adjustable_categories),
            "excludedCategories": list(spec.excluded_categories),
            "vocabularyFingerprint": records[spec.vocabulary_file]["sha256"],
        },
        "documentation": [],
    }


def _atomic_write(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as target:
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
        if path.exists():
            raise TaggerResourceFinalizationError(f"refusing to overwrite existing file: {path.name}")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _require_entries(package_root: Path, expected: set[str]) -> None:
    try:
        entries = {path.name for path in package_root.iterdir()}
    except OSError as exc:
        raise TaggerResourceFinalizationError(f"resource directory cannot be read: {package_root}") from exc
    if entries != expected or any(not (package_root / name).is_file() for name in expected):
        missing = ", ".join(sorted(expected - entries)) or "none"
        unexpected = ", ".join(sorted(entries - expected)) or "none"
        raise TaggerResourceFinalizationError(
            f"resource directory file set is invalid; missing: {missing}; unexpected: {unexpected}"
        )


def _validate_generated_files(package_root: Path, spec: TaggerSpec) -> None:
    for name, value in spec.generated_json.items():
        if (package_root / name).read_bytes() != _json_bytes(value):
            raise TaggerResourceFinalizationError(f"generated resource file is not canonical: {name}")


def _load_finalized_package(library_root: Path, package_root: Path, spec: TaggerSpec) -> ResourcePackage:
    _validate_raw_inputs(package_root, spec)
    _validate_generated_files(package_root, spec)
    manifest_path = package_root / "resource.json"
    manifest = _read_json(manifest_path, "resource.json")
    expected = _manifest(package_root, spec)
    if manifest != expected:
        raise TaggerResourceFinalizationError("existing resource.json does not match the fixed tagger specification")
    package = ResourcePackage.load(library_root, manifest_path, "tagging-model")
    package.verify_files(verify_hashes=True)
    return package


def _result(package: ResourcePackage, spec: TaggerSpec, status: str) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "status": status,
        "resourceId": package.resource_id,
        "resourceVersion": package.resource_version,
        "resourceManifest": str((package.package_root / "resource.json").resolve()),
        "resourceFingerprint": package.fingerprint,
        "vocabularyFingerprint": package.metadata["vocabularyFingerprint"],
        "tagCount": spec.tag_count,
    }


def _finalize_spec(library_root: Path, spec: TaggerSpec) -> dict[str, object]:
    root = library_root.resolve(strict=True)
    package_root = root / "tagging-models" / spec.resource_id
    if not package_root.is_dir():
        raise TaggerResourceFinalizationError(
            f"manual resource directory is missing: {package_root}"
        )

    entries = {path.name for path in package_root.iterdir()}
    finalized = set(spec.runtime_files) | {"resource.json"}
    if "resource.json" in entries:
        _require_entries(package_root, finalized)
        return _result(_load_finalized_package(root, package_root, spec), spec, "already_valid")

    _require_entries(package_root, set(spec.raw_files))
    _validate_raw_inputs(package_root, spec)
    created: list[Path] = []
    try:
        for name, value in sorted(spec.generated_json.items()):
            path = package_root / name
            _atomic_write(path, _json_bytes(value))
            created.append(path)
        manifest_path = package_root / "resource.json"
        _atomic_write(manifest_path, _json_bytes(_manifest(package_root, spec)))
        created.append(manifest_path)
        package = _load_finalized_package(root, package_root, spec)
        return _result(package, spec, "created")
    except Exception:
        for path in reversed(created):
            try:
                path.unlink(missing_ok=True)
            except OSError as cleanup_error:
                raise TaggerResourceFinalizationError(
                    f"finalization failed and generated file cleanup failed: {path.name}"
                ) from cleanup_error
        raise


def finalize_tagger_resource(library_root: Path, tagger: str) -> dict[str, object]:
    try:
        spec = SPECS[tagger]
    except KeyError as exc:
        raise TaggerResourceFinalizationError(f"unsupported Danbooru tagger: {tagger}") from exc
    return _finalize_spec(library_root, spec)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tagger", choices=sorted(SPECS), required=True)
    arguments = parser.parse_args()
    result = finalize_tagger_resource(REPOSITORY_ROOT / "resource-library", arguments.tagger)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
