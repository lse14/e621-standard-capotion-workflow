from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "core" / "src"))
sys.path.insert(0, str(ROOT / "workers" / "caption" / "src"))

from anima_caption_worker.model import (
    EXPECTED_MEAN,
    EXPECTED_STD,
    CaptionMetadataError,
    CaptionModel,
    CaptionSessionError,
    _default_session_factory,
    load_metadata,
)
from anima_caption_worker.resource import CaptionResourceError, load_caption_resource
from anima_caption_worker.worker import CaptionWorker, CaptionWorkerInitializationError
from anima_core.resource_manifest import ResourceManifestError, load_caption_resource_from_install


MANIFEST_RELATIVE = "manifests\\resources\\caption-e621.json"
RESOURCE_RELATIVE = "resources\\e621\\caption\\eva02_large_E621_FULL_V1"
CATEGORIES = ("general", "character", "species", "rating")
THRESHOLDS = {"general": 0.6, "character": 0.65, "species": 0.6, "rating": 0.65}
PREPROCESS = {
    "test": [
        {"type": "PadToSize", "size": [512, 512], "background_color": "white"},
        {"type": "Resize", "size": [448, 448], "interpolation": "bicubic"},
        {"type": "CenterCrop", "size": [448, 448]},
        {"type": "ToTensor"},
        {"type": "Normalize", "mean": list(EXPECTED_MEAN), "std": list(EXPECTED_STD)},
    ]
}


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _tags(count: int = 8_783, *, category_override: str | None = None) -> dict[str, object]:
    categories = {str(index): CATEGORIES[index % len(CATEGORIES)] for index in range(count)}
    if category_override is not None and categories:
        categories["0"] = category_override
    return {
        "tag_names": [f"tag_{index}" for index in range(count)],
        "idx_to_category": categories,
    }


def _json_bytes(value: object) -> bytes:
    return (_canonical(value) + "\n").encode("utf-8")


def _build_install(
    root: Path,
    *,
    tags: object | None = None,
    thresholds: object | None = None,
    preprocess: object | None = None,
) -> tuple[str, dict[str, Path]]:
    resource_root = root / Path(RESOURCE_RELATIVE.replace("\\", os.sep))
    resource_root.mkdir(parents=True)
    contents = {
        "model.onnx": b"pinned-onnx-model",
        "model.onnx.data": b"pinned-external-data",
        "tags.json": _json_bytes(_tags() if tags is None else tags),
        "thresholds.json": _json_bytes(THRESHOLDS if thresholds is None else thresholds),
        "preprocess.json": _json_bytes(PREPROCESS if preprocess is None else preprocess),
    }
    paths: dict[str, Path] = {}
    records: dict[str, dict[str, object]] = {}
    for name, data in contents.items():
        path = resource_root / name
        path.write_bytes(data)
        paths[name] = path
        records[name] = {"sizeBytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    unsigned = {
        "schemaVersion": 1,
        "resourceId": "caption-e621-eva02-large-full-v1",
        "owner": "caption",
        "profile": "e621",
        "resourceVersion": "test/eva02_large_E621_FULL_V1",
        "rootRelativePath": RESOURCE_RELATIVE,
        "tagCount": 8_783,
        "categories": list(CATEGORIES),
        "files": records,
    }
    fingerprint = hashlib.sha256(_canonical(unsigned).encode("utf-8")).hexdigest()
    manifest_path = root / Path(MANIFEST_RELATIVE.replace("\\", os.sep))
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(_canonical({**unsigned, "fingerprint": fingerprint}) + "\n", encoding="utf-8")
    return fingerprint, paths


def _build_v2_install(root: Path, runtime_format: str) -> tuple[str, str, dict[str, Path]]:
    if runtime_format == "cl-tagger-v2-onnx-v1":
        resource_id = "caption-danbooru-cl-tagger-v2-00"
        entrypoints = {
            "model": "model.onnx",
            "modelData": "model.onnx.data",
            "metadata": "model_metadata.json",
            "vocabulary": "model_vocabulary.json",
            "thresholds": "thresholds.json",
        }
        contents = {
            "model.onnx": b"cl-model",
            "model.onnx.data": b"cl-model-data",
            "model_metadata.json": b'{"image_size":384}',
            "model_vocabulary.json": b'{"fixture":"vocabulary"}',
            "thresholds.json": b'{"general":0.55,"character":0.55,"copyright":0.55}',
        }
        model_categories = ["General", "Character", "Copyright", "Meta", "Rating", "Quality"]
        adjustable = ["general", "character", "copyright"]
        excluded = ["meta", "rating", "quality"]
        vocabulary_role = "vocabulary"
        source_url = "https://huggingface.co/cella110n/cl_tagger_v2/tree/main/v2_00"
        license_url = "https://huggingface.co/cella110n/cl_tagger_v2/blob/main/LICENSE.md"
        tag_count = 106_536
    else:
        resource_id = "caption-danbooru-wd-eva02-large-v3"
        entrypoints = {
            "model": "model.onnx",
            "selectedTags": "selected_tags.csv",
            "preprocess": "preprocess.json",
            "thresholds": "thresholds.json",
        }
        contents = {
            "model.onnx": b"wd-model",
            "selected_tags.csv": b"name,category\nsolo,0\n",
            "preprocess.json": b'{"fixture":"preprocess"}',
            "thresholds.json": b'{"general":0.5296,"character":0.5296}',
        }
        model_categories = ["general", "character", "rating"]
        adjustable = ["general", "character"]
        excluded = ["rating"]
        vocabulary_role = "selectedTags"
        source_url = "https://huggingface.co/SmilingWolf/wd-eva02-large-tagger-v3"
        license_url = "https://huggingface.co/SmilingWolf/wd-eva02-large-tagger-v3/blob/main/LICENSE"
        tag_count = 10_861
    package = root / "tagging-models" / resource_id
    package.mkdir(parents=True)
    records: dict[str, dict[str, object]] = {}
    paths: dict[str, Path] = {}
    for name, content in contents.items():
        path = package / name
        path.write_bytes(content)
        paths[name] = path
        records[name] = {"sizeBytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
    metadata = {
        "tagCount": tag_count,
        "modelCategories": model_categories,
        "adjustableCategories": adjustable,
        "excludedCategories": excluded,
        "vocabularyFingerprint": records[entrypoints[vocabulary_role]]["sha256"],
    }
    distribution = {"mode": "local-only", "sourceUrl": source_url, "licenseUrl": license_url}
    unsigned = {
        "schemaVersion": 2,
        "kind": "tagging-model",
        "resourceId": resource_id,
        "resourceVersion": "test-v2",
        "profile": "danbooru",
        "runtimeFormat": runtime_format,
        "entrypoints": entrypoints,
        "files": records,
        "metadata": metadata,
        "distribution": distribution,
    }
    fingerprint = hashlib.sha256(_canonical(unsigned).encode("utf-8")).hexdigest()
    manifest = {
        **unsigned,
        "displayName": {"zh-CN": "Test", "en": "Test"},
        "description": {"zh-CN": "Test", "en": "Test"},
        "documentation": [],
    }
    (package / "resource.json").write_text(_canonical(manifest), encoding="utf-8")
    relative = str((package / "resource.json").relative_to(root)).replace("/", "\\")
    return relative, fingerprint, paths


def _hello(root: Path, fingerprint: str) -> dict[str, object]:
    dataset = root / "dataset"
    dataset.mkdir(exist_ok=True)
    return {
        "schemaVersion": 1,
        "payloadType": "caption_hello_request",
        "jobId": "job-1",
        "configHash": "a" * 64,
        "profile": "e621",
        "datasetRoot": str(dataset),
        "resourceManifestRelativePath": MANIFEST_RELATIVE,
        "resourceFingerprint": fingerprint,
        "thresholdPolicy": {"mode": "model_default"},
        "captionFormat": {
            "replaceUnderscoresWithSpaces": True,
            "preserveEscapes": True,
            "triggersEnabled": False,
            "triggerTerms": [],
        },
        "imageDecode": {
            "extensions": [".jpg", ".jpeg", ".png", ".webp", ".bmp"],
            "rejectMultiFrame": True,
            "applyExifTranspose": True,
            "alphaBackground": "#FFFFFF",
        },
    }


class _FakeInput:
    def __init__(self, shape: list[object] | None = None) -> None:
        self.name = "images"
        self.shape = [1, 3, 448, 448] if shape is None else shape
        self.type = "tensor(float)"


class _FakeSession:
    def __init__(self, provider: str = "CPUExecutionProvider", shape: list[object] | None = None) -> None:
        self._provider = provider
        self._input = _FakeInput(shape)

    def get_inputs(self) -> list[_FakeInput]:
        return [self._input]

    def get_providers(self) -> list[str]:
        return [self._provider]


class CaptionResourceTests(unittest.TestCase):
    def test_v2_cl_and_wd_manifests_expose_profile_runtime_and_entrypoints(self) -> None:
        for runtime_format in (
            "cl-tagger-v2-onnx-v1",
            "wd-eva02-large-tagger-v3-onnx-v1",
        ):
            with self.subTest(runtime_format=runtime_format), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                relative, fingerprint, paths = _build_v2_install(root, runtime_format)
                resource = load_caption_resource(root, relative, fingerprint, verify_external_data_hash=True)
                self.assertEqual(2, resource.schema_version)
                self.assertEqual("danbooru", resource.profile)
                self.assertEqual(runtime_format, resource.runtime_format)
                self.assertEqual(fingerprint, resource.fingerprint)
                self.assertEqual(set(paths), set(resource.files))
                self.assertEqual(set(resource.entrypoints), set(
                    {"model", "modelData", "metadata", "vocabulary", "thresholds"}
                    if runtime_format == "cl-tagger-v2-onnx-v1"
                    else {"model", "selectedTags", "preprocess", "thresholds"}
                ))

    def test_v2_vocabulary_fingerprint_and_hello_profile_are_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            relative, fingerprint, _ = _build_v2_install(root, "cl-tagger-v2-onnx-v1")
            manifest_path = root / Path(relative.replace("\\", os.sep))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["metadata"]["vocabularyFingerprint"] = "f" * 64
            unsigned = {
                key: manifest[key]
                for key in (
                    "schemaVersion", "kind", "resourceId", "resourceVersion", "profile",
                    "runtimeFormat", "entrypoints", "files", "metadata", "distribution",
                )
            }
            changed_fingerprint = hashlib.sha256(_canonical(unsigned).encode("utf-8")).hexdigest()
            manifest_path.write_text(_canonical(manifest), encoding="utf-8")
            with self.assertRaisesRegex(CaptionResourceError, "vocabulary fingerprint"):
                load_caption_resource(root, relative, changed_fingerprint)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            relative, fingerprint, _ = _build_v2_install(root, "cl-tagger-v2-onnx-v1")
            hello = _hello(root, fingerprint)
            hello["resourceManifestRelativePath"] = relative
            with self.assertRaises(CaptionWorkerInitializationError) as caught:
                CaptionWorker().initialize(hello, install_root=root, session_factory=lambda _: _FakeSession())
            self.assertEqual("caption_profile_mismatch", caught.exception.code)

    def test_resource_manifest_and_all_five_hashes_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fingerprint, _ = _build_install(root)
            manifest, core_paths = load_caption_resource_from_install(root, MANIFEST_RELATIVE, fingerprint)
            worker_resource = load_caption_resource(
                root,
                MANIFEST_RELATIVE,
                fingerprint,
                verify_external_data_hash=True,
            )
            self.assertEqual(fingerprint, manifest.fingerprint)
            self.assertEqual(fingerprint, worker_resource.fingerprint)
            self.assertEqual(set(core_paths), set(worker_resource.files))
            self.assertEqual(5, len(core_paths))

    def test_manifest_and_file_tampering_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fingerprint, paths = _build_install(root)
            paths["thresholds.json"].write_bytes(paths["thresholds.json"].read_bytes() + b" ")
            with self.assertRaises(ResourceManifestError):
                load_caption_resource_from_install(root, MANIFEST_RELATIVE, fingerprint)
            with self.assertRaises(CaptionResourceError):
                load_caption_resource(root, MANIFEST_RELATIVE, fingerprint)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fingerprint, _ = _build_install(root)
            manifest_path = root / Path(MANIFEST_RELATIVE.replace("\\", os.sep))
            value = json.loads(manifest_path.read_text(encoding="utf-8"))
            value["resourceVersion"] = "tampered"
            manifest_path.write_text(_canonical(value), encoding="utf-8")
            with self.assertRaises(ResourceManifestError):
                load_caption_resource_from_install(root, MANIFEST_RELATIVE, fingerprint)
            with self.assertRaises(CaptionResourceError):
                load_caption_resource(root, MANIFEST_RELATIVE, fingerprint)

    def test_manifest_and_resource_root_require_exactly_five_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fingerprint, paths = _build_install(root)
            paths["model.onnx"].parent.joinpath("extra.txt").write_text("extra", encoding="ascii")
            with self.assertRaises(ResourceManifestError):
                load_caption_resource_from_install(root, MANIFEST_RELATIVE, fingerprint)
            with self.assertRaises(CaptionResourceError):
                load_caption_resource(root, MANIFEST_RELATIVE, fingerprint)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, _ = _build_install(root)
            manifest_path = root / Path(MANIFEST_RELATIVE.replace("\\", os.sep))
            value = json.loads(manifest_path.read_text(encoding="utf-8"))
            value["files"].pop("preprocess.json")
            unsigned = {key: item for key, item in value.items() if key != "fingerprint"}
            fingerprint = hashlib.sha256(_canonical(unsigned).encode("utf-8")).hexdigest()
            manifest_path.write_text(_canonical({**unsigned, "fingerprint": fingerprint}), encoding="utf-8")
            with self.assertRaises(ResourceManifestError):
                load_caption_resource_from_install(root, MANIFEST_RELATIVE, fingerprint)
            with self.assertRaises(CaptionResourceError):
                load_caption_resource(root, MANIFEST_RELATIVE, fingerprint)

    def test_resource_manifest_path_escape_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fingerprint, _ = _build_install(root)
            with self.assertRaises(ResourceManifestError):
                load_caption_resource_from_install(root, "..\\caption.json", fingerprint)
            with self.assertRaises(CaptionResourceError):
                load_caption_resource(root, "..\\caption.json", fingerprint)

    def test_resource_manifest_reparse_path_is_rejected_when_supported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fingerprint, _ = _build_install(root)
            link = root / "linked-manifests"
            target = root / "manifests"
            try:
                os.symlink(target, link, target_is_directory=True)
            except (NotImplementedError, OSError):
                completed = subprocess.run(
                    ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                if completed.returncode != 0:
                    self.skipTest("current Windows account cannot create a symlink or junction")
            relative = "linked-manifests\\resources\\caption-e621.json"
            with self.assertRaises(ResourceManifestError):
                load_caption_resource_from_install(root, relative, fingerprint)
            with self.assertRaises(CaptionResourceError):
                load_caption_resource(root, relative, fingerprint)

    def test_metadata_count_categories_thresholds_and_preprocess_are_strict(self) -> None:
        cases = (
            {"tags": _tags(8_782)},
            {"tags": _tags(category_override="unknown")},
            {"thresholds": {"general": 0.6}},
            {"thresholds": {**THRESHOLDS, "general": 1.01}},
            {"preprocess": {"test": PREPROCESS["test"][:-1]}},
        )
        for index, arguments in enumerate(cases):
            with self.subTest(case=index), tempfile.TemporaryDirectory() as temporary:
                _, paths = _build_install(Path(temporary), **arguments)
                with self.assertRaises(CaptionMetadataError):
                    load_metadata(paths)

    def test_worker_creates_one_session_and_rejects_second_initialization(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fingerprint, _ = _build_install(root)
            session_loads = 0

            def factory(model_path: Path) -> _FakeSession:
                nonlocal session_loads
                self.assertEqual("model.onnx", model_path.name)
                session_loads += 1
                return _FakeSession()

            worker = CaptionWorker()
            result = worker.initialize(_hello(root, fingerprint), install_root=root, session_factory=factory)
            self.assertEqual(1, result["modelSessionLoads"])
            self.assertEqual(8_783, result["tagCount"])
            self.assertEqual("CPUExecutionProvider", result["provider"])
            with self.assertRaises(CaptionWorkerInitializationError) as caught:
                worker.initialize(_hello(root, fingerprint), install_root=root, session_factory=factory)
            self.assertEqual("caption_protocol_violation", caught.exception.code)
            self.assertEqual(1, session_loads)

    def test_worker_initialization_reports_metadata_and_session_codes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fingerprint, _ = _build_install(root, preprocess={"test": []})
            with self.assertRaises(CaptionWorkerInitializationError) as caught:
                CaptionWorker().initialize(
                    _hello(root, fingerprint),
                    install_root=root,
                    session_factory=lambda _: _FakeSession(),
                )
            self.assertEqual("caption_metadata_mismatch", caught.exception.code)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fingerprint, _ = _build_install(root)

            def fail_session(_: Path) -> _FakeSession:
                raise RuntimeError("injected session failure")

            with self.assertRaises(CaptionWorkerInitializationError) as caught:
                CaptionWorker().initialize(
                    _hello(root, fingerprint),
                    install_root=root,
                    session_factory=fail_session,
                )
            self.assertEqual("caption_model_load_failed", caught.exception.code)

    def test_cuda_session_failure_falls_back_to_required_cpu_provider(self) -> None:
        calls: list[tuple[str, ...]] = []

        def inference_session(_: str, *, providers: list[str]) -> _FakeSession:
            calls.append(tuple(providers))
            if providers[0] == "CUDAExecutionProvider":
                raise RuntimeError("injected CUDA load failure")
            return _FakeSession()

        fake_ort = SimpleNamespace(
            preload_dlls=lambda: print("injected preload diagnostic"),
            get_available_providers=lambda: ["CUDAExecutionProvider", "CPUExecutionProvider"],
            InferenceSession=inference_session,
        )
        with (
            mock.patch.dict(sys.modules, {"onnxruntime": fake_ort}),
            mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
            mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
        ):
            session = _default_session_factory(Path("model.onnx"))
        self.assertEqual("", stdout.getvalue())
        self.assertIn("injected preload diagnostic", stderr.getvalue())
        self.assertEqual("CPUExecutionProvider", session.get_providers()[0])
        self.assertEqual(
            [("CUDAExecutionProvider", "CPUExecutionProvider"), ("CPUExecutionProvider",)],
            calls,
        )

    def test_model_accepts_pinned_symbolic_batch_and_rejects_other_shapes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, paths = _build_install(Path(temporary))
            symbolic = CaptionModel(
                paths,
                session_factory=lambda _: _FakeSession(shape=["batch_size", 3, 448, 448]),
            )
            self.assertEqual("images", symbolic.input_name)
            for shape in ([None, 3, 448, 448], [2, 3, 448, 448], [1, 448, 448, 3]):
                with self.subTest(shape=shape), self.assertRaises(CaptionSessionError):
                    CaptionModel(paths, session_factory=lambda _, shape=shape: _FakeSession(shape=shape))


if __name__ == "__main__":
    unittest.main()
