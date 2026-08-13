# Source Bootstrap Acceptance

This procedure is for real isolated Windows machines or VMs only. A fixture, an import-only
check, or a host with Python, Node, CUDA Toolkit, Visual Studio, or Windows SDK does not prove
clean-machine acceptance.

Run the tracked acceptance runner from the extracted source root:

```powershell
.\packaging\scripts\Invoke-SourceBootstrapAcceptance.ps1 -ProjectRoot . -Scenario Cpu
```

It records one JSON result below `.runtime-build\acceptance`. `passed` requires a clean-host
preflight, a successful `Install-WebUI.bat` run, a matching install state, and a final
successful `Stop-WebUI.bat` run. A missing or nonzero Stop command makes the result `failed`.
The `webUiStartExitCode` records the `Install-WebUI.bat` result,
which includes its bootstrap-controlled WebUI start step; the runner does not start WebUI a
second time. `Stop-WebUI.bat` runs only after this runner actually invoked `Install-WebUI.bat`.
`not-clean` is evidence that the host is unsuitable; it is never a passing result.

Run and retain one JSON result for each scenario:

1. Windows 10 CPU: no Python, `py`, Node, npm, CUDA Toolkit, Visual Studio, or Windows SDK.
2. Windows 11 CPU interrupted-download: interrupt the bootstrap download once, then rerun and
   retain the resumable-download evidence and final acceptance JSON.
3. Windows 11 NVIDIA: use an existing supported NVIDIA driver, select `-Scenario Nvidia`, and
   retain CUDA Caption/Policy plus OCR GPU evidence from the install state and installer logs.
4. Chinese/space source ZIP path: extract the same released source ZIP into a path containing
   Chinese characters and spaces, then repeat the applicable CPU or NVIDIA run.

For a developer workstation, only use the preflight to record its non-clean status:

```powershell
.\packaging\scripts\Invoke-SourceBootstrapAcceptance.ps1 -ProjectRoot . -Scenario Cpu -PreflightOnly
```

Do not upload acceptance JSON files, model weights, or OCR archives as part of this procedure.
