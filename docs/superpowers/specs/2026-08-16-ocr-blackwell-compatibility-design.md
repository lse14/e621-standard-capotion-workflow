# OCR Blackwell Compatibility Design

## Goal

Improve the probability that the bundled OCR GPU runtime works on RTX 50-series Blackwell GPUs, while preserving deterministic CPU fallback for automatic device selection and explicit failure for forced CUDA selection.

## Scope

1. Replace the OCR GPU Paddle wheel from `paddlepaddle-gpu 3.2.2` built for CUDA 12.6 with the official Windows CPython 3.11 `paddlepaddle-gpu 3.3.0` wheel built for CUDA 12.9.
2. Keep PaddleOCR `3.7.0` and PaddleX `3.7.2` unless dependency resolution or real OCR validation proves that either must change.
3. Add a bounded startup probe that performs a deterministic CUDA matrix operation, synchronizes the device, and reads the result back to the host.
4. Preserve existing immutable OCR runtime bindings without re-probing, replacing, or migrating them.
5. Regenerate all affected requirement locks, runtime manifests, source-bootstrap inventories, sizes, and SHA-256 identities.

No new device option, task schema, OCR response schema, model resource, or unrelated UI control is introduced.

## Runtime Selection

For a new task without an OCR runtime binding:

1. `device=cpu` selects `ocr-paddle` without launching the GPU probe.
2. `device=auto` resolves `ocr-paddle-gpu` and runs the real CUDA operation probe.
3. A successful probe returns the observed VRAM and selects `ocr-paddle-gpu`.
4. A missing runtime, timeout, malformed response, unavailable CUDA device, CUDA kernel launch error, synchronization error, or wrong numeric result selects `ocr-paddle` and records the existing `startupReason=gpu_runtime_unavailable` value.
5. `device=cuda` applies the same probe but does not fall back. Failure returns a bounded message stating that the installed OCR CUDA runtime is incompatible or unavailable and instructs the user to choose Auto or CPU.

The probe must not inspect a GPU name or hard-code `sm_120`. Compatibility is determined by executing the installed Paddle runtime on the selected device.

## Probe Contract

The probe runs inside the isolated `ocr-paddle-gpu` interpreter with its launcher-provided environment. It must:

1. Confirm that Paddle was compiled with CUDA and at least one CUDA device is visible.
2. Select `gpu:0`.
3. Multiply fixed small matrices on the GPU.
4. synchronize the CUDA device explicitly.
5. copy one result to the host and compare it with the exact expected finite value.
6. return only a bounded JSON object containing `totalVramBytes`.

The existing 15-second timeout and bounded stdout contract remain. Probe stderr and raw provider errors are not returned through the API.

## Forced CUDA Error

For a new forced-CUDA task, the compatibility probe runs synchronously before the pipeline thread starts. A failure is returned through the existing start endpoint as a bad-request response with this stable user-facing meaning:

`The OCR CUDA runtime is unavailable or incompatible with this GPU. Choose Auto or CPU.`

The task remains in its pre-start workspace state, so the user can create a new task with another device choice. No background worker starts after this validation fails.

Automatic selection remains asynchronous-safe: it repeats the compatibility decision when OCR is reached and falls back to CPU without failing the task.

## Existing Bindings

If `ocr-runtime-binding-v1.json` already exists, runtime selection continues to read it directly. The new probe does not alter, delete, or replace that file. A task frozen to an older incompatible GPU binding must be replaced by a new task configured with Auto or CPU.

This preserves task reproducibility and the repair-child inheritance contract.

## Packaging

The official GPU input changes to:

`https://paddle-whl.bj.bcebos.com/stable/cu129/paddlepaddle-gpu/paddlepaddle_gpu-3.3.0-cp311-cp311-win_amd64.whl`

The build must resolve a fresh wheel inventory and regenerate both copies of `ocr-paddle-gpu.lock`. The assembled runtime, its manifest, and the source-bootstrap installer inventories must use the resulting exact sizes and SHA-256 values. Existing integrity checks are not relaxed.

The OCR worker runtime evidence must require Paddle `3.3.0` for the GPU runtime while continuing to require the existing CPU Paddle version for `ocr-paddle`. PaddleOCR and PaddleX evidence remains fixed to their verified versions.

## Tests

Test-first coverage must include:

1. A successful CUDA operation probe returns VRAM and selects GPU.
2. A simulated unsupported-compute-capability kernel failure makes `auto` select CPU.
3. The same simulated failure makes `cuda` return the stable incompatibility error and does not start a thread.
4. CPU selection never invokes the GPU probe.
5. Existing CPU and GPU bindings are read without a new probe or mutation.
6. Worker runtime evidence accepts GPU Paddle `3.3.0` and keeps the CPU version contract unchanged.
7. Packaging tests require the CUDA 12.9 Paddle wheel and regenerated exact artifact identities.
8. Source-bootstrap installation and second-start validation remain green.

Real validation on this development machine covers the CUDA operation probe and the existing three-model OCR probe on RTX 4090. RTX 50-series compatibility remains unverified until the recipient runs the same packaged probes on an RTX 50-series machine.

## Acceptance Criteria

1. New Auto tasks never bind the GPU runtime when a real Paddle CUDA operation cannot execute and synchronize correctly.
2. New forced-CUDA tasks receive a clear compatibility error without CPU fallback or background pipeline startup.
3. Existing OCR binding files remain byte-for-byte unchanged.
4. The packaged GPU runtime reports Paddle `3.3.0` and CUDA 12.9 evidence.
5. RTX 4090 real CUDA and three-model OCR validation pass.
6. All affected backend, worker, packaging, installer, runtime-drift, and frontend-build gates pass.
7. Documentation reports RTX 50 support as pending real-device validation until evidence from an RTX 50-series machine exists.

## Risks

- Paddle `3.3.0` may be incompatible with the currently frozen PaddleOCR or PaddleX versions. The upgrade stops rather than silently changing those dependencies unless a failing test or build result proves a required compatible version change.
- A small matrix operation proves basic Paddle kernel execution but cannot prove every OCR operator. The existing three-model real OCR probe remains the stronger packaging gate where the models and GPU are available.
- The GPU component is large. Interrupted downloads must use the existing staged, hash-verified build path and must not publish partial artifacts.
