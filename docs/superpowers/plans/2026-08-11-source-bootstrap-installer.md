# Source Bootstrap Installer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Make a source ZIP or clone install the complete local E621 WebUI after users double-click Install-WebUI.bat, without system Python, Node, CUDA Toolkit, Visual Studio, or Windows SDK.

**Architecture:** The BAT launches Windows PowerShell only. bootstrap_install.ps1 checks the immutable inventory, hardware, path and space; it verifies/expands a prebuilt CPython asset and starts a standard-library Python installer. The installer chooses CPU/NVIDIA variants, downloads only frozen artifacts, stages and probes every required runtime/resource offline, publishes atomically, records completion, and removes temporary payloads.

**Tech Stack:** Windows PowerShell 5.1 compatibility, Python 3.11 standard library, JSON, unittest, existing runtime manifests and worker protocols.

---

## Development test precondition

This isolated worktree intentionally has no ignored runtime/toolchain directories. Before Task 1, set `ANIMA_SOURCE_BOOTSTRAP_TEST_PYTHON` to a verified, read-only **project-embedded** CPython 3.11 interpreter; it must not point to a system Python installation. Validate it once:

~~~
$TestPython = (Resolve-Path -LiteralPath $env:ANIMA_SOURCE_BOOTSTRAP_TEST_PYTHON).Path
& $TestPython -B -I -c "import sys; assert sys.version_info[:2] == (3, 11); print(sys.executable)"
~~~

All displayed Python commands below use the short form `.runtime-build\\runtimes\\core\\python.exe` after bootstrap test setup exists. Before then, substitute `& $TestPython` for that executable. The target installer itself remains independent of this development-only interpreter.

---

## Constraints carried into every task

- All mutable installer state lives under .runtime-build. Do not alter PATH, registry, system Python, drivers, CUDA Toolkit, source, user data, data, or output.
- E621 only. Required: E621 EVA02, Qwen3 0.6B tokenizer, full LSE14/JTP-3/Waifu/CLIP quality stack, OCR CPU; NVIDIA also gets OCR GPU.
- caption-e621 and policy retain logical IDs. State records cpu or cuda. A CUDA import error never counts as CPU support.
- Every artifact record has HTTPS URL, allowed hosts, exact byte size, lowercase SHA-256, safe target path; Hugging Face records have a full 40-character revision.
- The developer-only CPython/index asset build and manifest generator never run on target machines. Public release upload is not authorized and remains a gate.
- Fixtures can prove installer mechanics only. Do not report clean CPU/NVIDIA or public URL/license acceptance unless actually run.

## File map

| Path | Responsibility |
| --- | --- |
| Install-WebUI.bat | Stable double-click entrypoint to bootstrap PowerShell. |
| packaging/scripts/bootstrap_install.ps1 | Built-in PowerShell preflight, pinned inventory, CPython fetch/extract, logs and handoff. |
| packaging/installer/manifest.py | Strict manifest parser, fingerprints and variant selection. |
| packaging/installer/download.py | HTTPS redirect allowlist, Range resume, checksum and manual diagnostics. |
| packaging/installer/paths.py | Root containment, safe ZIP handling, journals and cleanup. |
| packaging/installer/assemble.py | Exact wheel extraction, source/resource assembly, state checks. |
| packaging/installer/probes.py | Offline representative Core/Tagger/quality/tokenizer/OCR probes. |
| packaging/installer/install.py | Standard-library CLI orchestrator. |
| packaging/installer/install-manifest.json | Generated, versioned production inventory. |
| packaging/installer/release-artifacts.json | Controlled-release CPython/index metadata, rejected while incomplete. |
| packaging/scripts/build_bootstrap_runtime.ps1 | Developer-only CPython base packager. |
| packaging/scripts/build_install_manifest.py | Developer-only exact artifact inventory generator. |
| packaging/scripts/Validate-SourceBootstrapRelease.ps1 | Local non-publishing release validator. |
| packaging/requirements/caption-e621-{cpu,cuda}.* | Explicit ONNX Runtime variants. |
| packaging/requirements/policy-{cpu,cuda}.* | Explicit PyTorch variants. |
| tests/unit/test_source_bootstrap_*.py | Installer unit contracts. |
| tests/integration/test_source_bootstrap_fixture.py | Local end-to-end fixture matrix. |

## Task 1: Freeze manifest contract

**Files:**
- Create: packaging/installer/__init__.py
- Create: packaging/installer/manifest.py
- Create: packaging/installer/release-artifacts.json
- Create: packaging/installer/install-manifest.json
- Create: tests/unit/test_source_bootstrap_manifest.py
- Modify: ROADMAP.md
- Modify: MEMORY.md

- [ ] **Step 1: Write failing schema tests.**

~~~
def test_manifest_rejects_floating_revision_and_unknown_host(self) -> None:
    value = minimal_manifest()
    artifact = value["components"][0]["variants"]["cpu"]["artifacts"][0]
    artifact["url"] = "https://huggingface.co/Qwen/Qwen3-0.6B/resolve/main/tokenizer.json"
    artifact["revision"] = "main"
    with self.assertRaisesRegex(ManifestError, "full commit SHA"):
        load_manifest(value)

    artifact["url"] = "https://huggingface.co/Qwen/Qwen3-0.6B/resolve/" + "a" * 40 + "/tokenizer.json"
    artifact["revision"] = "a" * 40
    artifact["allowedHosts"] = ["huggingface.co", "evil.example"]
    with self.assertRaisesRegex(ManifestError, "allowed host"):
        load_manifest(value)
~~~

Add independent tests for non-HTTPS, zero size, invalid SHA, case-folded duplicate target, parent/drive target, mandatory component missing, CPU CUDA wheel, and incomplete Release record.

- [ ] **Step 2: Run RED.**

~~~
& .\.runtime-build\runtimes\core\python.exe -B -I -m unittest discover -s tests\unit -p 'test_source_bootstrap_manifest.py' -v
~~~

Expected: module import failure.

- [ ] **Step 3: Implement strict stdlib-only data types.**

Define immutable Artifact, ComponentVariant, Component, InstallManifest, ManifestError, canonical_json, sha256_bytes, load_manifest_path and select_components.

~~~
def validate_artifact(value: object, hosts: frozenset[str]) -> Artifact:
    permitted = {
        "id", "url", "allowedHosts", "sizeBytes", "sha256", "relativePath",
        "repository", "revision",
    }
    if not isinstance(value, dict) or set(value) - permitted:
        raise ManifestError("artifact fields are invalid")
    url = validate_https_url(value["url"])
    allowed = validate_hosts(value["allowedHosts"])
    if url.hostname not in allowed or not allowed.issubset(hosts):
        raise ManifestError("artifact allowed host is invalid")
    return Artifact(...)
~~~

Top level is exactly schemaVersion, releaseVersion, sourceCommit, allowedHosts, bootstrap, components, cleanup. Validate targets with PureWindowsPath. Fingerprint each component variant from canonical JSON. release-artifacts.json missing publishedUrl, sizeBytes or sha256 fails validation; install-manifest.json is generated only.

- [ ] **Step 4: Run green verification.**

~~~
& .\.runtime-build\runtimes\core\python.exe -B -I -m unittest discover -s tests\unit -p 'test_source_bootstrap_manifest.py' -v
& .\.runtime-build\runtimes\core\python.exe -B -I -m unittest discover -s tests\unit -p 'test_resource_catalog.py' -v
~~~

- [ ] **Step 5: Update R6 to [-], MEMORY, and commit.**

~~~
git add packaging/installer tests/unit/test_source_bootstrap_manifest.py ROADMAP.md MEMORY.md
git commit -m "feat: add source bootstrap manifest contract"
~~~

## Task 2: Implement verified Range-resume downloads

**Files:**
- Create: packaging/installer/download.py
- Create: tests/unit/test_source_bootstrap_download.py
- Modify: packaging/installer/manifest.py
- Modify: ROADMAP.md
- Modify: MEMORY.md

- [ ] **Step 1: Write failing fake-transport tests.**

~~~
def test_download_resumes_partial_with_range(self) -> None:
    payload = b"verified artifact payload"
    artifact = artifact_for(payload)
    partial = self.cache / f"{artifact.sha256}.partial"
    partial.write_bytes(payload[:9])
    transport = ScriptedTransport([
        response(206, {"Content-Range": f"bytes 9-{len(payload)-1}/{len(payload)}"}, payload[9:])
    ])

    verified = download_verified(artifact, self.cache, transport=transport)

    self.assertEqual(payload, verified.read_bytes())
    self.assertEqual("bytes=9-", transport.requests[0].headers["Range"])
    self.assertFalse(partial.exists())
~~~

Add 200-to-Range restart, forbidden redirect, mismatch deletion, transient retention of only partial, and ManualDownloadRequired URL/name/size/SHA tests.

- [ ] **Step 2: Run RED.**

~~~
& .\.runtime-build\runtimes\core\python.exe -B -I -m unittest discover -s tests\unit -p 'test_source_bootstrap_download.py' -v
~~~

- [ ] **Step 3: Implement downloader.**

Use urllib.request with a redirect handler validating every HTTPS hop and host. Cache names are SHA only.

~~~
partial = cache_root / f"{artifact.sha256}.partial"
complete = cache_root / artifact.sha256
if complete.is_file() and verify_file(complete, artifact):
    return complete
if complete.exists():
    complete.unlink()
offset = partial.stat().st_size if partial.exists() else 0
headers = {"Range": f"bytes={offset}-"} if offset else {}
~~~

Append only valid 206 matching Content-Range. A 200 response to Range deletes partial then retries once from zero. Stream 1 MiB chunks, verify bytes/SHA, use os.replace. Retry only temporary network/5xx errors up to three attempts; 403/404/auth/hash are terminal actionable errors.

- [ ] **Step 4: Run green verification.**

~~~
& .\.runtime-build\runtimes\core\python.exe -B -I -m unittest discover -s tests\unit -p 'test_source_bootstrap_download.py' -v
& .\.runtime-build\runtimes\core\python.exe -B -I -m unittest discover -s tests\unit -p 'test_source_bootstrap_manifest.py' -v
~~~

- [ ] **Step 5: Update R9 to [-], MEMORY, and commit.**

~~~
git add packaging/installer/download.py packaging/installer/manifest.py tests/unit/test_source_bootstrap_download.py ROADMAP.md MEMORY.md
git commit -m "feat: add verified bootstrap downloads"
~~~

## Task 3: Defend paths and publish transactions

**Files:**
- Create: packaging/installer/paths.py
- Create: tests/unit/test_source_bootstrap_paths.py
- Modify: ROADMAP.md
- Modify: MEMORY.md

- [ ] **Step 1: Write failing archive/recovery tests.**

~~~
def test_safe_extract_rejects_parent_traversal_before_output(self) -> None:
    archive = self.root / "escape.zip"
    with zipfile.ZipFile(archive, "w") as value:
        value.writestr("..\\outside.txt", b"unsafe")

    with self.assertRaisesRegex(PathSafetyError, "unsafe archive member"):
        safe_extract_zip(archive, self.root / "staging" / "base")

    self.assertFalse((self.root.parent / "outside.txt").exists())
~~~

Cover absolute drive/device paths, case collision, ZIP symlink bits, staging outside project, existing junction/reparse point, interrupted journal recovery, and cleanup trying to target source/resource/data/output.

- [ ] **Step 2: Run RED.**

~~~
& .\.runtime-build\runtimes\core\python.exe -B -I -m unittest discover -s tests\unit -p 'test_source_bootstrap_paths.py' -v
~~~

- [ ] **Step 3: Implement ProjectLayout and transaction API.**

Expose ProjectLayout.create, safe_relative, assert_within_root, safe_extract_zip, recover_transactions, publish_directories, cleanup_success. Resolve existing ancestors with os.lstat, reject links/reparse points, and use separator-boundary containment. ZIP only accepts regular files/dirs and safe normalized Windows-relative names.

Journal to .runtime-build/transactions/<uuid>.json; rename existing target to same-volume backup; rename fully probed stage to target; verify target; remove backup/journal. Recover before any download. Success cleanup permits bootstrap, complete cache, staging/build-cache/journal only. Failure keeps logs and partial files only.

- [ ] **Step 4: Run green verification.**

~~~
& .\.runtime-build\runtimes\core\python.exe -B -I -m unittest discover -s tests\unit -p 'test_source_bootstrap_paths.py' -v
& .\.runtime-build\runtimes\core\python.exe -B -I -m unittest discover -s tests\unit -p 'test_source_bootstrap_download.py' -v
~~~

- [ ] **Step 5: Update R13 to [-], MEMORY, and commit.**

~~~
git add packaging/installer/paths.py tests/unit/test_source_bootstrap_paths.py ROADMAP.md MEMORY.md
git commit -m "feat: add transactional bootstrap staging"
~~~

## Task 4: Create PowerShell-only stage one

**Files:**
- Modify: Install-WebUI.bat
- Create: packaging/scripts/bootstrap_install.ps1
- Modify: packaging/scripts/desktop_control.ps1
- Create: tests/unit/test_source_bootstrap_powershell.py
- Modify: tests/unit/test_desktop_control.py
- Modify: ROADMAP.md
- Modify: MEMORY.md

- [ ] **Step 1: Write failing static/parser tests.**

~~~
def test_source_entrypoint_uses_bootstrap_not_legacy_ocr_prompt(self) -> None:
    batch = (ROOT / "Install-WebUI.bat").read_text(encoding="ascii")
    script = (ROOT / "packaging" / "scripts" / "bootstrap_install.ps1").read_text(encoding="utf-8")
    self.assertIn("bootstrap_install.ps1", batch)
    self.assertNotIn("desktop_control.ps1", batch)
    self.assertIn("[Environment]::Is64BitOperatingSystem", script)
    self.assertIn("Get-Volume", script)
    self.assertIn("install-manifest.json", script)
    self.assertNotIn("LOCALAPPDATA", script)
~~~

Parse bootstrap through System.Management.Automation.Language.Parser.ParseFile. Require desktop control launcher state/log path at Join-Path $projectRoot '.runtime-build\launcher' and Install to validate complete state, not prompt OCR.

- [ ] **Step 2: Run RED.**

~~~
& .\.runtime-build\runtimes\core\python.exe -B -I -m unittest discover -s tests\unit -p 'test_source_bootstrap_powershell.py' -v
& .\.runtime-build\runtimes\core\python.exe -B -I -m unittest discover -s tests\unit -p 'test_desktop_control.py' -v
~~~

- [ ] **Step 3: Implement BAT and PowerShell bootstrap.**

BAT content:

~~~
@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0packaging\scripts\bootstrap_install.ps1" -ProjectRoot "%~dp0" %*
exit /b %ERRORLEVEL%
~~~

PowerShell validates Windows/x64/plain writable root, creates UTF-8 logs only below .runtime-build\logs, verifies hard-coded canonical manifest SHA, computes selected peak storage before any download, checks Get-Volume free bytes, safely expands the bootstrap zip and runs one Python command with project root, manifest path and pinned SHA arguments. Failure exits nonzero and prints URL/name/size/SHA. desktop_control moves state/logs under .runtime-build and requires state plus Core check before success.

- [ ] **Step 4: Run green verification.**

~~~
& .\.runtime-build\runtimes\core\python.exe -B -I -m unittest discover -s tests\unit -p 'test_source_bootstrap_powershell.py' -v
& .\.runtime-build\runtimes\core\python.exe -B -I -m unittest discover -s tests\unit -p 'test_desktop_control.py' -v
~~~

- [ ] **Step 5: Update R8 to [-], MEMORY, and commit.**

~~~
git add Install-WebUI.bat packaging/scripts/bootstrap_install.ps1 packaging/scripts/desktop_control.ps1 tests/unit/test_source_bootstrap_powershell.py tests/unit/test_desktop_control.py ROADMAP.md MEMORY.md
git commit -m "feat: add PowerShell source bootstrap entrypoint"
~~~

## Task 5: Build CPython asset and true CPU/CUDA variants

**Files:**
- Create: packaging/scripts/build_bootstrap_runtime.ps1
- Create: packaging/scripts/build_install_manifest.py
- Create: packaging/requirements/caption-e621-cpu.in
- Create: packaging/requirements/caption-e621-cpu.lock
- Create: packaging/requirements/caption-e621-cuda.in
- Create: packaging/requirements/caption-e621-cuda.lock
- Create: packaging/requirements/policy-cpu.in
- Create: packaging/requirements/policy-cpu.lock
- Create: packaging/requirements/policy-cuda.in
- Create: packaging/requirements/policy-cuda.lock
- Create: tests/unit/test_source_bootstrap_release_build.py
- Modify: packaging/installer/release-artifacts.json
- Modify: packaging/installer/install-manifest.json
- Modify: ROADMAP.md
- Modify: MEMORY.md

- [ ] **Step 1: Write failing builder/variant tests.**

~~~
def test_cpu_variants_have_no_cuda_distribution(self) -> None:
    manifest = load_manifest_path(PRODUCTION_MANIFEST)
    for component_id in ("caption-e621", "policy"):
        cpu = manifest.component(component_id).variants["cpu"].artifacts
        self.assertFalse(any("cuda" in item.id.lower() or "+cu" in item.id.lower() for item in cpu))
        self.assertTrue(manifest.component(component_id).variants["cuda"].artifacts)
~~~

Also assert base ZIP contains python.exe, python311.dll, Lib, python311._pth and provenance. Test generator rejects wheels missing URL/bytes/SHA and incomplete release metadata.

- [ ] **Step 2: Run RED.**

~~~
& .\.runtime-build\runtimes\core\python.exe -B -I -m unittest discover -s tests\unit -p 'test_source_bootstrap_release_build.py' -v
~~~

- [ ] **Step 3: Implement release-only builder and lock inventory.**

build_bootstrap_runtime.ps1 validates exact 3.11.15, stages interpreter/stdlib, writes deterministic pth/provenance, performs stdlib-only offline probe, archives it. It is never end-user invoked.

build_install_manifest.py validates every concrete Windows CPython 3.11 wheel against lock name/version/hash and emits URL/hosts/bytes/SHA/target/revision/probe/footprint; it rejects resolver, floating sources, unknown hosts and incomplete release metadata.

CPU inputs are exactly:

~~~
# caption-e621-cpu.in
numpy==2.2.6
onnxruntime==1.26.0
Pillow==11.3.0

# policy-cpu.in
--extra-index-url https://download.pytorch.org/whl/cpu
numpy==2.4.6
open-clip-torch==3.3.0
Pillow==12.3.0
safetensors==0.8.0
torch==2.9.1+cpu
torchvision==0.24.1+cpu
~~~

CUDA-named inputs copy current approved GPU sets and receive complete CPython 3.11 locks. CPU acceptance requires real CPU probe.

- [ ] **Step 4: Generate/validate then run tests.**

~~~
& .\.runtime-build\runtimes\core\python.exe -B -I packaging\scripts\build_install_manifest.py --repository-root . --output packaging\installer\install-manifest.json --validate-only
& .\.runtime-build\runtimes\core\python.exe -B -I -m unittest discover -s tests\unit -p 'test_source_bootstrap_release_build.py' -v
& .\.runtime-build\runtimes\core\python.exe -B -I -m unittest discover -s tests\unit -p 'test_source_bootstrap_manifest.py' -v
~~~

- [ ] **Step 5: Update R7/R10 to [-], MEMORY, and commit.**

~~~
git add packaging/scripts/build_bootstrap_runtime.ps1 packaging/scripts/build_install_manifest.py packaging/installer packaging/requirements/caption-e621-* packaging/requirements/policy-* tests/unit/test_source_bootstrap_release_build.py ROADMAP.md MEMORY.md
git commit -m "feat: freeze bootstrap asset and runtime variants"
~~~

## Task 6: Assemble E621 runtimes and mandatory resources

**Files:**
- Create: packaging/installer/assemble.py
- Create: packaging/installer/install.py
- Create: tests/unit/test_source_bootstrap_install.py
- Modify: packaging/installer/install-manifest.json
- Modify: packaging/scripts/generate_runtime_manifests.py
- Modify: ROADMAP.md
- Modify: MEMORY.md

- [ ] **Step 1: Write failing selection/idempotency tests.**

~~~
def test_nvidia_plan_has_cpu_and_gpu_ocr_but_cpu_plan_never_selects_cuda(self) -> None:
    manifest = fixture_manifest()
    cpu = installation_plan(manifest, accelerator="cpu")
    gpu = installation_plan(manifest, accelerator="nvidia")
    self.assertEqual({"ocr-paddle"}, cpu.runtime_ids & {"ocr-paddle", "ocr-paddle-gpu"})
    self.assertEqual({"ocr-paddle", "ocr-paddle-gpu"}, gpu.runtime_ids & {"ocr-paddle", "ocr-paddle-gpu"})
    self.assertNotIn("policy-cuda", cpu.lock_names)
    self.assertIn("policy-cuda", gpu.lock_names)
~~~

Also test skip requires component fingerprint/all files/runtime manifest; drift repairs; failed staging preserves prior target; duplicate wheel path rejects; resource JSON includes Tagger, Qwen3, quality, E621 indexes, OCR.

- [ ] **Step 2: Run RED.**

~~~
& .\.runtime-build\runtimes\core\python.exe -B -I -m unittest discover -s tests\unit -p 'test_source_bootstrap_install.py' -v
~~~

- [ ] **Step 3: Implement deterministic assembly.**

Copy CPython base to staged runtime; unzip only selected downloaded wheels; never run pip; copy declared owner/shared source; strip pip/wheel/pytest; generate existing runtime manifests. Map explicit components core, caption-e621, classify-e621, replace-e621, nl, policy, export, token-budget, ocr-cpu, ocr-gpu, e621-indexes, e621-tagger, qwen3-tokenizer, quality-stack, ocr-models.

Create ResourceCatalog-compatible resource JSON/defaults; E621 is mandatory and Danbooru remains unavailable. install.py recovers transaction, selects plan, stages/probes/publishes, then writes .runtime-build/manifests/install-state.json only after complete success. State records schema/source/release/manifest SHA/accelerator/variants/component fingerprints/time.

- [ ] **Step 4: Run green verification.**

~~~
& .\.runtime-build\runtimes\core\python.exe -B -I -m unittest discover -s tests\unit -p 'test_source_bootstrap_install.py' -v
& .\.runtime-build\runtimes\core\python.exe -B -I -m unittest discover -s tests\unit -p 'test_engineering_scripts.py' -v
~~~

Run fixture install twice and assert second run makes zero download requests.

- [ ] **Step 5: Update R11/R12 to [-], MEMORY, and commit.**

~~~
git add packaging/installer/assemble.py packaging/installer/install.py packaging/installer/install-manifest.json packaging/scripts/generate_runtime_manifests.py tests/unit/test_source_bootstrap_install.py ROADMAP.md MEMORY.md
git commit -m "feat: assemble mandatory source bootstrap components"
~~~

## Task 7: Require real offline probes and fallback

**Files:**
- Create: packaging/installer/probes.py
- Modify: packaging/installer/assemble.py
- Modify: packaging/installer/install.py
- Modify: tests/unit/test_source_bootstrap_install.py
- Create: tests/integration/test_source_bootstrap_fixture.py
- Modify: ROADMAP.md
- Modify: MEMORY.md

- [x] **Step 1: Write failing probe/fallback tests.**

~~~
def test_gpu_probe_failure_rebuilds_caption_policy_cpu_keeps_ocr_cpu(self) -> None:
    result = install_with_probe_results(
        nvidia_available=True,
        probes={"caption-cuda": False, "policy-cuda": False},
    )
    self.assertEqual("cpu", result.state["components"]["caption-e621"]["variant"])
    self.assertEqual("cpu", result.state["components"]["policy"]["variant"])
    self.assertEqual("cpu", result.state["components"]["ocr-cpu"]["variant"])
    self.assertNotIn("ocr-gpu", result.state["components"])
~~~

Also reject import-only result, CPU showing CUDA, GPU with no device, network-enabled probe environment, and quality/OCR failure publishing state.

- [x] **Step 2: Run RED.**

~~~
& .\.runtime-build\runtimes\core\python.exe -B -I -m unittest discover -s tests\unit -p 'test_source_bootstrap_install.py' -v
~~~

- [x] **Step 3: Implement network-blocked representative probes.**

Clear proxy variables, set HF_HUB_OFFLINE/TRANSFORMERS_OFFLINE, patch socket connection. Proof targets:

~~~
core:       anima_core --check-runtime
caption:    load E621 EVA02, infer RGB sample, parse non-empty sorted tags
quality:    load CLIP/LSE14/JTP-3/Waifu, produce finite scores
tokenizer:  load Qwen3 tokenizer JSON, count frozen UTF-8 string
ocr-cpu:    load local PaddleOCR models and process CPU sample
ocr-gpu:    report CUDA and gpu:0, process sample within CPU tolerance
~~~

nvidia-smi only selects. Failed Caption/Policy CUDA rebuilds CPU. Failed OCR GPU drops GPU staging while preserving CPU; no GPU success text is emitted.

- [x] **Step 4: Run unit/fixture CPU probes.**

~~~
& .\.runtime-build\runtimes\core\python.exe -B -I -m unittest discover -s tests\unit -p 'test_source_bootstrap_install.py' -v
& .\.runtime-build\runtimes\core\python.exe -B -I -m unittest discover -s tests\integration -p 'test_source_bootstrap_fixture.py' -v
~~~

- [x] **Step 5: Update ROADMAP/MEMORY and commit.**

~~~
git add packaging/installer/probes.py packaging/installer/assemble.py packaging/installer/install.py tests/unit/test_source_bootstrap_install.py tests/integration/test_source_bootstrap_fixture.py ROADMAP.md MEMORY.md
git commit -m "feat: verify source bootstrap runtimes offline"
~~~

## Task 8: Failure matrix, frontend output and release gate

**Files:**
- Modify: .gitignore
- Create: frontend/dist/** from project Node only
- Create: packaging/scripts/Validate-SourceBootstrapRelease.ps1
- Modify: README.md
- Modify: docs/THIRD_PARTY_NOTICES.md
- Modify: tests/integration/test_source_bootstrap_fixture.py
- Modify: ROADMAP.md
- Modify: MEMORY.md

- [x] **Step 1: Write failing matrix/release tests.**

~~~
def test_hash_failure_never_publishes_component_only_partial_and_log_remain(self) -> None:
    result = run_fixture_install(corrupt_artifact="e621-tagger-model")
    self.assertNotEqual(0, result.returncode)
    self.assertFalse((result.root / ".runtime-build" / "manifests" / "install-state.json").exists())
    self.assertFalse((result.root / "resource-library" / "tagging-models" / "caption-e621-eva02-large-full-v1").exists())
    self.assertTrue(list((result.root / ".runtime-build" / "logs").glob("*.log")))
~~~

Add disk shortfall before request, interrupted Range resume, repeated install no requests, Chinese/space path, offline success, no-NVIDIA route, incomplete Release metadata. Do not pass GPU without actual hardware probe.

- [x] **Step 2: Run RED.**

~~~
& .\.runtime-build\runtimes\core\python.exe -B -I -m unittest discover -s tests\integration -p 'test_source_bootstrap_fixture.py' -v
~~~

- [x] **Step 3: Implement cleanup, static frontend and validator.**

Unignore only source distribution output:

~~~
frontend/dist/*
!frontend/dist/
!frontend/dist/**
~~~

Build with project Node only and commit dist not node_modules. Validator reads manifest, checks frontend/dist/index.html, verifies mandatory URL/host/bytes/SHA/revision, notices coverage, and fails public mirror with unverified license/missing Release metadata. It never contacts GitHub or publishes.

Success deletes bootstrap/complete-cache/staging/build-cache/journal; leaves state/log. Failure removes complete cache/staging and leaves partial/log. Do not modify the seven inaccessible legacy OCR staging directories.

- [x] **Step 4: Run verification matrix.**

~~~
& .\.runtime-build\runtimes\core\python.exe -B -I -m unittest discover -s tests -p 'test_source_bootstrap_*.py' -v
& .\packaging\scripts\Validate-SourceBootstrapRelease.ps1 -ProjectRoot .
& .\.toolchains\node-v24.18.0-win-x64\npm.cmd --prefix frontend run typecheck
& .\.toolchains\node-v24.18.0-win-x64\npm.cmd --prefix frontend run build
~~~

- [x] **Step 5: Update docs/records and commit.**

README makes double-click Install-WebUI.bat the sole user install path; remove optional OCR/default none documentation. Document mandatory OCR, selection, local logs, manual diagnostic, offline behavior, Release gate. Notices list actual manifest sources/licenses. Mark R6-R13 only with evidence; leave R14/R15 unmarked absent clean-machine/public link evidence.

~~~
git add .gitignore frontend/dist packaging/scripts/Validate-SourceBootstrapRelease.ps1 README.md docs/THIRD_PARTY_NOTICES.md tests/integration/test_source_bootstrap_fixture.py ROADMAP.md MEMORY.md
git commit -m "feat: complete source bootstrap installer validation"
~~~

## Task 9: Final local verification and truthful handoff

**Files:**
- Modify: ROADMAP.md
- Modify: MEMORY.md

- [x] **Step 1: Compare final diff to approved design.**

~~~
git diff 2e85063...HEAD --stat
git diff 2e85063...HEAD -- Install-WebUI.bat packaging/installer packaging/scripts/bootstrap_install.ps1 packaging/scripts/desktop_control.ps1 README.md docs/THIRD_PARTY_NOTICES.md ROADMAP.md MEMORY.md
~~~

- [x] **Step 2: Run available project-local verification.**

~~~
& .\.runtime-build\runtimes\core\python.exe -B -I -m unittest discover -s tests\unit -p 'test_source_bootstrap_*.py' -v
& .\.runtime-build\runtimes\core\python.exe -B -I -m unittest discover -s tests\unit -p 'test_desktop_control.py' -v
& .\.runtime-build\runtimes\core\python.exe -B -I -m unittest discover -s tests\integration -p 'test_source_bootstrap_fixture.py' -v
& .\packaging\scripts\Verify-Project.ps1 -Level Fast -OcrMode Auto
& .\packaging\scripts\Validate-SourceBootstrapRelease.ps1 -ProjectRoot .
git status --short
~~~

Record exact command outputs and no-server evidence; import-only is never replacement for a probe.

- [x] **Step 3: Commit verification record after fresh evidence.**

~~~
git add ROADMAP.md MEMORY.md
git commit -m "docs: record source bootstrap verification"
~~~

- [x] **Step 4: Report only actual evidence and gates.**

Report branch, commits, changed modules, tests, run method and no-server status. Do not push/create/upload a Release or claim public availability without new authorization.

## Requirement coverage

| Requirement | Tasks |
| --- | --- |
| Double-click/no system dependencies | 4, 5, 6, 8 |
| Windows x64/chinese-space path | 3, 4, 8 |
| Mandatory E621 resources | 1, 5, 6, 7 |
| CPU/CUDA Caption/Policy and OCR | 5, 6, 7 |
| Exact URL/revision/size/SHA/host | 1, 2, 5, 8 |
| Resume/stage/offline/transaction/idempotence | 2, 3, 6, 7, 8 |
| Diagnostics/cleanup | 2, 3, 4, 8 |
| Frontend no target npm | 8 |
| No silent OCR/quality | 6, 7, 8 |
| Matrix | 7, 8, 9 |
| Third-party/license/publication gate | 5, 8, 9 |

## Plan self-review

- Coverage: approved design maps to tasks; clean-machine, public Release, and license closure are explicit gates.
- Placeholder scan: each task has files, RED, concrete behavior, green command, record update and commit.
- Consistency: artifact fields, IDs, state, variants and probes are fixed across tasks.
- Scope: no Danbooru feature or unrelated Core/worker refactor.
