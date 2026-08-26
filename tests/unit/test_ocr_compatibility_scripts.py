"""Safety contracts for the project-local OCR compatibility experiment."""
from __future__ import annotations

import base64
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
COMPATIBILITY_SCRIPT = ROOT / "packaging" / "scripts" / "Test-OcrCompatibility.ps1"
CLEANUP_SCRIPT = ROOT / "packaging" / "scripts" / "Clean-OcrImport.ps1"
DRIVER = ROOT / "packaging" / "scripts" / "ocr_compatibility.py"
RUNTIME_PROBE = ROOT / "packaging" / "scripts" / "ocr_runtime_probe.py"
TOOLCHAIN_PYTHON = ROOT / ".runtime-build" / "runtimes" / "core" / "python.exe"


def _ps_literal(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _powershell(command: str) -> subprocess.CompletedProcess[str]:
    command = "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false); " + command
    encoded = base64.b64encode(command.encode("utf-16le")).decode("ascii")
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _tree_state(root: Path) -> dict[str, tuple[int, int]]:
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in root.rglob("*")
        if path.is_file()
    }


class OcrCompatibilityScriptTests(unittest.TestCase):
    def test_scripts_parse_and_python_driver_compiles(self) -> None:
        for script in (COMPATIBILITY_SCRIPT, CLEANUP_SCRIPT):
            self.assertTrue(script.is_file(), f"missing production script: {script}")
            command = (
                "$tokens=$null; $errors=$null; "
                f"[System.Management.Automation.Language.Parser]::ParseFile({_ps_literal(script)},[ref]$tokens,[ref]$errors) | Out-Null; "
                "if ($errors.Count) { $errors | ForEach-Object { $_.Message }; exit 1 }"
            )
            completed = _powershell(command)
            self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)

        for python_file in (DRIVER, RUNTIME_PROBE):
            self.assertTrue(python_file.is_file(), f"missing production driver: {python_file}")
            completed = subprocess.run(
                [
                    str(TOOLCHAIN_PYTHON),
                    "-B",
                    "-I",
                    "-c",
                    "import pathlib,sys; p=pathlib.Path(sys.argv[1]); compile(p.read_text(encoding='utf-8'), str(p), 'exec')",
                    str(python_file),
                ],
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)

    def test_runtime_probe_uses_explicit_engines_and_provider_profiling(self) -> None:
        source = RUNTIME_PROBE.read_text(encoding="utf-8")

        self.assertIn('engine="paddle_static"', source)
        self.assertIn('engine="onnxruntime"', source)
        self.assertIn("enable_profiling = True", source)
        self.assertIn("end_profiling()", source)
        self.assertIn("CUDAExecutionProvider", source)
        self.assertIn("CPUExecutionProvider", source)

    def test_preview_freezes_candidates_models_and_writes_nothing(self) -> None:
        working_root = ROOT / ".runtime-build" / "ocr-import"
        before = _tree_state(working_root)
        command = (
            "$ErrorActionPreference='Stop'; "
            f"$record = & {_ps_literal(COMPATIBILITY_SCRIPT)} -ProjectRoot {_ps_literal(ROOT)} 6>$null; "
            "$record | ConvertTo-Json -Depth 8 -Compress"
        )
        completed = _powershell(command)

        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        record = json.loads(completed.stdout)
        self.assertEqual("TestOcrCompatibility", record["Action"])
        self.assertEqual("Preview", record["Mode"])
        self.assertEqual(str(TOOLCHAIN_PYTHON), record["ProjectPython"])
        self.assertEqual(str(working_root), record["WorkingRoot"])
        self.assertEqual(str(working_root / "environment"), record["Environment"])
        self.assertEqual(
            str(working_root / "conversion-cache" / "converter-environment"),
            record["ConverterEnvironment"],
        )
        self.assertEqual(str(working_root / "downloads"), record["Downloads"])
        self.assertEqual(str(working_root / "conversion-cache"), record["ConversionCache"])
        self.assertEqual(str(working_root / "evidence"), record["Evidence"])
        self.assertFalse(record["Resume"])
        self.assertEqual(
            {
                "paddleocr": "3.7.0",
                "paddlex[ocr-core]": "3.7.2",
                "onnxruntime-gpu": "1.26.0",
            },
            record["CandidatePackages"],
        )
        self.assertEqual(
            {
                "Package": "paddle2onnx",
                "Version": "2.1.0",
                "WheelSha256": "478993e17ed0212b79a4d6e2d8d0582ebb19c7230b7f365d51222833e98581b3",
                "SourceCommit": "c8b5048c3a0903986bd3ec1cce2af9915b391c49",
            },
            record["Converter"],
        )
        self.assertEqual(
            ["official-build-nightly", "local-v2.1.0-source-build"],
            [path["Name"] for path in record["CompatibilityPaths"]],
        )
        self.assertEqual(
            {
                "Requirement": "paddlepaddle==3.0.0.dev20250426",
                "Url": (
                    "https://paddle-whl.bj.bcebos.com/nightly/cpu/paddlepaddle/"
                    "paddlepaddle-3.0.0.dev20250426-cp311-cp311-win_amd64.whl"
                ),
                "Size": 98376372,
                "Sha256": "f62aaab2bd8d3ad4f4f7781bdeed43403546057b7afcdc10a4b33847b2617f1f",
            },
            record["CompatibilityPaths"][0]["PaddleWheel"],
        )
        self.assertEqual(
            str(working_root / "evidence" / "compatibility-v2.json"),
            record["EvidenceFile"],
        )
        self.assertEqual(
            [
                (
                    "PP-OCRv5_server_det",
                    88340480,
                    "22a33e0ba6a21425ea4192da03bf4395c9a0c67902bd924b7328fc859073045d",
                ),
                (
                    "PP-OCRv5_server_rec",
                    84869120,
                    "d99be2ffd348943ab52876179168be4fb5b14f5f0812f2ae4c76d89ec2ea750a",
                ),
                (
                    "PP-LCNet_x1_0_textline_ori",
                    6871040,
                    "6171f69605215a85624d650e9079fa45f7c3eaf944296181bcc5395bf3ddc7f6",
                ),
            ],
            [
                (model["Name"], model["Size"], model["Sha256"])
                for model in record["Models"]
            ],
        )
        self.assertEqual(
            [
                (
                    "text_detection",
                    398527,
                    "3ac37804e4e292f68c8960d553485147516cdc2e4154afeec6ca742a70e71dca",
                ),
                (
                    "text_recognition",
                    73730,
                    "5362ba97741413494c507237b5096ef09ed575a501c4d9e68bfeffe17528a6ad",
                ),
                (
                    "textline_orientation",
                    3996,
                    "872200f57a1408e7aab2856d5f2c687b3a937805e0c4ff74bd7de21df1f742b9",
                ),
            ],
            [
                (sample["Purpose"], sample["Size"], sample["Sha256"])
                for sample in record["Samples"]
            ],
        )
        self.assertEqual(before, _tree_state(working_root))

    def test_resume_preview_is_explicit_and_writes_nothing(self) -> None:
        working_root = ROOT / ".runtime-build" / "ocr-import"
        before = _tree_state(working_root)
        command = (
            "$ErrorActionPreference='Stop'; "
            f"$record = & {_ps_literal(COMPATIBILITY_SCRIPT)} -ProjectRoot {_ps_literal(ROOT)} -Resume 6>$null; "
            "$record | ConvertTo-Json -Depth 8 -Compress"
        )

        completed = _powershell(command)

        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        record = json.loads(completed.stdout)
        self.assertEqual("Preview", record["Mode"])
        self.assertTrue(record["Resume"])
        self.assertEqual(before, _tree_state(working_root))

    def test_compatibility_script_uses_only_explicit_isolated_python_invocations(self) -> None:
        source = COMPATIBILITY_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("& $Executable -B -I @Arguments", source)
        self.assertNotIn("& python", source.lower())
        self.assertNotIn("& pip", source.lower())
        self.assertNotIn("& paddle2onnx", source.lower())


class OcrCompatibilityDriverTests(unittest.TestCase):
    @staticmethod
    def _load_driver():
        spec = importlib.util.spec_from_file_location("ocr_compatibility", DRIVER)
        if spec is None or spec.loader is None:
            raise AssertionError(f"cannot load driver: {DRIVER}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_converter_wheel_is_downloaded_alone_before_dependency_resolution(self) -> None:
        module = self._load_driver()

        self.assertEqual(
            [
                "-m",
                "pip",
                "download",
                "--no-deps",
                "--only-binary=:all:",
                "--dest",
                "wheelhouse",
                "paddle2onnx==2.1.0",
            ],
            module.converter_download_arguments(Path("wheelhouse")),
        )

    def test_converter_build_dependencies_are_bound_to_the_resume_contract(self) -> None:
        module = self._load_driver()
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            wheelhouse = Path(temporary)

            self.assertEqual(
                [
                    "-m",
                    "pip",
                    "download",
                    "--only-binary=:all:",
                    "--find-links",
                    str(wheelhouse),
                    "--dest",
                    str(wheelhouse),
                    "paddlepaddle==3.0.0.dev20250426",
                ],
                module.converter_build_dependency_download_arguments(wheelhouse),
            )
            self.assertIn(
                module.PADDLE_BUILD_REQUIREMENT,
                module.dependency_download_contract(wheelhouse)["requirements"],
            )

    def test_dependency_download_marker_only_skips_matching_completed_resolution(self) -> None:
        module = self._load_driver()
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            wheelhouse = Path(temporary)
            dependency = wheelhouse / "dependency-1-py3-none-any.whl"
            dependency.write_bytes(b"complete-wheel")

            self.assertTrue(module.dependency_download_required(wheelhouse))

            module.mark_dependency_download_complete(wheelhouse)
            marker = module.dependency_download_marker(wheelhouse)
            self.assertTrue(marker.is_file())
            self.assertFalse(module.dependency_download_required(wheelhouse))

            dependency.write_bytes(b"changed-wheel")
            self.assertTrue(module.dependency_download_required(wheelhouse))
            dependency.write_bytes(b"complete-wheel")
            self.assertFalse(module.dependency_download_required(wheelhouse))

            marker.write_text("{\"schemaVersion\": 1, \"requirements\": []}\n", encoding="utf-8")
            self.assertTrue(module.dependency_download_required(wheelhouse))

            marker.write_text("not-json\n", encoding="utf-8")
            self.assertTrue(module.dependency_download_required(wheelhouse))

    def test_local_wheel_seed_requires_exact_hash_and_copies_into_staging(self) -> None:
        module = self._load_driver()
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            project = Path(temporary)
            source = project / "packaging" / "wheelhouse" / "source.whl"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"verified-wheel")
            expected_hash = module.sha256_file(source)
            staging = project / ".runtime-build" / "ocr-import" / "downloads" / "wheels"
            staging.mkdir(parents=True)

            records = module.seed_verified_local_wheels(
                project,
                staging,
                ((Path("packaging/wheelhouse/source.whl"), expected_hash),),
            )

            self.assertEqual(b"verified-wheel", (staging / "source.whl").read_bytes())
            self.assertEqual(expected_hash, records[0]["sha256"])
            self.assertEqual(str(source), records[0]["source"])
            self.assertEqual(str(staging / "source.whl"), records[0]["staged"])

            (staging / "source.whl").unlink()
            with self.assertRaisesRegex(RuntimeError, "SHA-256 mismatch"):
                module.seed_verified_local_wheels(
                    project,
                    staging,
                    ((Path("packaging/wheelhouse/source.whl"), "0" * 64),),
                )
            self.assertFalse((staging / "source.whl").exists())

    def test_verified_artifact_download_checks_size_and_hash(self) -> None:
        module = self._load_driver()
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            root = Path(temporary)
            source = root / "source.whl"
            source.write_bytes(b"artifact-content")
            destination = root / "downloads" / "artifact.whl"

            record = module.download_verified_artifact(
                source.as_uri(),
                destination,
                source.stat().st_size,
                module.sha256_file(source),
            )

            self.assertEqual(source.read_bytes(), destination.read_bytes())
            self.assertEqual(source.stat().st_size, record["size"])
            self.assertEqual(module.sha256_file(source), record["sha256"])
            self.assertFalse(destination.with_suffix(".whl.part").exists())

    def test_verified_artifact_downloader_uses_direct_https_transport(self) -> None:
        module = self._load_driver()

        self.assertEqual(
            (
                "files.pythonhosted.org",
                443,
                "/packages/example.whl?download=1",
            ),
            module.direct_https_target(
                "https://files.pythonhosted.org/packages/example.whl?download=1"
            ),
        )
        source = module.download_verified_artifact.__code__.co_names
        self.assertIn("open_direct_https", source)
        self.assertNotIn("urlopen", source)

    def test_verified_artifact_requests_always_use_resumable_range(self) -> None:
        module = self._load_driver()

        self.assertEqual(
            {
                "User-Agent": "Anima-OCR-Compatibility/1",
                "Range": "bytes=0-",
            },
            module.download_request_headers(0),
        )
        self.assertEqual("bytes=1048576-", module.download_request_headers(1048576)["Range"])

    def test_verified_artifact_copy_reuses_only_exact_local_content(self) -> None:
        module = self._load_driver()
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            root = Path(temporary)
            source = root / "resolved" / "artifact.whl"
            destination = root / "probe" / "artifact.whl"
            source.parent.mkdir()
            source.write_bytes(b"verified-local-artifact")

            record = module.copy_verified_artifact(
                source,
                destination,
                source.stat().st_size,
                module.sha256_file(source),
            )

            self.assertEqual(source.read_bytes(), destination.read_bytes())
            self.assertEqual(str(source), record["source"])
            self.assertEqual(str(destination), record["destination"])
            self.assertEqual(module.sha256_file(source), record["sha256"])

    def test_python_subprocess_emits_heartbeat_while_waiting(self) -> None:
        module = self._load_driver()
        commands = []
        output = io.StringIO()
        with redirect_stdout(output):
            completed = module.run_python(
                TOOLCHAIN_PYTHON,
                ["-c", "import time; time.sleep(0.15); print('finished')"],
                environment=os.environ.copy(),
                commands=commands,
                heartbeat_seconds=0.05,
            )

        self.assertEqual(0, completed.returncode)
        self.assertIn("still running", output.getvalue())
        self.assertIn("finished", commands[0]["output"])

    def test_python_subprocess_uses_explicit_cwd(self) -> None:
        module = self._load_driver()
        commands = []
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            conversion_cache = Path(temporary) / "conversion-cache"
            conversion_cache.mkdir()
            completed = module.run_python(
                TOOLCHAIN_PYTHON,
                [
                    "-c",
                    (
                        "from pathlib import Path; import sys; "
                        "print('cwd-match' if Path.cwd().resolve() == "
                        "Path(sys.argv[1]).resolve() else 'cwd-mismatch')"
                    ),
                    str(conversion_cache),
                ],
                environment=os.environ.copy(),
                commands=commands,
                cwd=conversion_cache,
            )

            self.assertEqual("cwd-match", completed.stdout.strip())
        self.assertEqual(["-B", "-I"], commands[0]["command"][1:3])

    def test_only_fixed_python_package_hosts_bypass_proxy_without_duplicates(self) -> None:
        module = self._load_driver()
        self.assertEqual(
            ("pypi.org", "files.pythonhosted.org"),
            module.PIP_NO_PROXY_HOSTS,
        )
        self.assertEqual(
            "localhost,pypi.org,files.pythonhosted.org",
            module.extend_no_proxy(
                "localhost,pypi.org",
                module.PIP_NO_PROXY_HOSTS,
            ),
        )

    def test_paddle_3_3_probe_uses_separate_environment_and_fixed_converter(self) -> None:
        module = self._load_driver()
        probe_root = Path("conversion-cache/paddle-3.3.0-probe")
        resolved_wheels = Path("downloads/wheels")

        plan = module.paddle_probe_plan(probe_root, resolved_wheels)

        self.assertEqual(probe_root / "environment", plan["environment"])
        self.assertEqual(probe_root / "wheels", plan["wheels"])
        self.assertEqual(
            [
                "-m",
                "pip",
                "install",
                "--no-index",
                "--find-links",
                str(probe_root / "wheels"),
                "--find-links",
                str(resolved_wheels),
                "paddlepaddle==3.3.0",
                "paddle2onnx==2.1.0",
            ],
            plan["installArguments"],
        )
        self.assertEqual(
            {
                "version": "3.2.2",
                "compatible": False,
                "failure": "WinError 127: missing libpaddle procedures",
            },
            plan["priorIncompatibility"],
        )

    def test_converter_install_binds_its_unpublished_packaging_runtime_dependency(self) -> None:
        module = self._load_driver()
        wheelhouse = Path("downloads/wheels")

        self.assertIn(
            "packaging==26.2",
            module.converter_install_arguments(wheelhouse),
        )
        self.assertIn(
            "packaging==26.2",
            module.dependency_download_contract(wheelhouse)["requirements"],
        )

    def test_inference_and_converter_installs_are_separate_and_offline(self) -> None:
        module = self._load_driver()

        self.assertEqual(
            [
                "-m",
                "pip",
                "install",
                "--no-index",
                "--find-links",
                str(Path("downloads/wheels")),
                "paddleocr==3.7.0",
                "paddlex[ocr-core]==3.7.2",
                "onnxruntime-gpu==1.26.0",
                "paddlepaddle==3.2.2",
            ],
            module.inference_install_arguments(Path("downloads/wheels")),
        )
        self.assertEqual(
            [
                "-m",
                "pip",
                "install",
                "--no-index",
                "--find-links",
                str(Path("downloads/wheels")),
                "paddle2onnx==2.1.0",
                "paddlepaddle==3.0.0.dev20250426",
                "packaging==26.2",
            ],
            module.converter_install_arguments(Path("downloads/wheels")),
        )

    def test_model_conversion_arguments_are_ascii_relative_for_all_models(self) -> None:
        module = self._load_driver()
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            conversion_cache = Path(temporary) / "conversion-cache"
            for artifact in module.MODEL_ARTIFACTS:
                name = artifact["name"]
                arguments = module.model_conversion_arguments(
                    conversion_cache / "source-models" / name,
                    conversion_cache / "onnx-models" / name / "inference.onnx",
                    conversion_cache=conversion_cache,
                )

                model_dir = arguments[arguments.index("--model_dir") + 1]
                output_file = arguments[arguments.index("--save_file") + 1]
                self.assertEqual(f"source-models/{name}", model_dir)
                self.assertEqual(f"onnx-models/{name}/inference.onnx", output_file)
                self.assertFalse(Path(model_dir).is_absolute())
                self.assertFalse(Path(output_file).is_absolute())
                self.assertTrue(model_dir.isascii())
                self.assertTrue(output_file.isascii())

    def test_model_conversion_arguments_reject_paths_outside_conversion_cache(self) -> None:
        module = self._load_driver()
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            root = Path(temporary)
            conversion_cache = root / "conversion-cache"
            source = conversion_cache / "source-models" / "PP-OCRv5_server_det"
            output = conversion_cache / "onnx-models" / "PP-OCRv5_server_det" / "inference.onnx"
            outside = root / "outside"

            with self.assertRaisesRegex(RuntimeError, "conversion cache"):
                module.model_conversion_arguments(
                    outside,
                    output,
                    conversion_cache=conversion_cache,
                )
            with self.assertRaisesRegex(RuntimeError, "conversion cache"):
                module.model_conversion_arguments(
                    source,
                    outside / "inference.onnx",
                    conversion_cache=conversion_cache,
                )
            with self.assertRaisesRegex(RuntimeError, "ASCII"):
                module.model_conversion_arguments(
                    conversion_cache / "source-models" / "det-中文",
                    output,
                    conversion_cache=conversion_cache,
                )

    def test_new_command_ledger_does_not_copy_failed_prior_commands(self) -> None:
        module = self._load_driver()
        prior_evidence = {
            "commands": [
                {
                    "command": ["converter-python", "-B", "-I", "-m", "paddle2onnx.command"],
                    "exitCode": 1,
                    "output": "parse_error.101",
                }
            ]
        }

        commands = module.new_command_ledger(prior_evidence)

        self.assertEqual([], commands)
        self.assertIsNot(commands, prior_evidence["commands"])
        self.assertEqual(1, prior_evidence["commands"][0]["exitCode"])

    def test_runtime_probe_arguments_are_explicit_and_bounded(self) -> None:
        module = self._load_driver()
        probe = Path("packaging/scripts/ocr_runtime_probe.py")

        self.assertEqual(
            [
                str(probe),
                "parity",
                "--source-root",
                str(Path("source")),
                "--onnx-root",
                str(Path("onnx")),
                "--samples-root",
                str(Path("samples")),
            ],
            module.parity_probe_arguments(probe, Path("source"), Path("onnx"), Path("samples")),
        )
        self.assertEqual(
            [
                str(probe),
                "provider",
                "--onnx-root",
                str(Path("onnx")),
                "--provider",
                "CUDAExecutionProvider",
                "--profile-root",
                str(Path("profiles/cuda")),
            ],
            module.provider_probe_arguments(
                probe,
                Path("onnx"),
                "CUDAExecutionProvider",
                Path("profiles/cuda"),
            ),
        )

    def test_model_root_discovery_requires_one_complete_model(self) -> None:
        module = self._load_driver()
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            root = Path(temporary)
            model = root / "nested" / "model"
            model.mkdir(parents=True)
            (model / "inference.json").write_text("{}", encoding="utf-8")
            (model / "inference.pdiparams").write_bytes(b"params")

            self.assertEqual(model, module.find_inference_model_root(root))
            duplicate = root / "duplicate"
            duplicate.mkdir()
            (duplicate / "inference.json").write_text("{}", encoding="utf-8")
            (duplicate / "inference.pdiparams").write_bytes(b"params")
            with self.assertRaisesRegex(RuntimeError, "exactly one"):
                module.find_inference_model_root(root)

    def test_fixed_model_and_sample_artifacts_match_preview_contract(self) -> None:
        module = self._load_driver()

        self.assertEqual(
            ["PP-OCRv5_server_det", "PP-OCRv5_server_rec", "PP-LCNet_x1_0_textline_ori"],
            [item["name"] for item in module.MODEL_ARTIFACTS],
        )
        self.assertEqual(
            ["text_detection", "text_recognition", "textline_orientation"],
            [item["purpose"] for item in module.SAMPLE_ARTIFACTS],
        )
        self.assertTrue(all(len(item["sha256"]) == 64 for item in module.MODEL_ARTIFACTS))
        self.assertTrue(all(len(item["sha256"]) == 64 for item in module.SAMPLE_ARTIFACTS))

    def test_parity_metrics_enforce_bbox_confidence_and_label_thresholds(self) -> None:
        module = self._load_driver()

        self.assertAlmostEqual(1.0, module.bbox_iou([0, 0, 10, 10], [0, 0, 10, 10]))
        self.assertAlmostEqual(0.25, module.bbox_iou([0, 0, 10, 10], [0, 0, 5, 5]))
        report = module.validate_parity(
            {
                "detection": {"boxes": [[0, 0, 10, 10]], "scores": [0.98]},
                "recognition": {"texts": ["PaddleOCR"], "scores": [0.97]},
                "orientation": {"labels": ["180_degree"], "scores": [0.99]},
            },
            {
                "detection": {"boxes": [[0.1, 0.1, 9.9, 9.9]], "scores": [0.97]},
                "recognition": {"texts": ["PaddleOCR"], "scores": [0.96]},
                "orientation": {"labels": ["180_degree"], "scores": [0.98]},
            },
        )
        self.assertGreaterEqual(report["minimumBboxIou"], 0.95)
        self.assertLessEqual(report["maximumConfidenceDelta"], 0.02)
        with self.assertRaisesRegex(RuntimeError, "recognition text"):
            module.validate_parity(
                {
                    "detection": {"boxes": [[0, 0, 10, 10]], "scores": [0.98]},
                    "recognition": {"texts": ["A"], "scores": [0.97]},
                    "orientation": {"labels": ["0_degree"], "scores": [0.99]},
                },
                {
                    "detection": {"boxes": [[0, 0, 10, 10]], "scores": [0.98]},
                    "recognition": {"texts": ["B"], "scores": [0.97]},
                    "orientation": {"labels": ["0_degree"], "scores": [0.99]},
                },
            )

    def test_provider_profile_requires_actual_expected_node_execution(self) -> None:
        module = self._load_driver()
        records = [
            {"cat": "Node", "name": "conv_kernel_time", "args": {"provider": "CUDAExecutionProvider"}},
            {"cat": "Session", "name": "model_run", "args": {}},
        ]

        report = module.validate_provider_profile(records, "CUDAExecutionProvider")

        self.assertEqual("CUDAExecutionProvider", report["provider"])
        self.assertEqual(1, report["nodeEvents"])
        with self.assertRaisesRegex(RuntimeError, "CPUExecutionProvider"):
            module.validate_provider_profile(records, "CPUExecutionProvider")

    def test_safe_tar_extraction_rejects_path_traversal(self) -> None:
        module = self._load_driver()
        import tarfile

        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            root = Path(temporary)
            archive = root / "bad.tar"
            with tarfile.open(archive, "w") as bundle:
                payload = b"escape"
                member = tarfile.TarInfo("../escape.txt")
                member.size = len(payload)
                bundle.addfile(member, io.BytesIO(payload))

            with self.assertRaisesRegex(RuntimeError, "unsafe tar member"):
                module.extract_verified_tar(archive, root / "output")
            self.assertFalse((root / "escape.txt").exists())

    def test_fixed_candidate_wheels_have_exact_size_and_hash_contracts(self) -> None:
        module = self._load_driver()

        self.assertEqual(
            (
                "https://paddle-whl.bj.bcebos.com/stable/cpu/paddlepaddle/"
                "paddlepaddle-3.2.2-cp311-cp311-win_amd64.whl"
            ),
            module.PADDLE_WHEEL["url"],
        )
        self.assertEqual(
            {
                "paddleocr-3.7.0-py3-none-any.whl": (
                    146750,
                    "c0f0a81ad4112727f30c6fcf986ac0ef6a120d31ee0991a01fae0357ee32d338",
                ),
                "paddlex-3.7.2-py3-none-any.whl": (
                    2239708,
                    "f1678bf650bbaccfd8f0d4e49d0ae631b4685c829fdae6e802ccd90d4fcb9a7f",
                ),
                "onnxruntime_gpu-1.26.0-cp311-cp311-win_amd64.whl": (
                    226539455,
                    "cc5329aad02d9745cc3ae9cdb185bfa1aad242a7bf89b8c471280002ec40f98a",
                ),
                "paddle2onnx-2.1.0-cp311-cp311-win_amd64.whl": (
                    2027711,
                    "478993e17ed0212b79a4d6e2d8d0582ebb19c7230b7f365d51222833e98581b3",
                ),
                "paddlepaddle-3.2.2-cp311-cp311-win_amd64.whl": (
                    101715828,
                    "7ee7a0783de00a50f89a959aabfa83dd969ccce57e2c53f92f4d592d1df1aceb",
                ),
            },
            {
                filename: (contract["size"], contract["sha256"])
                for filename, contract in module.FIXED_CANDIDATE_WHEELS.items()
            },
        )

    def test_wheel_inventory_records_every_file_size_and_hash(self) -> None:
        module = self._load_driver()
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            wheelhouse = Path(temporary)
            first = wheelhouse / "a-1-py3-none-any.whl"
            second = wheelhouse / "b-2-py3-none-any.whl"
            first.write_bytes(b"first")
            second.write_bytes(b"second")

            inventory = module.collect_wheel_inventory(wheelhouse)

            self.assertEqual([first.name, second.name], [item["filename"] for item in inventory])
            self.assertEqual([5, 6], [item["size"] for item in inventory])
            self.assertEqual(
                [module.sha256_file(first), module.sha256_file(second)],
                [item["sha256"] for item in inventory],
            )

    def test_converter_blocker_skips_all_downstream_model_gates(self) -> None:
        module = self._load_driver()
        evidence = {"gates": {}}

        module.record_converter_blocker(evidence, "WinError 127")

        self.assertEqual("blocked", evidence["gates"]["converterImport"])
        self.assertEqual("WinError 127", evidence["compatibilityBlocker"]["error"])
        self.assertEqual(
            {
                "converterImport": "blocked",
                "modelDownloads": "skipped",
                "modelConversion": "skipped",
                "paddleOnnxParity": "skipped",
                "cpuProviderInference": "skipped",
                "cudaProviderInference": "skipped",
            },
            evidence["gates"],
        )

    def test_last_json_object_ignores_loader_noise(self) -> None:
        module = self._load_driver()

        self.assertEqual(
            {"moduleLoaded": False, "winerror": 127},
            module.last_json_object(
                "warning from native loader\n"
                '{"moduleLoaded": false, "winerror": 127}\n'
            ),
        )
        self.assertIsNone(module.last_json_object("warning only\n"))

    def test_converter_probe_reports_numeric_windows_loader_error(self) -> None:
        module = self._load_driver()
        arguments = module.converter_probe_arguments()

        self.assertEqual("-c", arguments[0])
        self.assertIn("ctypes.WinDLL", arguments[1])
        self.assertIn("winerror", arguments[1])
        self.assertIn("paddle2onnx_cpp2py_export", arguments[1])


class CleanOcrImportScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertTrue(CLEANUP_SCRIPT.is_file(), f"missing production script: {CLEANUP_SCRIPT}")
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT)
        self.root = Path(self.temporary.name)
        self.working_root = self.root / ".runtime-build" / "ocr-import"
        self.targets = [
            self.working_root / "environment",
            self.working_root / "downloads",
            self.working_root / "conversion-cache",
        ]
        for index, target in enumerate(self.targets):
            target.mkdir(parents=True)
            (target / f"temporary-{index}.bin").write_bytes(b"temporary")
        self.evidence = self.working_root / "evidence" / "result.json"
        self.evidence.parent.mkdir(parents=True)
        self.evidence.write_text("{}\n", encoding="utf-8")
        self.sentinels = [
            self.root / "resource-library" / "ocr-models" / "installed" / "model.onnx",
            self.root / "packaging" / "scripts" / "source.py",
            self.root / "profiles" / "default.json",
            self.root / "dataset" / "sample.png",
            self.root / "output" / "result.json",
            self.root / ".toolchains" / "Python" / "python.exe",
        ]
        for sentinel in self.sentinels:
            sentinel.parent.mkdir(parents=True, exist_ok=True)
            sentinel.write_bytes(b"protected")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _invoke(self, *, apply: bool = False) -> subprocess.CompletedProcess[str]:
        apply_switch = " -Apply" if apply else ""
        command = (
            "$ErrorActionPreference='Stop'; "
            f"$records = & {_ps_literal(CLEANUP_SCRIPT)} -ProjectRoot {_ps_literal(self.root)}"
            f"{apply_switch} 6>$null; @($records) | ConvertTo-Json -Depth 5 -Compress"
        )
        return _powershell(command)

    def test_preview_lists_only_three_approved_targets_without_writing(self) -> None:
        before = _tree_state(self.root)
        completed = self._invoke()

        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        records = json.loads(completed.stdout)
        self.assertEqual(
            {str(target) for target in self.targets},
            {record["Path"] for record in records},
        )
        self.assertTrue(all(record["Type"] == "Directory" for record in records))
        self.assertEqual(before, _tree_state(self.root))

    def test_apply_removes_only_three_approved_targets(self) -> None:
        completed = self._invoke(apply=True)

        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        self.assertTrue(all(not target.exists() for target in self.targets))
        self.assertTrue(self.evidence.is_file())
        self.assertTrue(all(sentinel.is_file() for sentinel in self.sentinels))

    def test_reparse_point_below_target_is_rejected_before_removal(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        outside_sentinel = outside / "keep.bin"
        outside_sentinel.write_bytes(b"outside")
        junction = self.targets[0] / "outside-link"
        created = _powershell(
            f"New-Item -ItemType Junction -Path {_ps_literal(junction)} -Target {_ps_literal(outside)} | Out-Null"
        )
        if created.returncode:
            self.skipTest(f"junction creation is unavailable: {created.stdout}{created.stderr}")

        completed = self._invoke(apply=True)

        self.assertNotEqual(0, completed.returncode)
        self.assertIn("reparse", (completed.stdout + completed.stderr).lower())
        self.assertTrue(outside_sentinel.is_file())
        self.assertTrue(all(target.exists() for target in self.targets))


if __name__ == "__main__":
    unittest.main()
