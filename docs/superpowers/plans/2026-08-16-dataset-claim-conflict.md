# Dataset Claim Conflict Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Return an actionable HTTP 409 for dataset claim conflicts, keep a blocked preflight task retryable, and safely release only claims owned by succeeded tasks.

**Architecture:** Give persisted-claim conflicts a typed exception carrying the owner task ID. `JobPreparationService` performs narrowly scoped succeeded-owner cleanup before its first lock acquisition and preserves `ready` for acquisition conflicts; the API maps lock errors to 409 while the existing frontend renders the returned explanation.

**Tech Stack:** Python 3.11, FastAPI, SQLite, Windows exclusive file handles, React/TypeScript, Playwright, project-embedded Core and Node runtimes.

---

### Task 1: Add failing claim ownership and API regressions

**Files:**
- Modify: `tests/unit/test_job_preflight.py`
- Modify: `tests/unit/test_api.py`
- Modify: `tests/unit/test_lifecycle_retention.py`

- [x] **Step 1: Add a real interrupted-claim regression**

Import the existing `DatasetLockError` and `OverlayLayout`. Create two preflight tasks for one temporary dataset, seed the first task as `interrupted` with an overlay and a `dataset_claims` row, then confirm the second task:

```python
with self.assertRaises(DatasetLockError):
    service.confirm_workspace(second.jobId, confirmed=True, confirmed_rebuild=False)
self.assertEqual("ready", database.get_job(second.jobId)["status"])
self.assertEqual(first.jobId, database.connection.execute(
    "SELECT job_id FROM dataset_claims WHERE dataset_root=?", (str(source),),
).fetchone()["job_id"])
self.assertTrue(layout.root.is_dir())
```

The setup must use `database.set_workspace_metadata`, valid state transitions through `preparing_workspace`, `database.mark_interrupted`, and the production `dataset_claims` columns. It must not open a Windows lock handle, matching a backend restart. Repeat the ownership assertion for `interrupted`, `failed`, and `cancelled_recoverable`; each must keep its claim and make the new task remain `ready`.

- [x] **Step 2: Add succeeded-claim cleanup regressions**

Add one DB-only case by changing the seeded owner from `interrupted` to `succeeded`, then assert confirming the second task succeeds and owns the single remaining claim. Add one live-lock case by confirming the first task through `JobPreparationService`, moving it through valid transitions to `succeeded`, and confirming the second task through the same service. Capture the current `DatasetLockError` and convert it into an assertion value so RED fails as an assertion rather than a test error:

```python
try:
    workspace = service.confirm_workspace(second.jobId, confirmed=True, confirmed_rebuild=False)
except DatasetLockError:
    workspace = None
self.assertIsNotNone(workspace)
assert workspace is not None
self.assertEqual("preparing_workspace", workspace["status"])
self.assertEqual(
    [second.jobId],
    [str(row["job_id"]) for row in database.connection.execute("SELECT job_id FROM dataset_claims")],
)
```

The interrupted case must remain a conflict; no test may delete its overlay or claim.

- [x] **Step 3: Keep startup cleanup limited to succeeded claims**

Extend the existing lifecycle claim sweep regression with `failed` and `cancelled_recoverable` owners. Assert the sweep deletes only the `succeeded` claim and preserves both recoverable claims.

- [x] **Step 4: Add the API 409 contract and exact explanation**

Use the real preparation service and seed an interrupted owner claim for task `3bc585`, then call the real `confirm-workspace` endpoint for the second task. Capture any raised exception into a value before asserting its type, so current `DatasetLockError` behavior produces a normal RED assertion failure:

```python
try:
    confirm(second_job_id, _WorkspaceBody(confirmed=True, confirmedRebuild=False))
except Exception as exc:
    rejected = exc
else:
    rejected = None
self.assertIsInstance(rejected, HTTPException)
assert isinstance(rejected, HTTPException)
self.assertEqual(409, rejected.status_code)
self.assertEqual(
    "Dataset is claimed by task 3bc585. Select it under Recent tasks: "
    "Recover keeps its progress and continues to hold the dataset; "
    "Discard deletes its overlay and releases the dataset.",
    rejected.detail,
)
```

- [x] **Step 5: Run RED tests and record the expected failures**

Run:

```text
.runtime-build\runtimes\core\python.exe -B -I tests\unit\test_job_preflight.py JobPreflightTests.test_dataset_claim_conflict_keeps_new_job_ready
.runtime-build\runtimes\core\python.exe -B -I tests\unit\test_job_preflight.py JobPreflightTests.test_succeeded_dataset_claim_is_released_before_confirmation
.runtime-build\runtimes\core\python.exe -B -I tests\unit\test_job_preflight.py JobPreflightTests.test_succeeded_live_dataset_lock_is_released_before_confirmation
.runtime-build\runtimes\core\python.exe -B -I tests\unit\test_api.py ControlPlaneApiTests.test_confirm_workspace_maps_dataset_claim_conflict_to_actionable_409
.runtime-build\runtimes\core\python.exe -B -I tests\unit\test_lifecycle_retention.py LifecycleAndRetentionTests.test_discard_and_startup_sweep_release_dataset_claims
```

Expected: the blocked task becomes `failed`, succeeded owners still block, and the API assertion observes `DatasetLockError` instead of an HTTP 409. All failures must be assertion failures caused by the missing behavior, not import or fixture errors.

- [x] **Step 6: Commit the RED tests**

```text
git add tests/unit/test_job_preflight.py tests/unit/test_api.py
git commit -m test:dataset-claim-conflict-red
```

### Task 2: Implement typed conflict handling and succeeded-owner cleanup

**Files:**
- Modify: `core/src/anima_core/locks.py`
- Modify: `core/src/anima_core/job_preflight.py`
- Modify: `core/src/anima_core/api_jobs.py`
- Modify: `core/src/anima_core/db_jobs.py`
- Test: `tests/unit/test_job_preflight.py`
- Test: `tests/unit/test_api.py`
- Test: `tests/unit/test_lifecycle_retention.py`

- [x] **Step 1: Add a typed persisted-claim conflict**

In `locks.py`, keep `DatasetLockError` as the base for all lock failures and add:

```python
class DatasetClaimConflict(DatasetLockError):
    def __init__(self, claiming_job_id: str) -> None:
        self.claiming_job_id = claiming_job_id
        super().__init__(f"dataset is claimed by task {claiming_job_id}")
```

Raise `DatasetClaimConflict(str(existing["job_id"]))` only when the SQLite claim belongs to another task. Do not alter Windows lock failure handling or integrity checks.

- [x] **Step 2: Clean only a succeeded owner before first acquisition**

In `JobPreparationService`, add a private helper that queries the exact canonical dataset root. If and only if the claim owner has persisted status `succeeded`, release its live `DatasetLock` from `self._locks` (including its retained database connection) or delete the DB-only claim. Re-read the owner/status inside the deleting transaction for the DB-only path:

```python
if lock is not None:
    lock.release(recovery_complete=True)
    lock.database.close()
    del self._locks[claiming_job_id]
else:
    with database.transaction(immediate=True):
        owner = database.connection.execute(
            """SELECT c.job_id,j.status FROM dataset_claims c
               JOIN jobs j ON j.job_id=c.job_id WHERE c.dataset_root=?""",
            (str(dataset),),
        ).fetchone()
        if owner is not None and owner["status"] == "succeeded":
            database.connection.execute(
                "DELETE FROM dataset_claims WHERE dataset_root=? AND job_id=?",
                (str(dataset), str(owner["job_id"])),
            )
```

Call this helper immediately before `DatasetLock.acquire`. Never release `interrupted`, `failed`, `cancelled_recoverable`, or unknown owners.

Keep startup recovery aligned with the same boundary: `clear_stale_dataset_claims` must delete only claims owned by `succeeded` jobs. `discarded` jobs are already released by the discard transaction; `failed` and `cancelled_recoverable` remain recoverable and must retain their claims.

- [x] **Step 3: Preserve retryability for initial acquisition errors**

Import `DatasetLockError`. Track whether the first lock acquisition returned successfully. Add a dedicated `except DatasetLockError` before the generic handler: an initial acquisition failure re-raises without changing the job from `ready` or creating an overlay; a later lock failure retains the existing cleanup and `failed` behavior.

- [x] **Step 4: Map the typed conflict to actionable HTTP 409**

Import `conflict`, `DatasetClaimConflict`, and `DatasetLockError` in `api_jobs.py`. Catch the typed error first and return the exact explanation from Task 1; catch other `DatasetLockError` values as 409 with their existing bounded detail. Keep KeyError as 404 and `JobPreflightError` as 400.

- [x] **Step 5: Run GREEN tests and focused regressions**

Run the four Task 1 commands, followed by:

```text
.runtime-build\runtimes\core\python.exe -B -I tests\unit\test_job_preflight.py
.runtime-build\runtimes\core\python.exe -B -I tests\unit\test_api.py
.runtime-build\runtimes\core\python.exe -B -I tests\unit\test_lifecycle_retention.py
.runtime-build\runtimes\core\python.exe -B -I tests\unit\test_pipeline.py PipelineTests.test_startup_recovery_freezes_active_jobs_and_sweeps_finished_claims
```

Expected: all pass; the interrupted startup test continues to assert that its claim remains.

- [x] **Step 6: Commit the backend fix**

```text
git add core/src/anima_core/locks.py core/src/anima_core/job_preflight.py core/src/anima_core/api_jobs.py core/src/anima_core/db_jobs.py
git commit -m fix:dataset-claim-conflict
```

### Task 3: Verify the visible explanation and packaged runtime

**Files:**
- Modify: `frontend/tests/e2e/workflow.spec.ts`
- Generated: `.runtime-build/runtimes/core/Lib/site-packages/anima_core/locks.py`
- Generated: `.runtime-build/runtimes/core/Lib/site-packages/anima_core/job_preflight.py`
- Generated: `.runtime-build/runtimes/core/Lib/site-packages/anima_core/api_jobs.py`
- Generated: `frontend/dist/**` only if the deterministic build differs
- Modify: `ROADMAP.md`
- Modify: `MEMORY.md`

- [x] **Step 1: Add the browser explanation regression**

Use existing `failRoute` with status 409 for the task's `confirm-workspace` route, accept the confirmation dialog, and assert `.action-feedback [role=alert]` contains the full owner ID plus all four instructions: Recent tasks, Recover, progress/hold, and Discard/overlay/release.

- [x] **Step 2: Run the browser regression**

Run from `frontend` with the project toolchain:

```text
node_modules\.bin\playwright.cmd test workflow.spec.ts --project=chromium
```

Expected: the action feedback displays the supplied 409 detail and never displays `request failed: 409` or `request failed: 500`.

- [x] **Step 3: Sync Core runtime and prove zero drift**

Run via CMD:

```text
C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe -NoProfile -ExecutionPolicy Bypass -File packaging\scripts\Sync-CoreRuntime.ps1 -Apply
C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe -NoProfile -ExecutionPolicy Bypass -File packaging\scripts\Sync-CoreRuntime.ps1
.runtime-build\runtimes\core\python.exe -B -I tests\contract\test_assembled_tree_drift.py
```

Expected: apply copies only the changed Core modules, the second preview reports 0 add / 0 update / 0 remove, and drift tests pass.

- [x] **Step 4: Rebuild and verify the frontend**

Run from `frontend`:

```text
npm.cmd run typecheck
npm.cmd run build
```

Expected: both exit 0; no frontend source change is required because the existing API error region renders the backend detail.

- [x] **Step 5: Run project and packaging gates**

Run the focused backend suites again from the synchronized runtime, `git diff --check`, Core runtime drift, source-bootstrap install/manifest tests, and the relevant Playwright file. Do not claim full-suite success unless every invoked suite exits 0.

- [x] **Step 6: Update project records and commit generated evidence**

Mark the ROADMAP items complete only after the commands pass, record exact counts and any unverified full-suite areas in `MEMORY.md`, then commit tracked Core runtime, browser regression, deterministic frontend output if changed, and the design clarification:

```text
git add -f docs/superpowers/specs/2026-08-16-dataset-claim-conflict-design.md docs/superpowers/plans/2026-08-16-dataset-claim-conflict.md
git add frontend/tests/e2e/workflow.spec.ts .runtime-build/runtimes/core frontend/dist
git commit -m fix:verify-dataset-claim-conflict
```
