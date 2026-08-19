from __future__ import annotations

import hashlib
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from .classify_resource import ClassifyResourceError, load_classify_resource_from_install
from .contracts import canonical_json
from .overlay import OverlayLayout
from .path_safety import PathSafetyError, assert_no_reparse_tree, canonicalize, ensure_within, sha256_file
from .resource_catalog import ResourceCatalogError, ResourcePackage


class CustomClassificationResourceError(ValueError):
    pass


@dataclass(frozen=True)
class CustomClassificationResource:
    manifest_path: Path
    package: ResourcePackage
    content_sha256: str

    @property
    def resource_root(self) -> Path:
        return self.package.package_root


def _content_digest(package: ResourcePackage) -> str:
    records = []
    for path in sorted(package.package_root.rglob("*"), key=lambda item: str(item).casefold()):
        if path.is_file():
            records.append({
                "path": str(path.relative_to(package.package_root)).replace("/", "\\"),
                "sizeBytes": path.stat().st_size,
                "sha256": sha256_file(path),
            })
    return hashlib.sha256(canonical_json(records).encode("utf-8")).hexdigest()


def inspect_custom_classification_resource(path: str | Path) -> CustomClassificationResource:
    try:
        manifest_path = canonicalize(path, must_exist=True, directory=False).value
        if manifest_path.name.casefold() != "resource.json":
            raise CustomClassificationResourceError("custom classification path must select resource.json")
        package_root = canonicalize(manifest_path.parent, must_exist=True, directory=True).value
        assert_no_reparse_tree(package_root)
        install_root = canonicalize(package_root.parent, must_exist=True, directory=True).value
        package = ResourcePackage.load(
            install_root,
            manifest_path,
            "classification-index",
            allow_external_package=True,
        )
        package.verify_files(verify_hashes=True)
        relative_manifest = str(manifest_path.relative_to(install_root)).replace("/", "\\")
        load_classify_resource_from_install(
            install_root,
            relative_manifest,
            package.fingerprint,
            verify_hashes=True,
            allow_external_package=True,
        )
        return CustomClassificationResource(manifest_path, package, _content_digest(package))
    except CustomClassificationResourceError:
        raise
    except (ClassifyResourceError, OSError, PathSafetyError, ResourceCatalogError, ValueError) as exc:
        raise CustomClassificationResourceError(str(exc)) from exc


def freeze_custom_classification_resource(
    layout: OverlayLayout,
    resource: CustomClassificationResource,
) -> tuple[str, ResourcePackage]:
    overlay_resources = canonicalize(layout.root / "resources", must_exist=True, directory=True).value
    resource_root = layout.resource_path("classification-indexes")
    resource_root.mkdir(parents=True, exist_ok=True)
    destination = ensure_within(
        resource_root,
        resource_root / f"custom-{resource.package.fingerprint}",
    )
    staging = ensure_within(
        resource_root,
        resource_root / f".custom-{resource.package.fingerprint}.staging-{uuid.uuid4().hex}",
    )
    if destination.exists() or staging.exists():
        raise CustomClassificationResourceError("custom classification freeze destination already exists")
    try:
        staging.mkdir()
        for source in sorted(resource.resource_root.rglob("*"), key=lambda item: str(item).casefold()):
            relative = source.relative_to(resource.resource_root)
            target = ensure_within(staging, staging / relative)
            if source.is_dir():
                target.mkdir(exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.open("rb") as reader, target.open("xb") as writer:
                shutil.copyfileobj(reader, writer, length=1024 * 1024)
                writer.flush()
                os.fsync(writer.fileno())
        os.replace(staging, destination)
        manifest_relative_path = (
            f"classification-indexes\\custom-{resource.package.fingerprint}\\resource.json"
        )
        frozen_manifest = layout.resource_path(manifest_relative_path)
        frozen = ResourcePackage.load(overlay_resources, frozen_manifest, "classification-index")
        if frozen.fingerprint != resource.package.fingerprint:
            raise CustomClassificationResourceError("frozen classification resource fingerprint changed")
        frozen.verify_files(verify_hashes=True)
        load_classify_resource_from_install(
            overlay_resources,
            manifest_relative_path,
            frozen.fingerprint,
            verify_hashes=True,
        )
        return manifest_relative_path, frozen
    except Exception as exc:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        if destination.exists():
            shutil.rmtree(destination, ignore_errors=True)
        if isinstance(exc, CustomClassificationResourceError):
            raise
        raise CustomClassificationResourceError(str(exc)) from exc
