# Reliability And 100k Stability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix five reproducible reliability failures while preserving the existing bounded 100,000-image control-plane behavior.

**Architecture:** Keep the current Pipeline, lifecycle, SQLite, and worker JSONL boundaries. Apply local owner-level fixes and add regression tests before each production change; do not split broad facades or tune performance without a failed capacity assertion.

**Tech Stack:** Python 3.11, `unittest`, SQLite WAL, Windows process/file handles, synchronous JSONL worker processes, project-embedded runtimes.

---

### Task 1: Enforce Pipeline Thread State Invariants

**Files:**
- Modify: `tests/unit/test_pipeline.py`
- Modify: `tests/unit/test_api.py`
- Modify: `core/src/anima_core/pipeline.py`
- Modify: `core/src/anima_core/pipeline_recovery.py`
- Modify: `core/src/anima_core/api_nl.py`

- [x] Add focused tests proving initial start failure unregisters the thread, resume while a previous thread is registered leaves persisted state paused and returns a controlled error, and recovery start failure restores `interrupted` plus its original `resume_status`.
- [x] Run each new test against the current source and verify it fails for the reproduced state mismatch.
- [x] Add the minimal unregister/rollback logic at each existing `thread.start()` site; perform the resume in-process registration check before any SQLite mutation.
- [x] Make NL resume translate `PipelineError` through the existing bad-request path, matching Policy resume.
- [x] Run `tests\unit\test_pipeline.py`, `tests\unit\test_api.py`, and `tests\contract\test_core_module_decomposition.py`; expect zero failures.

### Task 2: Release Live Dataset Locks On Discard

**Files:**
- Modify: `tests/unit/test_lifecycle_retention.py`
- Modify: `tests/unit/test_api.py`
- Modify: `core/src/anima_core/job_preflight.py`
- Modify: `core/src/anima_core/api_jobs.py`

- [x] Add a Windows regression that holds a real `DatasetLock`, performs formal discard, invokes the service-level release path, and proves a second job can acquire the same dataset immediately.
- [x] Add an API regression proving the discard route coordinates the live-lock release after persisted lifecycle completion.
- [x] Run the tests against current source and verify the reacquire case fails with Windows sharing violation 32.
- [x] Add an idempotent `release_lock_for_discard(job_id)` owner method that validates `discarded`, releases and closes the lock, and removes `_locks[job_id]`.
- [x] Call it from the discard route after `JobLifecycle.discard` succeeds.
- [x] Run lifecycle, preflight, API, and path-lock tests; expect zero failures.
- [x] Preserve a retry path when live-lock release fails after the durable `discarded` transition.

### Task 3: Drain Worker Stderr With Bounded Memory

**Files:**
- Modify: `tests/unit/test_stdio_transport.py`
- Modify: `core/src/anima_core/stdio_transport.py`

- [x] Add a real-child-process regression whose worker writes 1 MiB to stderr before its JSONL response; enforce bounded cleanup so the red test cannot hang the suite.
- [x] Run the regression against current source and verify it reports the exchange thread still blocked.
- [x] Start a daemon stderr drainer in transport initialization, retain no more than 64 KiB, and join/close it during transport shutdown.
- [x] Run transport tests and worker boundary tests; expect the 1 MiB exchange to complete and all protocol checks to remain green.
- [x] Reap the spawned worker and wrap the error if the stderr drainer thread itself cannot start.

### Task 4: Preserve 100k Capacity And Project Boundaries

**Files:**
- Modify: `ROADMAP.md`
- Modify: `MEMORY.md`

- [x] Run the focused Core unit, contract, and integration suites through the project-embedded Python.
- [x] Run `tests\stress\test_control_plane_100k.py -v`; require both tests to pass and retain the existing memory/database/WAL assertions.
- [x] Run the project verification entry points, frontend checks available in the project toolchain, and `git diff --check`.
- [x] Confirm `.test-tmp` is empty, no WebUI/test ports or project test processes remain, and only intended files are staged.
- [x] Record exact verification evidence and known external acceptance limitations in `ROADMAP.md` and `MEMORY.md`.
- [x] Commit the verified changes directly on `main` with a reliability-focused message.
