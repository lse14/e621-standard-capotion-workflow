# Optional OCR Model Bootstrap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let `Install-WebUI.bat` install/start the non-OCR-model WebUI while OCR models remain user-supplied, hash-verified, and fail-closed only when OCR is enabled.

**Architecture:** Keep OCR CPU/GPU runtimes in the source-bootstrap plan, but make the OCR model package a delayed local input. The existing OCR resource importer remains the only code that extracts, hashes, probes, and publishes model files. Bootstrap starts WebUI after a complete base install; Core preflight blocks only OCR-enabled tasks without a valid package.

**Tech Stack:** Windows PowerShell, Python 3.11 standard library, existing `ocr_resource.py`, unittest, existing Core resource catalog.

---

## File map

| Path | Responsibility |
| --- | --- |
| `packaging/installer/assemble.py` | Defines the components required for a base E621 installation. |
| `packaging/installer/probes.py` | Runs OCR functional probes only when both runtime and model resource are selected. |
| `packaging/installer/install.py` | Completes base install and invokes local OCR import only with complete user archives. |
| `packaging/scripts/ocr_resource.py` | Validates, stages, probes, and publishes user-provided model archives without rebuilding a runtime. |
| `packaging/scripts/bootstrap_install.ps1` | Starts WebUI after installer success and prints the OCR download guide. |
| `packaging/scripts/Validate-SourceBootstrapRelease.ps1` | Requires OCR runtimes but not a mirrored manual OCR model component. |
| `core/src/anima_core/job_preflight.py` | Explains how an OCR-enabled task is blocked when the validated model is absent. |
| `OCR_MODEL_DOWNLOAD.md` | Canonical official URLs, names, sizes, hashes, and the one directory users populate. |
| `tests/unit/test_source_bootstrap_install.py` | Base-plan and probe-selection regression tests. |
| `tests/unit/test_job_preflight.py` | OCR-enabled missing-model preflight regression test. |
| `tests/unit/test_source_bootstrap_powershell.py` | Auto-start/static PowerShell entrypoint test. |

## Task 1: Separate delayed OCR models from mandatory base components

**Files:**
- Modify: `packaging/installer/assemble.py`
- Modify: `packaging/installer/probes.py`
- Modify: `tests/unit/test_source_bootstrap_install.py`

- [ ] **Step 1: Write failing base-plan tests.**

```python
def test_base_e621_validation_does_not_require_delayed_ocr_models(self) -> None:
    manifest = self._manifest_without("ocr-models")
    assemble.validate_mandatory_e621_components(manifest)

def test_ocr_probe_is_skipped_when_no_model_component_is_selected(self) -> None:
    results = probes.run_offline_probes(
        self._cpu_components_without_ocr_models(),
        component_targets=self._targets_without_ocr_models(),
        runner=self.fail_runner,
    )
    self.assertIsNone(results["ocr-cpu"])
```

- [ ] **Step 2: Run the focused test and confirm RED.**

```powershell
& $env:ANIMA_SOURCE_BOOTSTRAP_TEST_PYTHON -B -I -m unittest tests.unit.test_source_bootstrap_install.SourceBootstrapInstallTests.test_base_e621_validation_does_not_require_delayed_ocr_models tests.unit.test_source_bootstrap_install.SourceBootstrapInstallTests.test_ocr_probe_is_skipped_when_no_model_component_is_selected -v
```

Expected: the first test reports `ocr-models` as missing; the second marks OCR runtime
functional evidence as absent without invoking the runner.

- [ ] **Step 3: Implement the minimum selection change.**

```python
MANDATORY_E621_COMPONENTS = frozenset({
    "core", "caption-e621", "classify-e621", "replace-e621", "nl", "policy",
    "export", "token-budget", "ocr-cpu", "e621-indexes", "e621-tagger",
    "qwen3-tokenizer", "quality-stack",
})

if "ocr-models" in selected:
    run_group(
        ("ocr-cpu", "ocr-models"),
        lambda: _probe_ocr(
            _target(component_targets, "ocr-cpu"),
            _target(component_targets, "ocr-models"),
            "cpu",
            runner=runner,
        ),
        "ocr-cpu",
    )
else:
    results["ocr-cpu"] = None
```

Keep GPU probing conditional on `ocr-models`. Teach the installer that only the explicit
`None` result for an OCR runtime without a selected model is acceptable for base install;
an explicit `False` remains a fatal runtime probe failure. No missing model may produce a
positive OCR functional probe.

- [ ] **Step 4: Run focused green verification.**

```powershell
& $env:ANIMA_SOURCE_BOOTSTRAP_TEST_PYTHON -B -I -m unittest tests.unit.test_source_bootstrap_install -v
```

Expected: exit code 0 with all source bootstrap installer unit tests passing.

- [ ] **Step 5: Commit the completed task.**

```powershell
git add packaging/installer/assemble.py packaging/installer/probes.py tests/unit/test_source_bootstrap_install.py
git commit -m "feat: make OCR models a delayed bootstrap resource"
```

## Task 2: Enforce manual archive input and automatic WebUI start

**Files:**
- Modify: `packaging/installer/install.py`
- Modify: `packaging/scripts/ocr_resource.py`
- Modify: `packaging/scripts/bootstrap_install.ps1`
- Modify: `tests/unit/test_source_bootstrap_install.py`
- Modify: `tests/unit/test_source_bootstrap_powershell.py`
- Modify: `tests/unit/test_ocr_resource_scripts.py`

- [ ] **Step 1: Write failing installer and PowerShell tests.**

```python
def test_complete_manual_archives_trigger_model_only_import_after_base_install(self) -> None:
    result = install_project(
        project_root=self.root,
        source_root=self.root,
        manifest=self.manifest,
        accelerator="cpu",
        base_runtime=self.base_runtime,
        fetch_artifact=self.fetch,
        probe_component=self.probe,
        write_runtime_manifest=self.write_runtime_manifest,
        require_mandatory_e621=False,
        import_optional_ocr_models=self._importer,
    )
    self.assertEqual([self.root], self.importer_roots)
    self.assertIn("OCR model import completed", result.messages)

def test_missing_manual_archives_leave_base_install_complete(self) -> None:
    result = install_project(
        project_root=self.root,
        source_root=self.root,
        manifest=self.manifest,
        accelerator="cpu",
        base_runtime=self.base_runtime,
        fetch_artifact=self.fetch,
        probe_component=self.probe,
        write_runtime_manifest=self.write_runtime_manifest,
        require_mandatory_e621=False,
        import_optional_ocr_models=self._importer,
    )
    self.assertEqual([], self.importer_roots)
    self.assertIn("OCR_MODEL_DOWNLOAD.md", "\n".join(result.messages))
```

```python
def test_bootstrap_starts_webui_and_mentions_the_manual_ocr_guide(self) -> None:
    script = BOOTSTRAP.read_text(encoding="utf-8")
    self.assertIn("desktop_control.ps1", script)
    self.assertIn("-Action Start", script)
    self.assertIn("OCR_MODEL_DOWNLOAD.md", script)
    self.assertNotIn("OcrMode", script)
```

- [ ] **Step 2: Run RED.**

```powershell
& $env:ANIMA_SOURCE_BOOTSTRAP_TEST_PYTHON -B -I -m unittest tests.unit.test_source_bootstrap_install tests.unit.test_source_bootstrap_powershell -v
```

Expected: constructor/argument assertions fail because there is no delayed model-only importer or Start call.

- [ ] **Step 3: Implement the minimum behavior.**

```python
def import_local_model_resource(
    project_root: Path, *, model_root: Path, staging_root: Path, runtime: Path,
) -> dict[str, object]:
    staged = stage_model_resource(model_root, staging_root / "resource-library")
    probe = _offline_probe(runtime, staging_root / "resource-library", staged)
    return {"resource": install_resource_package(project_root, staged), "probe": probe}
```

Add a small `ocr_resource.py` predicate that returns true only when all three expected filenames
are present under `ocr-model-archives\\`; hash verification remains inside the importer. Use an
injected model-only importer in tests and the new helper only after the base state is durable.
Import failure must raise, preserving the base state but publishing no partial OCR resource or
rebuilt runtime. A valid existing resource is verified and reported as idempotent without archives.

At the end of `bootstrap_install.ps1`, invoke:

```powershell
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $script:projectRoot 'packaging\scripts\desktop_control.ps1') -Action Start
if ($LASTEXITCODE -ne 0) { throw "WebUI failed to start; see $script:logPath" }
```

- [ ] **Step 4: Run green verification.**

```powershell
& $env:ANIMA_SOURCE_BOOTSTRAP_TEST_PYTHON -B -I -m unittest tests.unit.test_source_bootstrap_install tests.unit.test_source_bootstrap_powershell tests.unit.test_desktop_control -v
```

Expected: exit code 0; tests assert the installer never downloads OCR models.

- [ ] **Step 5: Commit the completed task.**

```powershell
git add packaging/installer/install.py packaging/scripts/ocr_resource.py packaging/scripts/bootstrap_install.ps1 tests/unit/test_source_bootstrap_install.py tests/unit/test_source_bootstrap_powershell.py tests/unit/test_ocr_resource_scripts.py
git commit -m "feat: start WebUI after source bootstrap"
```

## Task 3: Make OCR preflight guidance actionable and synchronize documentation

**Files:**
- Modify: `core/src/anima_core/job_preflight.py`
- Modify: `tests/unit/test_job_preflight.py`
- Modify: `OCR_MODEL_DOWNLOAD.md`
- Modify: `README.md`
- Modify: `RULES.md`
- Modify: `models/README.md`
- Modify: `docs/THIRD_PARTY_NOTICES.md`
- Modify: `packaging/scripts/Validate-SourceBootstrapRelease.ps1`
- Modify: `ROADMAP.md`
- Modify: `MEMORY.md`

- [ ] **Step 1: Write the failing preflight assertion.**

```python
with self.assertRaisesRegex(
    JobPreflightError,
    r"ocr_resource_install_required.*OCR_MODEL_DOWNLOAD\.md.*ocr-model-archives",
):
    service.preflight(self._ocr_config(source, enabled=True))
```

- [ ] **Step 2: Run RED.**

```powershell
& $env:ANIMA_SOURCE_BOOTSTRAP_TEST_PYTHON -B -I -m unittest tests.unit.test_job_preflight.JobPreparationServiceTests.test_ocr_enabled_requires_the_fixed_resource_when_not_installed -v
```

Expected: the current error lacks the canonical guide and archive directory.

- [ ] **Step 3: Implement and document exact guidance.**

```python
raise ResourceCatalogError(
    "ocr_resource_install_required: selected OCR resource "
    f"{OCR_MODEL_RESOURCE_ID} is unavailable; download the exact archives listed in "
    "OCR_MODEL_DOWNLOAD.md into <project-root>/ocr-model-archives and run Install-WebUI.bat"
) from exc
```

Update every user-facing document to state: default OCR off; no `-OcrMode`; archive directory is `ocr-model-archives`; the three official URL/name/size/SHA records remain unchanged; model absence blocks only OCR-enabled jobs. Mark the matching ROADMAP item `[-]` until production manifest and clean-machine evidence exist.
Update the release validator so `ocr-cpu` remains mandatory but `ocr-models` is not a required
automatic-download component. It must still fail closed for the absent production manifest and
unverified model licenses.

- [ ] **Step 4: Run green documentation and unit verification.**

```powershell
& $env:ANIMA_SOURCE_BOOTSTRAP_TEST_PYTHON -B -I -m unittest tests.unit.test_job_preflight tests.unit.test_ocr_resource_scripts -v
git diff --check
```

Expected: exit code 0 and no whitespace errors.

- [ ] **Step 5: Commit the completed task.**

```powershell
git add core/src/anima_core/job_preflight.py tests/unit/test_job_preflight.py OCR_MODEL_DOWNLOAD.md README.md RULES.md models/README.md docs/THIRD_PARTY_NOTICES.md packaging/scripts/Validate-SourceBootstrapRelease.ps1 ROADMAP.md MEMORY.md
git commit -m "docs: document manual OCR model bootstrap"
```

## Task 4: Integrate current main and record local evidence

**Files:**
- Modify: `ROADMAP.md`
- Modify: `MEMORY.md`

- [ ] **Step 1: Confirm current main E621 indexes remain in the branch.**

```powershell
git merge-base --is-ancestor main HEAD
git ls-files resource-library/classification-indexes/e621-classify-20260724-v1 resource-library/replacement-indexes/e621-replace-20260726-v2
```

Expected: both index packages are tracked; no model or runtime files are listed.

- [ ] **Step 2: Run source-bootstrap regression suite using only project-local Python.**

```powershell
$TestPython = 'E:\Desktop\Anima idg标准标注处理\.runtime-build\runtimes\core\python.exe'
& $TestPython -B -I -m unittest discover -s tests/unit -p 'test_source_bootstrap_*.py' -v
& $TestPython -B -I -m unittest discover -s tests/unit -p 'test_desktop_control.py' -v
& $TestPython -B -I -m unittest discover -s tests/integration -p 'test_source_bootstrap_fixture.py' -v
```

Expected: exit code 0. Do not substitute system Python.

- [ ] **Step 3: Run expected fail-closed gates and record them truthfully.**

```powershell
& .\packaging\scripts\Validate-SourceBootstrapRelease.ps1 -ProjectRoot .
```

Expected: nonzero because production `install-manifest.json` and Release metadata remain absent. This is evidence of a gate, not an installation success.

- [ ] **Step 4: Update evidence and commit.**

```powershell
git add ROADMAP.md MEMORY.md
git commit -m "docs: record manual OCR bootstrap verification"
```

## Plan self-review

- Coverage: Task 1 separates model readiness from base installation; Task 2 provides one-click start and model import; Task 3 makes missing OCR errors actionable; Task 4 preserves E621 index commits and records verified limits.
- Scope: no model mirror, no release upload, no CUDA driver/toolkit installation, no unrelated Core refactor.
- Ambiguity: base install can succeed without OCR model files, but OCR functionality cannot; the document and preflight make that distinction explicit.
