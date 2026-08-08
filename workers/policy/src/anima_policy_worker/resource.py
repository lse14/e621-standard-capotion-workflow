from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path, PureWindowsPath


RESOURCE_FILES = frozenset({
    "fusion/5kdataset.safetensors",
    "jtp3/jtp-3-hydra.safetensors",
    "waifu/model.safetensors",
    "clip/ViT-L-14.pt",
})
SHA256 = re.compile(r"^[0-9a-f]{64}$")


class PolicyResourceError(ValueError):
    pass


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _safe_relative(value: object) -> str:
    if not isinstance(value, str):
        raise PolicyResourceError("resource path must be a string")
    path = PureWindowsPath(value.replace("/", "\\"))
    if path.is_absolute() or path.drive or any(part in {"", ".", ".."} for part in path.parts):
        raise PolicyResourceError("resource path is unsafe")
    return str(path)


def load_policy_resource(
    install_root: Path,
    manifest_relative_path: str,
    expected_fingerprint: str,
) -> tuple[dict[str, object], dict[str, Path]]:
    relative = _safe_relative(manifest_relative_path)
    install = install_root.resolve(strict=True)
    manifest_path = (install / Path(relative)).resolve(strict=True)
    if os.path.commonpath((str(install), str(manifest_path))) != str(install):
        raise PolicyResourceError("resource manifest escaped the install root")
    data = manifest_path.read_bytes()
    if len(data) > 1_048_576:
        raise PolicyResourceError("resource manifest exceeds 1 MiB")
    try:
        manifest = json.loads(data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PolicyResourceError("resource manifest is invalid JSON") from exc
    catalog_manifest = isinstance(manifest, dict) and manifest.get("kind") == "dropout-model"
    if catalog_manifest:
        required = {
            "schemaVersion", "kind", "resourceId", "resourceVersion", "profile", "displayName",
            "description", "runtimeFormat", "entrypoints", "files", "metadata", "documentation",
        }
        if set(manifest) != required or (
            manifest.get("schemaVersion") != 1 or manifest.get("profile") != "e621"
            or manifest.get("runtimeFormat") != "lse14-scorer-5k-v1" or manifest.get("metadata") != {}
        ):
            raise PolicyResourceError("catalog policy resource identity is invalid")
        resource_id, resource_version = manifest.get("resourceId"), manifest.get("resourceVersion")
        if (
            not isinstance(resource_id, str) or re.fullmatch(r"[a-z0-9][a-z0-9-]{0,127}", resource_id) is None
            or not isinstance(resource_version, str) or not resource_version
        ):
            raise PolicyResourceError("catalog policy resource id or version is invalid")
        entrypoints, raw_files = manifest.get("entrypoints"), manifest.get("files")
        roles = {"clip", "fusion", "jtp3", "waifu"}
        if not isinstance(entrypoints, dict) or set(entrypoints) != roles or not isinstance(raw_files, dict) or set(entrypoints.values()) != set(raw_files):
            raise PolicyResourceError("catalog policy resource entrypoints are invalid")
        normalized_entrypoints = {role: _safe_relative(entrypoints[role]) for role in sorted(entrypoints)}
        normalized_files = {_safe_relative(name): record for name, record in raw_files.items()}
        unsigned = {
            "schemaVersion": 1, "kind": "dropout-model", "resourceId": resource_id,
            "resourceVersion": resource_version, "profile": "e621", "runtimeFormat": "lse14-scorer-5k-v1",
            "entrypoints": normalized_entrypoints,
            "files": {name: normalized_files[name] for name in sorted(normalized_files)}, "metadata": {},
        }
        fingerprint = hashlib.sha256(_canonical_json(unsigned).encode("utf-8")).hexdigest()
        if fingerprint != expected_fingerprint:
            raise PolicyResourceError("catalog policy resource fingerprint does not match the frozen job")
        root = manifest_path.parent
        logical_files = {
            "clip/ViT-L-14.pt": normalized_entrypoints["clip"],
            "fusion/5kdataset.safetensors": normalized_entrypoints["fusion"],
            "jtp3/jtp-3-hydra.safetensors": normalized_entrypoints["jtp3"],
            "waifu/model.safetensors": normalized_entrypoints["waifu"],
        }
        manifest = {**manifest, "fingerprint": fingerprint}
        raw_files = normalized_files
    else:
        required = {"schemaVersion", "resourceId", "owner", "resourceVersion", "rootRelativePath", "files", "fingerprint"}
        if not isinstance(manifest, dict) or set(manifest) != required:
            raise PolicyResourceError("resource manifest fields are invalid")
        if manifest["schemaVersion"] != 1 or manifest["resourceId"] != "lse14-scorer-5k-v1" or manifest["owner"] != "policy":
            raise PolicyResourceError("resource manifest identity is invalid")
        fingerprint = manifest["fingerprint"]
        if not isinstance(fingerprint, str) or not SHA256.fullmatch(fingerprint) or fingerprint != expected_fingerprint:
            raise PolicyResourceError("resource fingerprint does not match the frozen job")
        unsigned = {key: value for key, value in manifest.items() if key != "fingerprint"}
        if hashlib.sha256(_canonical_json(unsigned).encode("utf-8")).hexdigest() != fingerprint:
            raise PolicyResourceError("resource manifest fingerprint is invalid")
        raw_files = manifest["files"]
        if not isinstance(raw_files, dict) or set(raw_files) != RESOURCE_FILES:
            raise PolicyResourceError("resource manifest file set is invalid")
        root = (install / Path(_safe_relative(manifest["rootRelativePath"]))).resolve(strict=True)
        logical_files = {name: _safe_relative(name) for name in RESOURCE_FILES}
    if os.path.commonpath((str(install), str(root))) != str(install):
        raise PolicyResourceError("resource root escaped the install root")
    result: dict[str, Path] = {}
    for name, relative_name in logical_files.items():
        normalized = _safe_relative(relative_name)
        raw = raw_files[normalized]
        if not isinstance(raw, dict) or set(raw) != {"sizeBytes", "sha256"}:
            raise PolicyResourceError("resource file record is invalid")
        target = (root / Path(normalized)).resolve(strict=True)
        if os.path.commonpath((str(root), str(target))) != str(root):
            raise PolicyResourceError("resource file escaped its root")
        if type(raw["sizeBytes"]) is not int or target.stat().st_size != raw["sizeBytes"]:
            raise PolicyResourceError(f"resource file size mismatch: {name}")
        if not isinstance(raw["sha256"], str) or _digest(target) != raw["sha256"]:
            raise PolicyResourceError(f"resource file digest mismatch: {name}")
        result[name] = target
    return manifest, result
