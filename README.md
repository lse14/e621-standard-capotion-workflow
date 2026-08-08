# Anima Dataset Annotation Tool

Anima is a local Windows application for turning image datasets into reviewed
caption, tag, classification, natural-language, and export annotations. The
application is built around a fixed workflow:

`Caption -> Classify -> Replace -> OCR (optional) -> NL -> Count Review -> Dropout -> Export`

The control plane is a local HTTP service with a React/Vite frontend. Core
processing and each worker communicate through versioned JSON contracts.

## Current Scope

- E621 is the supported end-to-end profile.
- Caption supports v8 TXT interpretation modes:
  - `Tag` (the default): a non-empty TXT is parsed as tags and skips the image
    tagger. Missing or empty TXT can fall back to the tagger by default. When
    fallback is disabled, the sample receives a non-blocking warning and stays
    out of Export until the source TXT is fixed and the task is rerun.
  - `NL`: the baseline TXT becomes the final JSON `nl` value while the tagger
    still produces tags for Classify. Invalid UTF-8, NUL bytes, or TXT larger
    than 16,384 UTF-8 bytes block the task.
- OCR is an optional local installation (`None`, `Cpu`, or `Gpu`) and is
  disabled in the base source snapshot.
- Count Review and Token Budget Review remain explicit review stages; TXT
  mode does not create a separate hidden review queue.

Danbooru formal model/resource acceptance is not part of the supported release
path yet. OCR model resources are local-only and are not redistributed here.

## Repository Contents

This repository contains application source, worker implementations, JSON
contracts, profiles, packaging scripts, dependency lock files, and tests.

The following are intentionally excluded from GitHub: work notes and plans,
embedded Python/Node runtimes, browser binaries, model weights, resource
payloads, wheelhouse packages, datasets, logs, databases, and generated output.
Those files may exist in a local release workspace but are not source files.

## Prerequisites

- Windows PowerShell 5.1 or later.
- A project-local embedded runtime assembled according to the packaging scripts:
  - `.runtime-build\runtimes\core\python.exe`
  - `.toolchains\node-v24.18.0-win-x64\node.exe`
  - `.toolchains\node-v24.18.0-win-x64\npm.cmd`
- Any model/resource archives required by the selected profile, obtained and
  verified through your own approved distribution process.

The source repository does not download or redistribute runtimes, browsers,
models, or user data.

## Run The WebUI

From the project root in Windows PowerShell:

```powershell
.\Install-WebUI.bat
.\Start-WebUI.bat
# Use .\Stop-WebUI.bat when finished.
```

`Install-WebUI.bat` accepts `-OcrMode None`, `-OcrMode Cpu`, or
`-OcrMode Gpu`. The default is `None`; OCR installation is explicit and
requires the corresponding locally verified resources.

## Verification

The project verification entry point uses only project-local interpreters:

```powershell
& .\packaging\scripts\Verify-Project.ps1 -Level Fast
& .\packaging\scripts\Verify-Project.ps1 -Level Full
& .\packaging\scripts\Verify-Project.ps1 -Level Release
```

`Fast` runs the embedded Core/contract/worker checks and frontend typecheck;
`Full` also builds the frontend; `Release` adds assembled-tree, resource, and
browser checks. Browser interaction and visual checks require the project-local
Playwright Chromium binary. If it is absent, those checks remain unverified;
the project does not silently use a system browser.

Frontend-only commands, once the local Node toolchain and dependencies are
available:

```powershell
Set-Location frontend
..\.toolchains\node-v24.18.0-win-x64\npm.cmd run typecheck
..\.toolchains\node-v24.18.0-win-x64\npm.cmd run build
```

## Architecture

| Area | Entry points |
| --- | --- |
| HTTP API and request models | `core/src/anima_core/api*.py` |
| Pipeline and recovery | `core/src/anima_core/pipeline*.py` |
| Database and review/export operations | `core/src/anima_core/db*.py` |
| Worker implementations | `workers/*/src/` |
| Frontend | `frontend/src/` |
| JSON contracts | `contracts/schemas/` |
| Packaging and verification | `packaging/scripts/` |

## Security

Keep API keys and local credentials outside the repository. Diagnostics use
transient credentials and should be run only against endpoints you control.
Before publishing any fork, run a secret scan and inspect the staged file list.

## License

No license has been declared in this source snapshot. Add an appropriate
`LICENSE` file before distributing it to third parties.
